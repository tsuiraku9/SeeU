from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.account_state import (
    INTERRUPTED_POLL_ERROR,
    account_ledger_path,
    observed_ids_for_account,
    pending_refs_for_account,
    recover_interrupted_polls,
    write_account_ledger,
)
from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    Account,
    AccountStatus,
    CompletenessStatus,
    CrawlRun,
    JobStatus,
    ObservedContent,
    Platform,
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def test_recovery_finishes_runs_reschedules_accounts_and_preserves_integrity() -> None:
    settings = get_settings()
    gap_at = datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc)
    disabled_schedule = datetime.now(timezone.utc) + timedelta(days=1)
    with SessionLocal() as db:
        enabled = Account(
            platform=Platform.weibo,
            display_name="enabled",
            slug="interrupted-enabled",
            source_url="https://weibo.com/u/10001",
            enabled=True,
            baseline_established=True,
            completeness_status=CompletenessStatus.gap_detected,
            gap_detected_at=gap_at,
            seen_ids=["terminal-json"],
            status=AccountStatus.polling,
            next_poll_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        disabled = Account(
            platform=Platform.bilibili,
            display_name="disabled",
            slug="interrupted-disabled",
            source_url="https://space.bilibili.com/10002",
            enabled=False,
            baseline_established=True,
            completeness_status=CompletenessStatus.pending_retry,
            status=AccountStatus.polling,
            next_poll_at=disabled_schedule,
        )
        untouched = Account(
            platform=Platform.bilibili,
            display_name="untouched",
            slug="untouched",
            source_url="https://space.bilibili.com/10003",
            status=AccountStatus.healthy,
        )
        db.add_all([enabled, disabled, untouched])
        db.flush()
        db.add_all(
            [
                ObservedContent(
                    account_id=enabled.id,
                    remote_id="terminal-row",
                    source_url="https://m.weibo.cn/status/terminal-row",
                ),
                ObservedContent(
                    account_id=enabled.id,
                    remote_id="retry-enabled",
                    source_url="https://m.weibo.cn/status/retry-enabled",
                    retry_pending=True,
                    attempt_count=2,
                    last_error="media incomplete",
                ),
                ObservedContent(
                    account_id=disabled.id,
                    remote_id="retry-disabled",
                    source_url="https://www.bilibili.com/video/BV1retry",
                    retry_pending=True,
                    attempt_count=1,
                    last_error="provider unavailable",
                ),
            ]
        )
        enabled_run = CrawlRun(
            account_id=enabled.id,
            status=JobStatus.running,
            details={"returned_count": 7},
        )
        disabled_run = CrawlRun(account_id=disabled.id, status=JobStatus.running)
        completed_run = CrawlRun(
            account_id=untouched.id,
            status=JobStatus.complete,
            finished_at=datetime.now(timezone.utc),
        )
        db.add_all([enabled_run, disabled_run, completed_run])
        db.flush()
        write_account_ledger(
            settings,
            enabled,
            observed_ids_for_account(db, enabled),
            pending_refs_for_account(db, enabled),
        )
        write_account_ledger(
            settings,
            disabled,
            observed_ids_for_account(db, disabled),
            pending_refs_for_account(db, disabled),
        )
        db.commit()
        enabled_id = enabled.id
        disabled_id = disabled.id
        untouched_id = untouched.id
        enabled_run_id = enabled_run.id
        disabled_run_id = disabled_run.id
        completed_run_id = completed_run.id

    recovery_started = datetime.now(timezone.utc)
    with SessionLocal() as db:
        assert recover_interrupted_polls(db, settings) == (2, 2)
        db.commit()

        enabled = db.get(Account, enabled_id)
        disabled = db.get(Account, disabled_id)
        untouched = db.get(Account, untouched_id)
        enabled_run = db.get(CrawlRun, enabled_run_id)
        disabled_run = db.get(CrawlRun, disabled_run_id)
        completed_run = db.get(CrawlRun, completed_run_id)
        assert enabled and disabled and untouched
        assert enabled_run and disabled_run and completed_run

        assert enabled.status == AccountStatus.pending
        assert enabled.next_poll_at is not None
        assert _utc(enabled.next_poll_at) >= recovery_started
        assert disabled.status == AccountStatus.paused
        assert disabled.next_poll_at is not None
        assert _utc(disabled.next_poll_at) == disabled_schedule
        assert untouched.status == AccountStatus.healthy

        assert enabled.completeness_status == CompletenessStatus.gap_detected
        assert _utc(enabled.gap_detected_at) == gap_at
        assert disabled.completeness_status == CompletenessStatus.pending_retry
        pending = {
            row.remote_id: row
            for row in db.scalars(
                select(ObservedContent).where(
                    ObservedContent.account_id.in_([enabled_id, disabled_id])
                )
            )
        }
        assert pending["retry-enabled"].retry_pending is True
        assert pending["retry-enabled"].attempt_count == 2
        assert pending["retry-enabled"].last_error == "media incomplete"
        assert pending["retry-disabled"].retry_pending is True
        assert pending["terminal-row"].retry_pending is False

        for run in (enabled_run, disabled_run):
            assert run.status == JobStatus.failed
            assert run.finished_at is not None
            assert run.error == INTERRUPTED_POLL_ERROR
            assert run.details["recovered_after_restart"] is True
        assert enabled_run.details["returned_count"] == 7
        assert completed_run.status == JobStatus.complete

        ledger_revision = enabled.ledger_revision
        assert recover_interrupted_polls(db, settings) == (0, 0)
        assert enabled.ledger_revision == ledger_revision

    enabled_ledger = json.loads(
        account_ledger_path(settings, enabled).read_text(encoding="utf-8")
    )
    disabled_ledger = json.loads(
        account_ledger_path(settings, disabled).read_text(encoding="utf-8")
    )
    assert enabled_ledger["status"] == "pending"
    assert enabled_ledger["completeness_status"] == "gap_detected"
    assert {item["remote_id"] for item in enabled_ledger["pending_refs"]} == {
        "retry-enabled"
    }
    assert {"terminal-json", "terminal-row"} <= set(enabled_ledger["seen_ids"])
    assert disabled_ledger["status"] == "paused"
    assert disabled_ledger["completeness_status"] == "pending_retry"
    assert [item["remote_id"] for item in disabled_ledger["pending_refs"]] == [
        "retry-disabled"
    ]


def test_application_startup_recovers_a_persisted_interrupted_poll() -> None:
    settings = get_settings()
    future_poll = datetime.now(timezone.utc) + timedelta(days=2)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.weibo,
            display_name="startup",
            slug="startup-interrupted",
            source_url="https://weibo.com/u/20001",
            enabled=True,
            baseline_established=True,
            completeness_status=CompletenessStatus.pending_retry,
            status=AccountStatus.polling,
            next_poll_at=future_poll,
        )
        db.add(account)
        db.flush()
        db.add(
            ObservedContent(
                account_id=account.id,
                remote_id="retry-after-startup",
                source_url="https://m.weibo.cn/status/retry-after-startup",
                retry_pending=True,
                last_error="interrupted media",
            )
        )
        run = CrawlRun(account_id=account.id, status=JobStatus.running)
        db.add(run)
        db.flush()
        write_account_ledger(
            settings,
            account,
            observed_ids_for_account(db, account),
            pending_refs_for_account(db, account),
        )
        db.commit()
        account_id = account.id
        run_id = run.id

    startup_started = datetime.now(timezone.utc)
    with TestClient(app):
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            run = db.get(CrawlRun, run_id)
            assert account and run
            assert account.status == AccountStatus.pending
            assert account.next_poll_at is not None
            assert _utc(account.next_poll_at) >= startup_started
            assert account.completeness_status == CompletenessStatus.pending_retry
            pending = db.scalar(
                select(ObservedContent).where(
                    ObservedContent.account_id == account_id,
                    ObservedContent.remote_id == "retry-after-startup",
                )
            )
            assert pending is not None and pending.retry_pending is True
            assert run.status == JobStatus.failed
            assert run.finished_at is not None
            assert run.error == INTERRUPTED_POLL_ERROR

