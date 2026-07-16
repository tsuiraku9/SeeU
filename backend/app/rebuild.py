from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .adapters.base import NormalizedContent
from .archive import ArchiveError, ArchiveManager, sanitize_component
from .config import Settings
from .models import (
    Account,
    AccountStatus,
    CompletenessStatus,
    ContentIndex,
    JobStatus,
    ObservedContent,
    Platform,
)


logger = logging.getLogger(__name__)


def _datetime(value: object, default: datetime | None = None) -> datetime | None:
    if not value:
        return default
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return default
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _content_from_metadata(
    data: dict,
    *,
    expected_platform: Platform | None = None,
    expected_remote_id: str | None = None,
) -> NormalizedContent:
    platform = Platform(str(data["platform"]))
    remote_id = str(data["content_id"]).strip()
    published_at = _datetime(data.get("published_at"))
    if not remote_id or published_at is None:
        raise ArchiveError("Archive metadata is missing its content identity or publication time")
    if expected_platform is not None and platform != expected_platform:
        raise ArchiveError("Archive platform does not match the index row")
    if expected_remote_id is not None and remote_id != expected_remote_id:
        raise ArchiveError("Archive content ID does not match the index row")
    return NormalizedContent(
        platform=platform,
        remote_id=remote_id,
        source_url=str(data.get("source_url", "")),
        title=str(data.get("title", "")),
        author=str(data.get("author", "")),
        text=str(data.get("text", "")),
        published_at=published_at,
        content_type=str(data.get("content_type", "text")),
    )


def _require_declared_manifest_types(data: dict) -> None:
    """Do not let filename inference turn an incomplete canonical manifest into proof."""

    media = data.get("media")
    if not isinstance(media, list):
        raise ArchiveError("Archive media manifest is invalid")
    if any(
        not isinstance(record, dict) or not str(record.get("mime_type") or "").strip()
        for record in media
    ):
        raise ArchiveError("Archive media manifest is missing a declared MIME type")


def _canonical_archive_location(
    metadata_path: Path,
    settings: Settings,
    content: NormalizedContent,
) -> tuple[Path, str]:
    """Return a canonical relative directory and account slug or reject the path."""

    archive_root = settings.archive_root
    try:
        relative = metadata_path.parent.relative_to(archive_root)
    except ValueError as exc:
        raise ArchiveError("Archive metadata is outside the configured archive root") from exc
    parts = relative.parts
    if len(parts) != 5:
        raise ArchiveError("Archive path must have platform/account/YYYY/MM/content layout")
    if any(
        part.startswith(".") or part.casefold().endswith(".tmp") or ".tmp-" in part.casefold()
        for part in parts
    ):
        raise ArchiveError("Temporary and hidden archive paths are not canonical")

    # Rebuild must not follow a symlinked directory or metadata file into a
    # different tree while treating its lexical path as canonical.
    current = archive_root
    for part in parts:
        current /= part
        if current.is_symlink():
            raise ArchiveError("Symlinked archive paths are not canonical")
    if metadata_path.is_symlink():
        raise ArchiveError("Symlinked archive metadata is not canonical")

    platform_part, account_slug, year, month, content_part = parts
    published_at = content.published_at.astimezone(timezone.utc)
    expected_parts = (
        content.platform.value,
        sanitize_component(account_slug, "account"),
        f"{published_at.year:04d}",
        f"{published_at.month:02d}",
        sanitize_component(content.remote_id, "content"),
    )
    if tuple(parts) != expected_parts or platform_part != content.platform.value:
        raise ArchiveError("Archive path does not match its canonical metadata identity")
    return relative, account_slug


