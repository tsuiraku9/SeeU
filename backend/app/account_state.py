from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .archive import sanitize_component
from .config import Settings
from .models import Account, AccountStatus, CrawlRun, JobStatus, ObservedContent, utcnow


LEDGER_SCHEMA_VERSION = 2
INTERRUPTED_POLL_ERROR = "Polling interrupted by service restart before completion"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()


def account_ledger_path(settings: Settings, account: Account) -> Path:
    return (
        settings.archive_root.resolve()
        / "_state"
        / "accounts"
        / account.platform.value
        / f"{sanitize_component(account.slug, 'account')}.json"
    )


def observed_ids_for_account(db: Session, account: Account) -> list[str]:
    rows = list(
        db.scalars(
            select(ObservedContent.remote_id)
            .where(
                ObservedContent.account_id == account.id,
                ObservedContent.retry_pending.is_(False),
            )
            .order_by(ObservedContent.id)
        )
    )
    # Upgrade legacy databases lazily: their JSON high-water marks remain valid.
    return list(dict.fromkeys([*(account.seen_ids or []), *rows]))


def pending_refs_for_account(db: Session, account: Account) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(ObservedContent)
            .where(
                ObservedContent.account_id == account.id,
                ObservedContent.retry_pending.is_(True),
            )
            .order_by(ObservedContent.first_seen_at, ObservedContent.id)
        )
    )
    return [
        {
            "remote_id": row.remote_id,
            "source_url": row.source_url,
            "attempt_count": row.attempt_count,
            "last_attempt_at": _iso(row.last_attempt_at),
            "last_error": row.last_error,
        }
        for row in rows
    ]


def write_account_ledger(
    settings: Settings,
    account: Account,
    observed_ids: Iterable[str] | None = None,
    pending_refs: Iterable[dict[str, Any]] | None = None,
) -> Path:
    ids = list(dict.fromkeys(str(value) for value in (observed_ids or []) if str(value)))
    pending = []
    pending_ids: set[str] = set()
    for value in pending_refs or []:
        remote_id = str(value.get("remote_id") or "").strip()
        source_url = str(value.get("source_url") or "").strip()
        if not remote_id or not source_url or remote_id in pending_ids:
            continue
        pending_ids.add(remote_id)
        pending.append(
            {
                "remote_id": remote_id,
                "source_url": source_url,
                "attempt_count": max(0, int(value.get("attempt_count") or 0)),
                "last_attempt_at": value.get("last_attempt_at"),
                "last_error": str(value.get("last_error") or "")[:4000] or None,
            }
        )
    account.ledger_revision = int(account.ledger_revision or 0) + 1
    payload = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "account_key": f"{account.platform.value}:{sanitize_component(account.slug, 'account')}",
        "revision": account.ledger_revision,
        "deleted": False,
        "platform": account.platform.value,
        "display_name": account.display_name,
        "slug": account.slug,
        "source_url": account.source_url,
        "enabled": account.enabled,
        "interval_minutes": account.interval_minutes,
        "baseline_established": account.baseline_established,
        "completeness_status": account.completeness_status.value,
        "gap_detected_at": _iso(account.gap_detected_at),
        "status": account.status.value,
        "consecutive_failures": account.consecutive_failures,
        "last_error": account.last_error,
        "seen_ids": ids,
        "pending_refs": pending,
        "last_polled_at": _iso(account.last_polled_at),
        "next_poll_at": _iso(account.next_poll_at),
        "created_at": _iso(account.created_at),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    target = account_ledger_path(settings, account)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def remove_account_ledger(settings: Settings, account: Account) -> None:
    account_ledger_path(settings, account).unlink(missing_ok=True)


def write_account_tombstone(settings: Settings, account: Account) -> Path:
    account.ledger_revision = int(account.ledger_revision or 0) + 1
    target = account_ledger_path(settings, account)
    payload = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "account_key": f"{account.platform.value}:{sanitize_component(account.slug, 'account')}",
        "revision": account.ledger_revision,
        "deleted": True,
        "platform": account.platform.value,
        "slug": account.slug,
        "source_url": account.source_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def sync_all_account_ledgers(db: Session, settings: Settings) -> int:
    accounts = list(db.scalars(select(Account).order_by(Account.id)))
    created = 0
    for account in accounts:
        if account_ledger_path(settings, account).exists():
            continue
        write_account_ledger(
            settings,
            account,
            observed_ids_for_account(db, account),
            pending_refs_for_account(db, account),
        )
        created += 1
    return created


def recover_interrupted_polls(db: Session, settings: Settings) -> tuple[int, int]:
    """Finish orphaned poll runs and make their accounts safely schedulable again.

    A process restart cannot resume an in-memory collector task.  Any persisted
    ``running`` run and any account still marked ``polling`` are therefore stale.
    This recovery deliberately leaves completeness, failure counters, seen IDs,
    and pending observations unchanged; those describe collection integrity, not
    process liveness.
    """

    interrupted_at = utcnow()
    runs = list(
        db.scalars(
            select(CrawlRun)
            .where(CrawlRun.status == JobStatus.running)
            .order_by(CrawlRun.id)
        )
    )
    account_ids = {run.account_id for run in runs}
    account_ids.update(
        db.scalars(select(Account.id).where(Account.status == AccountStatus.polling))
    )

    for run in runs:
        run.status = JobStatus.failed
        run.finished_at = run.finished_at or interrupted_at
        run.error = INTERRUPTED_POLL_ERROR
        details = dict(run.details or {})
        details["recovered_after_restart"] = True
        run.details = details

    if not account_ids:
        return len(runs), 0

    accounts = list(
        db.scalars(select(Account).where(Account.id.in_(account_ids)).order_by(Account.id))
    )
    for account in accounts:
        if account.enabled:
            account.status = AccountStatus.pending
            account.next_poll_at = interrupted_at
        else:
            account.status = AccountStatus.paused

    db.flush()
    for account in accounts:
        write_account_ledger(
            settings,
            account,
            observed_ids_for_account(db, account),
            pending_refs_for_account(db, account),
        )
    return len(runs), len(accounts)
