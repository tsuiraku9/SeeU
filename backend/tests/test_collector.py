import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import app.collector as collector_module
from app.adapters.base import ContentRef, NonOriginalContentError, NormalizedContent
from app.collector import CollectorService, safe_error
from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    Account,
    AccountStatus,
    CompletenessStatus,
    ContentIndex,
    JobStatus,
    ObservedContent,
    Platform,
)
from app.provider import ProviderExecutionError
from app.provider import LoginRequiredError


class FakeAdapter:
    platform = Platform.bilibili

    def __init__(self):
        self.refs = [
            ContentRef("history-newest", "https://www.bilibili.com/video/BV1NEWEST"),
            ContentRef("history-middle", "https://www.bilibili.com/video/BV1MIDDLE"),
            ContentRef("history-oldest", "https://www.bilibili.com/video/BV1OLDEST"),
        ]

    async def fetch_latest(self, _url):
        return self.refs

    async def fetch_detail(self, ref):
        return NormalizedContent(
            platform=self.platform,
            remote_id=ref.remote_id,
            source_url=ref.source_url,
            title=ref.remote_id,
            author="tester",
            text="new content",
            published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
            content_type="text",
        )


class FailingAdapter(FakeAdapter):
    async def fetch_detail(self, ref):
        raise RuntimeError("temporary media failure")


class RecoveringAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.fail = True

    async def fetch_detail(self, ref):
        if self.fail:
            raise RuntimeError("temporary seed failure")
        return await super().fetch_detail(ref)


class NonOriginalAdapter(FakeAdapter):
    async def fetch_detail(self, _ref):
        raise NonOriginalContentError("known repost")


@pytest.mark.asyncio
async def test_first_poll_archives_only_newest_history_and_second_archives_only_new(monkeypatch):
    fake = FakeAdapter()
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="fixture",
            slug="fixture",
            source_url="https://space.bilibili.com/123",
        )
        db.add(account); db.commit(); account_id = account.id
    collector = CollectorService(get_settings())
    first = await collector.poll_account(account_id)
    assert first.status == JobStatus.baseline
    assert first.discovered_count == 3
    assert first.archived_count == 1
    assert first.details == {
        "baseline_ids": ["history-middle", "history-oldest"],
        "seed_content_id": "history-newest",
        "seed_archived": True,
        "provider_path": "fallback",
        "primary_provider_failure": {
            "code": "provider_unavailable",
            "phase": None,
            "retryable": None,
            "message": "External provider is not configured",
        },
    }
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account is not None
        assert account.baseline_established is True
        assert account.seen_ids == ["history-newest", "history-middle", "history-oldest"]
        rows = list(db.scalars(select(ContentIndex)))
        assert [row.remote_id for row in rows] == ["history-newest"]
    fake.refs = [ContentRef("new-2", "https://www.bilibili.com/video/BV1NEW"), *fake.refs]
    second = await collector.poll_account(account_id)
    assert second.status == JobStatus.complete
    assert second.archived_count == 1
    with SessionLocal() as db:
        rows = list(db.scalars(select(ContentIndex)))
        assert [row.remote_id for row in rows] == ["history-newest", "new-2"]


