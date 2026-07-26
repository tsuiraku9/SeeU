from __future__ import annotations

import asyncio
import logging
import random
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .adapters import get_adapter
from .adapters.base import (
    AccessBlockedError,
    AdapterError,
    ContentRef,
    NonOriginalContentError,
)
from .account_state import (
    observed_ids_for_account,
    pending_refs_for_account,
    write_account_ledger,
)
from .archive import ArchiveManager, InsufficientStorageError
from .config import Settings
from .database import SessionLocal
from .models import (
    Account,
    AccountStatus,
    CompletenessStatus,
    ContentIndex,
    CrawlRun,
    JobStatus,
    ObservedContent,
    utcnow,
)
from .provider import HttpProvider, ProviderError, ProviderExecutionError, ProviderUnavailableError


logger = logging.getLogger(__name__)
POLL_CANCELLED_ERROR = "Polling cancelled before completion"


def safe_error(exc: Exception) -> str:
    """Keep diagnostics while removing signed/query-string credentials from URLs."""
    return re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[query-redacted]", str(exc))[:4000]


def provider_failure_details(exc: ProviderError) -> dict[str, object]:
    """Persist actionable provider metadata without leaking signed URL queries."""
    return {
        "code": exc.code,
        "phase": exc.phase,
        "retryable": exc.retryable,
        "message": safe_error(exc),
    }


def ref_identities(ref: ContentRef) -> tuple[str, ...]:
    return tuple(dict.fromkeys((ref.remote_id, *ref.aliases)))