def restore_account_ledgers(db: Session, settings: Settings) -> dict[tuple[Platform, str], Account]:
    accounts: dict[tuple[Platform, str], Account] = {}
    ledger_root = settings.archive_root / "_state" / "accounts"
    if not ledger_root.exists():
        return accounts
    for ledger_path in sorted(ledger_root.glob("*/*.json")):
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
            schema_version = int(data.get("schema_version", 0))
            if schema_version not in {1, 2}:
                continue
            platform = Platform(str(data["platform"]))
            slug = str(data["slug"]).strip()
            source_url = str(data["source_url"]).strip()
            if not slug or not source_url:
                continue
            account = db.scalar(
                select(Account).where(Account.platform == platform, Account.slug == slug)
            )
            if account is None:
                account = db.scalar(select(Account).where(Account.source_url == source_url))
            if data.get("deleted") is True:
                if account is not None:
                    db.delete(account)
                    db.flush()
                continue
            if account is None:
                account = Account(platform=platform, slug=slug, source_url=source_url)
                db.add(account)
            account.display_name = str(data.get("display_name") or slug)[:160]
            account.source_url = source_url
            account.enabled = bool(data.get("enabled", True))
            account.interval_minutes = max(5, min(int(data.get("interval_minutes", 60)), 1440))
            account.baseline_established = bool(
                account.baseline_established or data.get("baseline_established", False)
            )
            try:
                ledger_completeness = CompletenessStatus(
                    str(data.get("completeness_status", "unknown"))
                )
            except ValueError:
                ledger_completeness = CompletenessStatus.unknown
            rank = {
                CompletenessStatus.unknown: 0,
                CompletenessStatus.complete: 1,
                CompletenessStatus.pending_retry: 2,
                CompletenessStatus.gap_detected: 3,
            }
            current_completeness = account.completeness_status or CompletenessStatus.unknown
            if rank[ledger_completeness] > rank[current_completeness]:
                account.completeness_status = ledger_completeness
            account.gap_detected_at = _datetime(
                data.get("gap_detected_at"), account.gap_detected_at
            )
            ledger_seen = [
                str(value).strip() for value in data.get("seen_ids", []) if str(value).strip()
            ]
            account.status = AccountStatus.pending if account.enabled else AccountStatus.paused
            account.consecutive_failures = max(0, int(data.get("consecutive_failures", 0)))
            account.last_error = str(data.get("last_error") or "")[:4000] or None
            account.last_polled_at = _datetime(data.get("last_polled_at"), account.last_polled_at)
            account.next_poll_at = _datetime(data.get("next_poll_at"), account.next_poll_at)
            account.ledger_revision = max(
                int(account.ledger_revision or 0), int(data.get("revision") or 0)
            )
            created_at = _datetime(data.get("created_at"))
            if created_at is not None:
                account.created_at = created_at
            db.flush()

            existing_rows = {
                row.remote_id: row
                for row in db.scalars(
                    select(ObservedContent).where(ObservedContent.account_id == account.id)
                )
            }
            terminal_ids = list(
                dict.fromkeys(
                    [
                        *(account.seen_ids or []),
                        *(
                            row.remote_id
                            for row in existing_rows.values()
                            if not row.retry_pending
                        ),
                        *ledger_seen,
                    ]
                )
            )
            account.seen_ids = terminal_ids[-500:]
            for remote_id in terminal_ids:
                row = existing_rows.get(remote_id)
                if row is None:
                    row = ObservedContent(account_id=account.id, remote_id=remote_id)
                    db.add(row)
                    existing_rows[remote_id] = row
                row.retry_pending = False
                row.last_error = None
            for value in data.get("pending_refs", []) if schema_version >= 2 else []:
                if not isinstance(value, dict):
                    continue
                remote_id = str(value.get("remote_id") or "").strip()
                source = str(value.get("source_url") or "").strip()
                if not remote_id or not source or remote_id in terminal_ids:
                    continue
                row = existing_rows.get(remote_id)
                if row is None:
                    row = ObservedContent(account_id=account.id, remote_id=remote_id)
                    db.add(row)
                    existing_rows[remote_id] = row
                row.source_url = source
                row.retry_pending = True
                row.attempt_count = max(
                    int(row.attempt_count or 0), int(value.get("attempt_count") or 0)
                )
                row.last_attempt_at = _datetime(
                    value.get("last_attempt_at"), row.last_attempt_at
                )
                row.last_error = str(value.get("last_error") or "")[:4000] or row.last_error
                if account.completeness_status != CompletenessStatus.gap_detected:
                    account.completeness_status = CompletenessStatus.pending_retry
            accounts[(platform, slug)] = account
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Skipping invalid account recovery ledger %s: %s", ledger_path, exc)
            continue
    db.flush()
    return accounts