@pytest.mark.asyncio
async def test_failed_first_archive_establishes_older_baseline_and_retries_newest(monkeypatch):
    fake = RecoveringAdapter()
    fake.refs = [
        ContentRef("seed-retry", "https://www.bilibili.com/video/BV1RETRY"),
        ContentRef("history-seen", "https://www.bilibili.com/video/BV1SEEN"),
    ]
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="seed retry fixture",
            slug="seed-retry-fixture",
            source_url="https://space.bilibili.com/456",
        )
        db.add(account); db.commit(); account_id = account.id

    collector = CollectorService(get_settings())
    first = await collector.poll_account(account_id)

    assert first.status == JobStatus.failed
    assert first.archived_count == 0
    assert first.details["seed_archived"] is False
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account is not None
        assert account.baseline_established is True
        assert account.seen_ids == ["history-seen"]
        assert account.completeness_status == CompletenessStatus.pending_retry
        pending = db.scalar(
            select(ObservedContent).where(ObservedContent.remote_id == "seed-retry")
        )
        assert pending is not None and pending.retry_pending is True
        assert db.scalar(select(ContentIndex)) is None

    fake.fail = False
    second = await collector.poll_account(account_id)

    assert second.status == JobStatus.complete
    assert second.archived_count == 1
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account is not None
        assert account.seen_ids == ["seed-retry", "history-seen"]
        assert account.completeness_status == CompletenessStatus.complete
        pending = db.scalar(
            select(ObservedContent).where(ObservedContent.remote_id == "seed-retry")
        )
        assert pending is not None and pending.retry_pending is False
        row = db.scalar(select(ContentIndex))
        assert row is not None
        assert row.remote_id == "seed-retry"


@pytest.mark.asyncio
async def test_first_poll_with_no_content_establishes_empty_baseline(monkeypatch):
    fake = FakeAdapter()
    fake.refs = []
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="empty fixture",
            slug="empty-fixture",
            source_url="https://space.bilibili.com/654",
        )
        db.add(account); db.commit(); account_id = account.id

    result = await CollectorService(get_settings()).poll_account(account_id)

    assert result.status == JobStatus.baseline
    assert result.discovered_count == 0
    assert result.archived_count == 0
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account is not None
        assert account.baseline_established is True
        assert account.seen_ids == []


@pytest.mark.asyncio
async def test_failed_new_item_remains_eligible_for_retry(monkeypatch):
    fake = FailingAdapter()
    fake.refs = [ContentRef("new-failed", "https://www.bilibili.com/video/BV1FAIL")]
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="fixture",
            slug="retry-fixture",
            source_url="https://space.bilibili.com/987",
            baseline_established=True,
            seen_ids=["old"],
        )
        db.add(account); db.commit(); account_id = account.id
    collector = CollectorService(get_settings())
    result = await collector.poll_account(account_id)
    assert result.status == JobStatus.failed
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account is not None
        assert "new-failed" not in account.seen_ids
        assert account.completeness_status == CompletenessStatus.pending_retry
        pending = db.scalar(
            select(ObservedContent).where(ObservedContent.remote_id == "new-failed")
        )
        assert pending is not None and pending.retry_pending is True


@pytest.mark.asyncio
async def test_known_repost_is_terminal_without_archive_or_retry(monkeypatch):
    fake = NonOriginalAdapter()
    fake.refs = [ContentRef("known-repost", "https://www.bilibili.com/video/BV1REPOST")]
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="known repost",
            slug="known-repost",
            source_url="https://space.bilibili.com/986",
            baseline_established=True,
            seen_ids=["old"],
        )
        db.add(account)
        db.commit()
        account_id = account.id

    result = await CollectorService(get_settings()).poll_account(account_id)

    assert result.status == JobStatus.complete
    assert result.archived_count == 0
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        observation = db.scalar(
            select(ObservedContent).where(ObservedContent.remote_id == "known-repost")
        )
        assert account is not None and "known-repost" in account.seen_ids
        assert observation is not None
        assert observation.retry_pending is False
        assert observation.archived_at is None
        assert db.scalar(select(ContentIndex)) is None


@pytest.mark.asyncio
async def test_pending_item_retries_after_it_leaves_discovery_window(monkeypatch):
    fake = RecoveringAdapter()
    fake.refs = [ContentRef("evicted-failure", "https://www.bilibili.com/video/BV1EVICT")]
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="durable pending",
            slug="durable-pending",
            source_url="https://space.bilibili.com/988",
            baseline_established=True,
            seen_ids=["old"],
        )
        db.add(account)
        db.commit()
        account_id = account.id

    collector = CollectorService(get_settings())
    assert (await collector.poll_account(account_id)).status == JobStatus.failed

    # The failed reference is no longer discoverable, but its URL is retained
    # in ObservedContent and the canonical account ledger.
    fake.refs = []
    fake.fail = False
    retried = await collector.poll_account(account_id)

    assert retried.status == JobStatus.complete
    assert retried.details["pending_retried"] == 1
    with SessionLocal() as db:
        row = db.scalar(
            select(ObservedContent).where(ObservedContent.remote_id == "evicted-failure")
        )
        assert row is not None and row.retry_pending is False and row.archived_at is not None
        assert db.scalar(
            select(ContentIndex).where(ContentIndex.remote_id == "evicted-failure")
        ) is not None