class CollectorService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.archive = ArchiveManager(settings)
        self.provider = HttpProvider(settings)
        self._locks: dict[int, asyncio.Lock] = {}
        self._poll_slots = asyncio.Semaphore(settings.provider_poll_concurrency)
        self._platform_limits = {platform: asyncio.Semaphore(1) for platform in get_platforms()}

    @asynccontextmanager
    async def provider_slot(self):
        """Share the browser budget with scheduled polls and manual tests."""
        async with self._poll_slots:
            yield

    def _schedule_next(self, account: Account, failed: bool = False) -> None:
        if failed:
            minutes = min(account.interval_minutes * (2 ** min(account.consecutive_failures, 4)), 24 * 60)
        else:
            minutes = account.interval_minutes + random.randint(0, self.settings.poll_jitter_minutes)
        account.next_poll_at = utcnow() + timedelta(minutes=minutes)

    def _persist_cancelled_poll(self, account_id: int, run_id: int) -> None:
        """Make cancellation terminal even while a poll is waiting for its platform slot."""

        with SessionLocal() as db:
            account = db.get(Account, account_id)
            run = db.get(CrawlRun, run_id)
            if not account or not run or run.status != JobStatus.running:
                return
            account.status = AccountStatus.pending if account.enabled else AccountStatus.paused
            if account.enabled:
                account.next_poll_at = utcnow()
            account.last_error = POLL_CANCELLED_ERROR
            run.status = JobStatus.failed
            run.error = POLL_CANCELLED_ERROR
            run.finished_at = utcnow()
            details = dict(run.details or {})
            details["cancelled"] = True
            run.details = details
            db.flush()
            write_account_ledger(
                self.settings,
                account,
                observed_ids_for_account(db, account),
                pending_refs_for_account(db, account),
            )
            db.commit()

    async def poll_account(self, account_id: int) -> CrawlRun:
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Account is already being polled")
        async with lock:
            with SessionLocal() as db:
                account = db.get(Account, account_id)
                if not account:
                    raise LookupError("Account not found")
                run = CrawlRun(account_id=account.id, status=JobStatus.running)
                db.add(run)
                account.status = AccountStatus.polling
                db.commit()
                run_id = run.id
                platform = account.platform
            try:
                async with self.provider_slot():
                    async with self._platform_limits[platform]:
                        await self._execute(account_id, run_id)
            except asyncio.CancelledError:
                self._persist_cancelled_poll(account_id, run_id)
                raise
            with SessionLocal() as db:
                return db.get(CrawlRun, run_id)  # type: ignore[return-value]

    async def _execute(self, account_id: int, run_id: int) -> None:
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            run = db.get(CrawlRun, run_id)
            assert account and run
            adapter = get_adapter(account.platform, self.settings)
            source_url = account.source_url
            baseline_established = account.baseline_established
            seen_ids = observed_ids_for_account(db, account)
            seen_set = set(seen_ids)
            pending_refs = [
                ContentRef(value["remote_id"], value["source_url"])
                for value in pending_refs_for_account(db, account)
            ]
            pending_ids = {ref.remote_id for ref in pending_refs}
            account_slug = account.slug
        try:
            used_fallback = False
            fallback_attempted = False
            primary_provider_failure: dict[str, object] | None = None
            try:
                refs = await self.provider.discover(
                    adapter.platform, source_url, self.settings.provider_discovery_limit
                )
            except ProviderError as exc:
                primary_provider_failure = provider_failure_details(exc)
                if (
                    not isinstance(
                        exc,
                        (ProviderUnavailableError, ProviderExecutionError),
                    )
                    or adapter.platform.value not in {"bilibili", "weibo"}
                ):
                    raise
                fallback_attempted = True
                used_fallback = True
                refs = await adapter.fetch_latest(source_url)
            active_discovery_limit = 20 if used_fallback else self.settings.provider_discovery_limit
            remote_ids = [ref.remote_id for ref in refs]
            if not baseline_established:
                seed_ref = refs[0] if refs else None
                baseline_ids = remote_ids[1:] if seed_ref else []
                archived = 0
                errors: list[str] = []
                completed_ids: list[str] = []
                archived_observation_ids: set[str] = set()
                failures: dict[str, tuple[ContentRef, str]] = {}
                if seed_ref:
                    try:
                        if await self._archive_ref(account_id, account_slug, adapter, seed_ref):
                            archived = 1
                        completed_ids.append(seed_ref.remote_id)
                        archived_observation_ids.add(seed_ref.remote_id)
                    except NonOriginalContentError:
                        completed_ids.append(seed_ref.remote_id)
                    except Exception as exc:
                        # Establish the older baseline, but leave the newest item
                        # in the durable pending queue so bounded discovery can
                        # never evict it before a later retry.
                        diagnostic = safe_error(exc)
                        errors.append(f"{seed_ref.remote_id}: {diagnostic}")
                        failures[seed_ref.remote_id] = (seed_ref, diagnostic)
                with SessionLocal() as db:
                    account = db.get(Account, account_id)
                    run = db.get(CrawlRun, run_id)
                    assert account and run
                    account.baseline_established = True
                    account.seen_ids = list(dict.fromkeys(completed_ids + baseline_ids + seen_ids))[:500]
                    self._record_observations(
                        db,
                        account.id,
                        refs,
                        archived_ids=archived_observation_ids,
                        observed_ids=set(completed_ids + baseline_ids),
                        failures=failures,
                    )
                    account.completeness_status = (
                        CompletenessStatus.pending_retry
                        if failures
                        else CompletenessStatus.complete
                    )
                    account.status = AccountStatus.error if errors else AccountStatus.healthy
                    account.consecutive_failures = account.consecutive_failures + 1 if errors else 0
                    account.last_error = "\n".join(errors)[:4000] if errors else None
                    account.last_polled_at = utcnow()
                    self._schedule_next(account, failed=bool(errors))
                    run.status = JobStatus.failed if errors else JobStatus.baseline
                    run.discovered_count = len(refs)
                    run.archived_count = archived
                    run.error = account.last_error
                    run.finished_at = utcnow()
                    run.details = {
                        "baseline_ids": baseline_ids,
                        "seed_content_id": seed_ref.remote_id if seed_ref else None,
                        "seed_archived": bool(
                            seed_ref and seed_ref.remote_id in archived_observation_ids
                        ),
                        "provider_path": "fallback" if used_fallback else "external_http",
                    }
                    if primary_provider_failure is not None:
                        run.details["primary_provider_failure"] = primary_provider_failure
                    db.flush()
                    write_account_ledger(
                        self.settings,
                        account,
                        observed_ids_for_account(db, account),
                        pending_refs_for_account(db, account),
                    )
                    db.commit()
                return
            current_refs = {
                identity: ref
                for ref in refs
                for identity in ref_identities(ref)
            }
            retry_refs = [current_refs.get(ref.remote_id, ref) for ref in pending_refs]
            fresh_refs = [
                ref
                for ref in refs
                if not set(ref_identities(ref)) & (seen_set | pending_ids)
            ]
            work_refs = [*retry_refs, *reversed(fresh_refs)]
            if used_fallback:
                window_truncated = len(refs) >= active_discovery_limit
            else:
                window_truncated = bool(refs and refs[0].window_truncated) or len(
                    refs
                ) >= active_discovery_limit
            gap_detected = window_truncated and not any(
                set(ref_identities(ref)) & seen_set and not ref.pinned for ref in refs
            )
            archived = 0
            errors: list[str] = []
            completed_ids: list[str] = []
            archived_observation_ids: set[str] = set()
            failures: dict[str, tuple[ContentRef, str]] = {}
            for ref in work_refs:
                try:
                    if await self._archive_ref(account_id, account_slug, adapter, ref):
                        archived += 1
                    completed_ids.append(ref.remote_id)
                    archived_observation_ids.add(ref.remote_id)
                except NonOriginalContentError:
                    completed_ids.append(ref.remote_id)
                except Exception as exc:  # an individual post must not stop the account
                    diagnostic = safe_error(exc)
                    errors.append(f"{ref.remote_id}: {diagnostic}")
                    failures[ref.remote_id] = (ref, diagnostic)
            with SessionLocal() as db:
                account = db.get(Account, account_id)
                run = db.get(CrawlRun, run_id)
                assert account and run
                account.seen_ids = list(dict.fromkeys(completed_ids + seen_ids))[:500]
                self._record_observations(
                    db,
                    account.id,
                    work_refs,
                    archived_ids=archived_observation_ids,
                    observed_ids=set(completed_ids),
                    failures=failures,
                )
                db.flush()
                has_pending = bool(
                    db.scalar(
                        select(func.count(ObservedContent.id)).where(
                            ObservedContent.account_id == account.id,
                            ObservedContent.retry_pending.is_(True),
                        )
                    )
                )
                if gap_detected or account.completeness_status == CompletenessStatus.gap_detected:
                    account.completeness_status = CompletenessStatus.gap_detected
                    account.gap_detected_at = account.gap_detected_at or utcnow()
                elif has_pending:
                    account.completeness_status = CompletenessStatus.pending_retry
                else:
                    account.completeness_status = CompletenessStatus.complete
                account.status = AccountStatus.error if errors else AccountStatus.healthy
                account.consecutive_failures = account.consecutive_failures + 1 if errors else 0
                account.last_error = "\n".join(errors)[:4000] if errors else None
                account.last_polled_at = utcnow()
                self._schedule_next(account, failed=bool(errors))
                run.status = JobStatus.failed if errors else JobStatus.complete
                run.discovered_count = len(fresh_refs)
                run.archived_count = archived
                run.error = account.last_error
                run.finished_at = utcnow()
                run.details = {
                    "returned_count": len(refs),
                    "new_count": len(fresh_refs),
                    "pending_retried": len(retry_refs),
                    "pending_remaining": has_pending,
                    "discovery_limit": active_discovery_limit,
                    "window_truncated": window_truncated,
                    "provider_path": "fallback" if used_fallback else "external_http",
                    "gap_detected": gap_detected,
                }
                if primary_provider_failure is not None:
                    run.details["primary_provider_failure"] = primary_provider_failure
                write_account_ledger(
                    self.settings,
                    account,
                    observed_ids_for_account(db, account),
                    pending_refs_for_account(db, account),
                )
                db.commit()
        except asyncio.CancelledError:
            # Graceful shutdown cannot resume an in-memory crawl. Persist a
            # terminal run and leave the account immediately retryable without
            # weakening its completeness or pending-reference state.
            self._persist_cancelled_poll(account_id, run_id)
            raise
        except Exception as exc:
            with SessionLocal() as db:
                account = db.get(Account, account_id)
                run = db.get(CrawlRun, run_id)
                assert account and run
                account.consecutive_failures += 1
                account.status = AccountStatus.blocked if isinstance(exc, AccessBlockedError) else AccountStatus.error
                fallback_error = safe_error(exc)
                if primary_provider_failure is not None and fallback_attempted:
                    primary_message = str(
                        primary_provider_failure.get("message")
                        or "external provider failed"
                    )
                    account.last_error = (
                        f"External provider discovery failed: {primary_message}; "
                        f"fallback discovery failed: {fallback_error}"
                    )[:4000]
                    run.details = {
                        "provider_path": "fallback",
                        "primary_provider_failure": primary_provider_failure,
                        "fallback_failure": {
                            "type": type(exc).__name__,
                            "message": fallback_error,
                        },
                    }
                elif primary_provider_failure is not None:
                    account.last_error = fallback_error
                    run.details = {
                        "provider_path": "external_http",
                        "primary_provider_failure": primary_provider_failure,
                    }
                else:
                    account.last_error = fallback_error
                account.last_polled_at = utcnow()
                self._schedule_next(account, failed=True)
                run.status = JobStatus.failed
                run.error = account.last_error
                run.finished_at = utcnow()
                db.flush()
                write_account_ledger(
                    self.settings,
                    account,
                    observed_ids_for_account(db, account),
                    pending_refs_for_account(db, account),
                )
                db.commit()

    @staticmethod
    def _record_observations(
        db: Session,
        account_id: int,
        refs: list[ContentRef],
        *,
        archived_ids: set[str],
        observed_ids: set[str],
        failures: dict[str, tuple[ContentRef, str]],
    ) -> None:
        refs_by_id = {ref.remote_id: ref for ref in refs}
        refs_by_id.update(
            {remote_id: ref for remote_id, (ref, _diagnostic) in failures.items()}
        )
        relevant_ids = observed_ids | set(failures)
        if not relevant_ids:
            return
        identity_ids = {
            identity
            for remote_id in relevant_ids
            for identity in ref_identities(
                refs_by_id.get(remote_id, ContentRef(remote_id, ""))
            )
        }
        ref_urls = {ref.remote_id: ref.source_url for ref in refs}
        existing = {
            row.remote_id: row
            for row in db.scalars(
                select(ObservedContent).where(
                    ObservedContent.account_id == account_id,
                    ObservedContent.remote_id.in_(identity_ids),
                )
            )
        }
        now = utcnow()
        for remote_id in observed_ids:
            row = existing.get(remote_id)
            if row is None:
                row = ObservedContent(
                    account_id=account_id,
                    remote_id=remote_id,
                    source_url=ref_urls.get(remote_id, ""),
                )
                db.add(row)
                existing[remote_id] = row
            row.source_url = ref_urls.get(remote_id, row.source_url)
            row.retry_pending = False
            row.last_error = None
            if remote_id in archived_ids:
                row.archived_at = row.archived_at or now
            ref = refs_by_id.get(remote_id)
            for alias in ref.aliases if ref else ():
                alias_row = existing.get(alias)
                if alias_row is not None:
                    alias_row.retry_pending = False
                    alias_row.last_error = None
        for remote_id, (ref, diagnostic) in failures.items():
            if remote_id in observed_ids:
                continue
            row = existing.get(remote_id)
            if row is None:
                row = ObservedContent(account_id=account_id, remote_id=remote_id)
                db.add(row)
                existing[remote_id] = row
            row.source_url = ref.source_url
            row.retry_pending = True
            row.attempt_count = int(row.attempt_count or 0) + 1
            row.last_attempt_at = now
            row.last_error = diagnostic[:4000]
            for alias in ref.aliases:
                alias_row = existing.get(alias)
                if alias_row is not None:
                    alias_row.retry_pending = False
                    alias_row.last_error = None

    async def _archive_ref(self, account_id: int, account_slug: str, adapter, ref: ContentRef) -> bool:
        with SessionLocal() as db:
            existing = db.scalar(
                select(ContentIndex).where(
                    ContentIndex.platform == adapter.platform,
                    ContentIndex.remote_id.in_(ref_identities(ref)),
                )
            )
            if existing:
                return False
        staged = None
        try:
            self.archive._assert_storage()
            staged = await self.provider.stage(adapter.platform, ref)
            from .adapters.base import NormalizedContent

            if staged.platform != adapter.platform or staged.remote_id != ref.remote_id:
                raise ValueError("Provider staged content identity does not match the discovery reference")
            if (
                not staged.complete
                or staged.downloaded_media_count != len(staged.media)
                or staged.expected_media_count != staged.downloaded_media_count
            ):
                raise ProviderExecutionError(
                    "Provider media is incomplete: "
                    f"expected {staged.expected_media_count}, "
                    f"downloaded {staged.downloaded_media_count}"
                )

            content = NormalizedContent(
                platform=staged.platform, remote_id=staged.remote_id, source_url=staged.source_url,
                title=staged.title, author=staged.author, text=staged.text,
                published_at=staged.published_at, content_type=staged.content_type,
            )
            archive_path, metadata = self.archive.archive_from_files(
                content,
                account_slug,
                staged.local_root,
                staged.media,
                expected_media_count=staged.expected_media_count,
                provider_complete=staged.complete,
            )
        except (ProviderUnavailableError, ProviderExecutionError):
            if adapter.platform.value not in {"bilibili", "weibo"}:
                raise
            content = await adapter.fetch_detail(ref)
            archive_path, metadata = await self.archive.archive(content, account_slug)
        finally:
            if staged:
                await self.provider.cleanup(staged)
        with SessionLocal() as db:
            existing = db.scalar(
                select(ContentIndex).where(
                    ContentIndex.platform == content.platform, ContentIndex.remote_id == content.remote_id
                )
            )
            if existing:
                return False
            db.add(
                ContentIndex(
                    account_id=account_id,
                    platform=content.platform,
                    remote_id=content.remote_id,
                    title=content.title[:500],
                    author=content.author[:160],
                    content_type=str(metadata.get("content_type") or content.content_type),
                    source_url=content.source_url,
                    published_at=content.published_at,
                    collected_at=datetime.fromisoformat(metadata["collected_at"]),
                    archive_path=str(archive_path.relative_to(self.settings.archive_root.resolve())),
                    summary=content.text[:500],
                    media_count=len(metadata.get("media", [])),
                    expected_media_count=int(
                        metadata.get("expected_media_count", len(metadata.get("media", [])))
                    ),
                    verified_media_count=int(
                        metadata.get("verified_media_count", len(metadata.get("media", [])))
                    ),
                    integrity_status=CompletenessStatus.complete,
                    status=JobStatus.complete,
                )
            )
            db.commit()
        return True

    async def poll_due_accounts(self) -> None:
        now = utcnow()
        with SessionLocal() as db:
            ids = list(
                db.scalars(
                    select(Account.id).where(
                        Account.enabled.is_(True),
                        (Account.next_poll_at.is_(None)) | (Account.next_poll_at <= now),
                    )
                )
            )
        async def poll_one(account_id: int) -> None:
            try:
                await self.poll_account(account_id)
            except (LookupError, RuntimeError):
                return
            except Exception as exc:
                logger.error("Scheduled poll for account %s failed: %s", account_id, safe_error(exc))

        await asyncio.gather(*(poll_one(account_id) for account_id in ids))


def get_platforms():
    from .models import Platform

    return list(Platform)