def rebuild_index(db: Session, settings: Settings) -> tuple[int, int]:
    """Rebuild content and monitoring continuity from the canonical archive tree."""

    restored_accounts = restore_account_ledgers(db, settings)
    manager = ArchiveManager(settings)
    scanned = 0
    imported = 0
    canonical_identities: set[tuple[Platform, str]] = set()
    for metadata_path in sorted(settings.archive_root.rglob("metadata.json")):
        if "_state" in metadata_path.parts:
            continue
        scanned += 1
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            if data.get("status") != "complete":
                logger.warning("Skipping incomplete archive metadata %s", metadata_path)
                continue
            content = _content_from_metadata(data)
            _require_declared_manifest_types(data)
            platform = content.platform
            remote_id = content.remote_id
            relative, account_slug = _canonical_archive_location(metadata_path, settings, content)
            # Hash, size, path, identity and completeness are re-verified before
            # an on-disk directory is allowed back into the operational index.
            _, verified_metadata = manager._validate_existing_archive(metadata_path.parent, content)
            account = restored_accounts.get((platform, account_slug)) or db.scalar(
                select(Account).where(Account.platform == platform, Account.slug == account_slug)
            )
            if not account:
                account = Account(
                    platform=platform,
                    display_name=data.get("author") or account_slug,
                    slug=account_slug,
                    source_url=f"recovered://{platform.value}/{account_slug}",
                    enabled=False,
                    status=AccountStatus.paused,
                    baseline_established=True,
                    completeness_status=CompletenessStatus.unknown,
                )
                db.add(account)
                db.flush()
            media_count = len(verified_metadata.get("media", []))
            collected_at = _datetime(
                verified_metadata.get("collected_at"), datetime.now(timezone.utc)
            )
            existing = db.scalar(
                select(ContentIndex).where(
                    ContentIndex.platform == platform, ContentIndex.remote_id == remote_id
                )
            )
            if existing is None:
                existing = ContentIndex(platform=platform, remote_id=remote_id)
                db.add(existing)
                imported += 1
            # The on-disk record has just passed full archive validation, so it
            # is safe to repair stale operational fields even for an existing row.
            existing.account_id = account.id
            existing.title = str(verified_metadata.get("title", ""))[:500]
            existing.author = str(verified_metadata.get("author", ""))[:160]
            existing.content_type = str(verified_metadata.get("content_type", "text"))
            existing.source_url = str(verified_metadata.get("source_url", ""))
            existing.published_at = content.published_at
            existing.collected_at = collected_at or datetime.now(timezone.utc)
            existing.archive_path = str(relative)
            existing.summary = str(verified_metadata.get("text", ""))[:500]
            existing.media_count = media_count
            existing.expected_media_count = int(
                verified_metadata.get("expected_media_count", media_count)
            )
            existing.verified_media_count = int(
                verified_metadata.get("verified_media_count", media_count)
            )
            existing.integrity_status = CompletenessStatus.complete
            existing.status = JobStatus.complete
            existing.error = None
            observation = db.scalar(
                select(ObservedContent).where(
                    ObservedContent.account_id == account.id,
                    ObservedContent.remote_id == remote_id,
                )
            )
            if observation is None:
                observation = ObservedContent(
                    account_id=account.id,
                    remote_id=remote_id,
                    source_url=content.source_url,
                )
                db.add(observation)
            observation.retry_pending = False
            observation.last_error = None
            observation.archived_at = collected_at or datetime.now(timezone.utc)
            canonical_identities.add((platform, remote_id))
        except (ArchiveError, OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Skipping invalid archive metadata %s: %s", metadata_path, exc)
            continue
    orphaned_rows = [
        row
        for row in db.scalars(select(ContentIndex).order_by(ContentIndex.id))
        if (row.platform, row.remote_id) not in canonical_identities
    ]
    for row in orphaned_rows:
        db.delete(row)
    if orphaned_rows:
        logger.info(
            "Removed %d content index rows without a canonical archive",
            len(orphaned_rows),
        )
    db.commit()
    return scanned, imported


def reconcile_legacy_content_index(db: Session, settings: Settings) -> int:
    """Repair legacy index rows whose archive already proves better metadata."""

    rows = list(
        db.scalars(
            select(ContentIndex).where(
                or_(
                    ContentIndex.content_type.not_in(("text", "image", "video", "audio")),
                    (
                        ContentIndex.content_type.in_(("image", "video", "audio"))
                        & (ContentIndex.media_count == 0)
                    ),
                    ContentIndex.expected_media_count != ContentIndex.media_count,
                    ContentIndex.verified_media_count != ContentIndex.media_count,
                    ContentIndex.integrity_status != CompletenessStatus.complete,
                )
            )
        )
    )
    changed = 0
    manager = ArchiveManager(settings)
    for row in rows:
        try:
            raw_relative = Path(row.archive_path)
            if raw_relative.is_absolute():
                raise ArchiveError("Index archive path must be relative")
            metadata_path = settings.archive_root / raw_relative / "metadata.json"
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            content = _content_from_metadata(
                data,
                expected_platform=row.platform,
                expected_remote_id=row.remote_id,
            )
            _require_declared_manifest_types(data)
            relative, _ = _canonical_archive_location(metadata_path, settings, content)
            if relative != raw_relative:
                raise ArchiveError("Index archive path is not canonical")
            _, verified_metadata = manager._validate_existing_archive(metadata_path.parent, content)
            media = verified_metadata["media"]
            expected = int(verified_metadata.get("expected_media_count", len(media)))
            verified = int(verified_metadata.get("verified_media_count", len(media)))
            row.title = str(verified_metadata.get("title", row.title))[:500]
            row.author = str(verified_metadata.get("author", row.author))[:160]
            row.content_type = str(verified_metadata.get("content_type", row.content_type))
            row.source_url = str(verified_metadata.get("source_url", row.source_url))
            row.published_at = content.published_at
            row.collected_at = _datetime(
                verified_metadata.get("collected_at"), row.collected_at
            ) or row.collected_at
            row.archive_path = str(relative)
            row.summary = str(verified_metadata.get("text", row.summary))[:500]
            row.media_count = len(media)
            row.expected_media_count = expected
            row.verified_media_count = verified
            row.integrity_status = CompletenessStatus.complete
            row.error = None
            changed += 1
        except (ArchiveError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Legacy index archive validation failed for row %s: %s", row.id, exc)
            if (
                row.integrity_status != CompletenessStatus.unknown
                or row.verified_media_count != 0
            ):
                row.integrity_status = CompletenessStatus.unknown
                row.verified_media_count = 0
                changed += 1
    if changed:
        db.commit()
    return changed