@pytest.mark.asyncio
async def test_pinned_overlap_does_not_hide_saturated_window_gap(monkeypatch):
    fake = FakeAdapter()
    fake.refs = [
        ContentRef("old-pinned", "https://www.bilibili.com/video/BV1PIN", pinned=True),
        *[
            ContentRef(f"new-{index}", f"https://www.bilibili.com/video/BV{index}")
            for index in range(19)
        ],
    ]
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="pinned gap",
            slug="pinned-gap",
            source_url="https://space.bilibili.com/989",
            baseline_established=True,
            seen_ids=["old-pinned"],
        )
        db.add(account)
        db.commit()
        account_id = account.id

    await CollectorService(get_settings()).poll_account(account_id)

    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account is not None
        assert account.completeness_status == CompletenessStatus.gap_detected


@pytest.mark.asyncio
async def test_legacy_provider_alias_is_treated_as_the_same_seen_content(monkeypatch):
    fake = FakeAdapter()
    fake.refs = [
        ContentRef(
            "BV1canonical",
            "https://www.bilibili.com/video/BV1canonical",
            aliases=("170001",),
        )
    ]
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="legacy alias",
            slug="legacy-alias",
            source_url="https://space.bilibili.com/991",
            baseline_established=True,
            seen_ids=["170001"],
        )
        db.add(account)
        db.flush()
        db.add(ObservedContent(account_id=account.id, remote_id="170001"))
        db.commit()
        account_id = account.id

    result = await CollectorService(get_settings()).poll_account(account_id)

    assert result.status == JobStatus.complete
    assert result.archived_count == 0
    with SessionLocal() as db:
        assert db.scalar(select(ContentIndex)) is None
        account = db.get(Account, account_id)
        assert account is not None
        assert account.completeness_status == CompletenessStatus.complete


@pytest.mark.asyncio
async def test_legacy_pending_alias_is_retried_under_canonical_identity(monkeypatch):
    fake = RecoveringAdapter()
    fake.fail = False
    fake.refs = [
        ContentRef(
            "BV1retrycanonical",
            "https://www.bilibili.com/video/BV1retrycanonical",
            aliases=("170002",),
        )
    ]
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="legacy pending alias",
            slug="legacy-pending-alias",
            source_url="https://space.bilibili.com/992",
            baseline_established=True,
            completeness_status=CompletenessStatus.pending_retry,
        )
        db.add(account)
        db.flush()
        db.add(
            ObservedContent(
                account_id=account.id,
                remote_id="170002",
                source_url="https://www.bilibili.com/video/av170002",
                retry_pending=True,
                attempt_count=1,
            )
        )
        db.commit()
        account_id = account.id

    result = await CollectorService(get_settings()).poll_account(account_id)

    assert result.status == JobStatus.complete
    assert result.archived_count == 1
    with SessionLocal() as db:
        rows = {
            row.remote_id: row
            for row in db.scalars(
                select(ObservedContent).where(ObservedContent.account_id == account_id)
            )
        }
        assert rows["170002"].retry_pending is False
        assert rows["BV1retrycanonical"].retry_pending is False
        assert db.scalar(
            select(ContentIndex).where(ContentIndex.remote_id == "BV1retrycanonical")
        ) is not None


@pytest.mark.asyncio
async def test_legacy_index_alias_prevents_duplicate_archive(monkeypatch):
    fake = FakeAdapter()
    fake.refs = [
        ContentRef(
            "BV1alreadyarchived",
            "https://www.bilibili.com/video/BV1alreadyarchived",
            aliases=("170003",),
        )
    ]
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="legacy index alias",
            slug="legacy-index-alias",
            source_url="https://space.bilibili.com/993",
            baseline_established=True,
        )
        db.add(account)
        db.flush()
        db.add(
            ContentIndex(
                account_id=account.id,
                platform=Platform.bilibili,
                remote_id="170003",
                title="legacy",
                author="author",
                content_type="video",
                source_url="https://www.bilibili.com/video/av170003",
                published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                archive_path="bilibili/legacy-index-alias/2026/07/170003",
            )
        )
        db.commit()
        account_id = account.id

    result = await CollectorService(get_settings()).poll_account(account_id)

    assert result.status == JobStatus.complete
    assert result.archived_count == 0
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ContentIndex)))) == 1
        canonical = db.scalar(
            select(ObservedContent).where(
                ObservedContent.remote_id == "BV1alreadyarchived"
            )
        )
        assert canonical is not None and canonical.retry_pending is False


def test_error_diagnostics_redact_url_queries():
    error = RuntimeError("download failed https://cdn.example/video.mp4?token=secret&expires=1")
    assert safe_error(error) == "download failed https://cdn.example/video.mp4?[query-redacted]"


@pytest.mark.asyncio
async def test_fallback_failure_preserves_primary_provider_diagnostics(monkeypatch):
    class BrokenFallback(FakeAdapter):
        async def fetch_latest(self, _url):
            raise RuntimeError(
                "fallback blocked https://api.example/items?access_token=fallback-secret"
            )

    broken = BrokenFallback()
    monkeypatch.setattr(
        collector_module,
        "get_adapter",
        lambda _platform, _settings: broken,
    )
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="combined diagnostics",
            slug="combined-diagnostics",
            source_url="https://space.bilibili.com/995",
        )
        db.add(account)
        db.commit()
        account_id = account.id

    collector = CollectorService(get_settings())

    async def fail_provider(*_args, **_kwargs):
        raise ProviderExecutionError(
            "provider rejected https://cdn.example/media?token=primary-secret",
            code="provider_contract_invalid",
            phase="discovery_contract",
            retryable=False,
        )

    monkeypatch.setattr(collector.provider, "discover", fail_provider)
    result = await collector.poll_account(account_id)

    assert result.status == JobStatus.failed
    assert "primary-secret" not in (result.error or "")
    assert "fallback-secret" not in (result.error or "")
    assert "External provider discovery failed" in (result.error or "")
    assert "fallback discovery failed" in (result.error or "")
    assert result.details == {
        "provider_path": "fallback",
        "primary_provider_failure": {
            "code": "provider_contract_invalid",
            "phase": "discovery_contract",
            "retryable": False,
            "message": "provider rejected https://cdn.example/media?[query-redacted]",
        },
        "fallback_failure": {
            "type": "RuntimeError",
            "message": "fallback blocked https://api.example/items?[query-redacted]",
        },
    }


@pytest.mark.asyncio
async def test_non_fallback_provider_failure_preserves_structured_diagnostics(monkeypatch):
    fake = FakeAdapter()
    fake.platform = Platform.xiaohongshu
    monkeypatch.setattr(
        collector_module,
        "get_adapter",
        lambda _platform, _settings: fake,
    )
    with SessionLocal() as db:
        account = Account(
            platform=Platform.xiaohongshu,
            display_name="provider diagnostics",
            slug="provider-diagnostics",
            source_url="https://www.xiaohongshu.com/user/profile/123",
        )
        db.add(account)
        db.commit()
        account_id = account.id

    collector = CollectorService(get_settings())

    async def require_login(*_args, **_kwargs):
        raise LoginRequiredError(
            "session expired",
            code="login_required",
            phase="session",
            retryable=True,
        )

    monkeypatch.setattr(collector.provider, "discover", require_login)
    result = await collector.poll_account(account_id)

    assert result.status == JobStatus.failed
    assert result.error == "session expired"
    assert result.details == {
        "provider_path": "external_http",
        "primary_provider_failure": {
            "code": "login_required",
            "phase": "session",
            "retryable": True,
            "message": "session expired",
        },
    }


@pytest.mark.asyncio
async def test_default_poll_concurrency_serializes_different_platform_browsers(
    monkeypatch,
):
    class EmptyAdapter(FakeAdapter):
        def __init__(self, platform):
            super().__init__()
            self.platform = platform

    monkeypatch.setattr(
        collector_module,
        "get_adapter",
        lambda platform, _settings: EmptyAdapter(platform),
    )
    with SessionLocal() as db:
        accounts = [
            Account(
                platform=platform,
                display_name=f"serialized {platform.value}",
                slug=f"serialized-{platform.value}",
                source_url=source_url,
            )
            for platform, source_url in (
                (Platform.bilibili, "https://space.bilibili.com/996"),
                (
                    Platform.xiaohongshu,
                    "https://www.xiaohongshu.com/user/profile/996",
                ),
            )
        ]
        db.add_all(accounts)
        db.commit()
        account_ids = [account.id for account in accounts]

    collector = CollectorService(get_settings())
    active = 0
    maximum_active = 0

    async def discover(*_args, **_kwargs):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return []

    monkeypatch.setattr(collector.provider, "discover", discover)
    results = await asyncio.gather(
        *(collector.poll_account(account_id) for account_id in account_ids)
    )

    assert maximum_active == 1
    assert all(result.status == JobStatus.baseline for result in results)


@pytest.mark.asyncio
async def test_cancelled_poll_is_terminal_and_immediately_retryable(monkeypatch):
    started = asyncio.Event()

    class BlockingAdapter(FakeAdapter):
        async def fetch_latest(self, _url):
            started.set()
            await asyncio.Event().wait()

    fake = BlockingAdapter()
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="cancelled poll",
            slug="cancelled-poll",
            source_url="https://space.bilibili.com/990",
            baseline_established=True,
            completeness_status=CompletenessStatus.pending_retry,
        )
        db.add(account)
        db.commit()
        account_id = account.id

    task = asyncio.create_task(CollectorService(get_settings()).poll_account(account_id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with SessionLocal() as db:
        account = db.get(Account, account_id)
        run = db.scalar(select(collector_module.CrawlRun))
        assert account is not None and run is not None
        assert account.status == AccountStatus.pending
        assert account.completeness_status == CompletenessStatus.pending_retry
        assert account.next_poll_at is not None
        assert run.status == JobStatus.failed
        assert run.finished_at is not None
        assert run.details["cancelled"] is True


@pytest.mark.asyncio
async def test_poll_cancelled_while_waiting_for_platform_slot_is_terminal(monkeypatch):
    fake = FakeAdapter()
    monkeypatch.setattr(collector_module, "get_adapter", lambda _platform, _settings: fake)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="cancelled before slot",
            slug="cancelled-before-slot",
            source_url="https://space.bilibili.com/994",
            baseline_established=True,
        )
        db.add(account)
        db.commit()
        account_id = account.id

    collector = CollectorService(get_settings())
    platform_slot = collector._platform_limits[Platform.bilibili]
    await platform_slot.acquire()
    try:
        task = asyncio.create_task(collector.poll_account(account_id))
        for _ in range(20):
            await asyncio.sleep(0)
            with SessionLocal() as db:
                run = db.scalar(select(collector_module.CrawlRun))
                if run is not None:
                    break
        assert run is not None and run.status == JobStatus.running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        platform_slot.release()

    with SessionLocal() as db:
        account = db.get(Account, account_id)
        run = db.scalar(select(collector_module.CrawlRun))
        assert account is not None and run is not None
        assert account.status == AccountStatus.pending
        assert account.next_poll_at is not None
        assert run.status == JobStatus.failed
        assert run.finished_at is not None
        assert run.details["cancelled"] is True
