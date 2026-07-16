from datetime import datetime, timezone

import json
import pytest
import uuid
import hashlib

from sqlalchemy import delete, select

from app.adapters.base import NormalizedContent
from app.archive import ArchiveManager
from app.config import get_settings
from app.database import SessionLocal
from app.account_state import sync_all_account_ledgers, write_account_ledger, write_account_tombstone
from app.models import Account, AccountStatus, CompletenessStatus, ContentIndex, ObservedContent, Platform
from app.rebuild import rebuild_index, reconcile_legacy_content_index, restore_account_ledgers


def _write_text_archive_metadata(path, platform, remote_id, published_at):
    path.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": 2,
        "platform": platform.value,
        "content_id": remote_id,
        "source_url": f"https://example.test/{remote_id}",
        "title": f"title-{remote_id}",
        "author": "test-author",
        "text": "test body",
        "content_type": "text",
        "published_at": published_at.isoformat(),
        "collected_at": published_at.isoformat(),
        "status": "complete",
        "integrity_status": "complete",
        "expected_media_count": 0,
        "verified_media_count": 0,
        "media": [],
    }
    (path / "content.md").write_text("# test\n", encoding="utf-8")
    (path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


@pytest.mark.asyncio
async def test_sqlite_index_rebuilds_from_metadata_files():
    settings = get_settings()
    content = NormalizedContent(
        platform=Platform.xiaohongshu,
        remote_id="rebuild-1",
        source_url="https://www.xiaohongshu.com/explore/rebuild1",
        title="可重建内容",
        author="archive-author",
        text="文件是最终事实来源",
        published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        content_type="text",
    )
    await ArchiveManager(settings).archive(content, "archive-author")
    with SessionLocal() as db:
        scanned, imported = rebuild_index(db, settings)
        row = db.scalar(
            select(ContentIndex).where(ContentIndex.platform == Platform.xiaohongshu, ContentIndex.remote_id == "rebuild-1")
        )
        assert scanned >= 1
        assert imported >= 1
        assert row is not None and row.title == "可重建内容"


@pytest.mark.asyncio
async def test_rebuild_removes_orphan_index_and_preserves_account_ledger_state():
    settings = get_settings()
    token = uuid.uuid4().hex[:10]
    slug = f"orphan-cleanup-{token}"
    valid_id = f"valid-{token}"
    orphan_id = f"orphan-{token}"
    published_at = datetime(2026, 7, 14, tzinfo=timezone.utc)
    content = NormalizedContent(
        platform=Platform.bilibili,
        remote_id=valid_id,
        source_url=f"https://www.bilibili.com/video/BV{token}",
        title="canonical content",
        author=slug,
        text="canonical archive remains indexed",
        published_at=published_at,
        content_type="text",
    )
    archive_path, _ = await ArchiveManager(settings).archive(content, slug)

    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="orphan cleanup",
            slug=slug,
            source_url=f"https://space.bilibili.com/{int(token, 16)}",
            baseline_established=True,
        )
        db.add(account)
        db.flush()
        db.add_all(
            [
                ContentIndex(
                    account_id=account.id,
                    platform=Platform.bilibili,
                    remote_id=valid_id,
                    title="stale valid row",
                    author=slug,
                    content_type="text",
                    source_url=content.source_url,
                    published_at=published_at,
                    archive_path=str(
                        archive_path.relative_to(settings.archive_root.resolve())
                    ),
                ),
                ContentIndex(
                    account_id=account.id,
                    platform=Platform.bilibili,
                    remote_id=orphan_id,
                    title="missing from disk",
                    author=slug,
                    content_type="text",
                    source_url=f"https://www.bilibili.com/video/{orphan_id}",
                    published_at=published_at,
                    archive_path=f"bilibili/{slug}/2026/07/{orphan_id}",
                ),
                ObservedContent(
                    account_id=account.id,
                    remote_id=orphan_id,
                    source_url=f"https://www.bilibili.com/video/{orphan_id}",
                ),
            ]
        )
        db.flush()
        ledger_path = write_account_ledger(settings, account, [valid_id, orphan_id])
        ledger_before = ledger_path.read_bytes()
        db.commit()

        rebuild_index(db, settings)
        db.expire_all()
        preserved_account = db.scalar(select(Account).where(Account.slug == slug))
        valid_row = db.scalar(
            select(ContentIndex).where(ContentIndex.remote_id == valid_id)
        )
        orphan_row = db.scalar(
            select(ContentIndex).where(ContentIndex.remote_id == orphan_id)
        )
        orphan_observation = db.scalar(
            select(ObservedContent).where(
                ObservedContent.account_id == account.id,
                ObservedContent.remote_id == orphan_id,
            )
        )

    assert preserved_account is not None
    assert valid_row is not None and valid_row.title == content.title
    assert orphan_row is None
    assert orphan_observation is not None
    assert ledger_path.read_bytes() == ledger_before


@pytest.mark.asyncio
async def test_rebuild_restores_account_configuration_and_seen_watermark():
    settings = get_settings()
    token = uuid.uuid4().hex[:10]
    slug = f"ledger-{token}"
    source_url = f"https://space.bilibili.com/{int(token, 16)}"
    content = NormalizedContent(
        platform=Platform.bilibili,
        remote_id=f"post-{token}",
        source_url=f"https://www.bilibili.com/video/BV{token}",
        title="账本恢复",
        author=slug,
        text="restore continuity",
        published_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        content_type="text",
    )
    await ArchiveManager(settings).archive(content, slug)
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="恢复账号",
            slug=slug,
            source_url=source_url,
            enabled=True,
            interval_minutes=135,
            baseline_established=True,
            completeness_status=CompletenessStatus.gap_detected,
            status=AccountStatus.healthy,
            seen_ids=[content.remote_id, f"older-{token}"],
        )
        db.add(account)
        db.flush()
        db.add_all(
            [
                ObservedContent(account_id=account.id, remote_id=content.remote_id),
                ObservedContent(account_id=account.id, remote_id=f"older-{token}"),
            ]
        )
        db.flush()
        write_account_ledger(settings, account, [content.remote_id, f"older-{token}"])
        db.commit()

        db.execute(delete(ObservedContent))
        db.execute(delete(ContentIndex))
        db.execute(delete(Account))
        db.commit()

        rebuild_index(db, settings)
        restored = db.scalar(select(Account).where(Account.source_url == source_url))
        assert restored is not None
        assert restored.display_name == "恢复账号"
        assert restored.interval_minutes == 135
        assert restored.baseline_established is True
        assert restored.completeness_status == CompletenessStatus.gap_detected
        observed = set(
            db.scalars(
                select(ObservedContent.remote_id).where(ObservedContent.account_id == restored.id)
            )
        )
        assert {content.remote_id, f"older-{token}"} <= observed


def test_startup_reconciliation_normalizes_legacy_unknown_media_index(tmp_path):
    settings = get_settings()
    token = uuid.uuid4().hex[:10]
    staging = tmp_path / "job"
    media_dir = staging / "media"
    media_dir.mkdir(parents=True)
    media_file = media_dir / "clip.mp4"
    payload = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
    media_file.write_bytes(payload)
    content = NormalizedContent(
        platform=Platform.douyin,
        remote_id=f"legacy-{token}",
        source_url=f"https://www.douyin.com/video/{token}",
        title="legacy",
        author="legacy",
        text="",
        published_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        content_type="unknown",
    )
    archive_path, _ = ArchiveManager(settings).archive_from_files(
        content,
        f"legacy-{token}",
        staging,
        [
            {
                "local_path": "media/clip.mp4",
                "kind": "video",
                "mime_type": "video/mp4",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    )
    with SessionLocal() as db:
        account = Account(
            platform=Platform.douyin,
            display_name="legacy",
            slug=f"legacy-{token}",
            source_url=f"https://www.douyin.com/user/{token}",
        )
        db.add(account)
        db.flush()
        row = ContentIndex(
            account_id=account.id,
            platform=Platform.douyin,
            remote_id=content.remote_id,
            title="legacy",
            author="legacy",
            content_type="unknown",
            source_url=content.source_url,
            published_at=content.published_at,
            archive_path=str(archive_path.relative_to(settings.archive_root.resolve())),
            media_count=1,
            expected_media_count=0,
            verified_media_count=0,
        )
        db.add(row)
        db.commit()

        assert reconcile_legacy_content_index(db, settings) == 1
        db.refresh(row)
        assert row.content_type == "video"
        assert row.expected_media_count == 1
        assert row.verified_media_count == 1
        assert row.integrity_status == CompletenessStatus.complete


@pytest.mark.parametrize("path_kind", ["hidden", "temporary", "noncanonical"])
def test_rebuild_rejects_hidden_temporary_and_noncanonical_paths(path_kind):
    settings = get_settings()
    token = uuid.uuid4().hex[:10]
    remote_id = f"invalid-path-{token}"
    published_at = datetime(2026, 7, 12, tzinfo=timezone.utc)
    if path_kind == "hidden":
        archive_dir = (
            settings.archive_root
            / Platform.bilibili.value
            / f".hidden-{token}"
            / "2026"
            / "07"
            / remote_id
        )
    elif path_kind == "temporary":
        archive_dir = (
            settings.archive_root
            / Platform.bilibili.value
            / f"account-{token}"
            / "2026"
            / "07"
            / f".{remote_id}.tmp-deadbeef"
        )
    else:
        archive_dir = (
            settings.archive_root
            / Platform.bilibili.value
            / f"account-{token}"
            / "2026"
            / "06"
            / remote_id
        )
    _write_text_archive_metadata(
        archive_dir, Platform.bilibili, remote_id, published_at
    )

    with SessionLocal() as db:
        rebuild_index(db, settings)
        row = db.scalar(
            select(ContentIndex).where(
                ContentIndex.platform == Platform.bilibili,
                ContentIndex.remote_id == remote_id,
            )
        )
        assert row is None


@pytest.mark.asyncio
async def test_rebuild_repairs_an_existing_index_from_validated_canonical_metadata():
    settings = get_settings()
    token = uuid.uuid4().hex[:10]
    slug = f"repair-{token}"
    content = NormalizedContent(
        platform=Platform.xiaohongshu,
        remote_id=f"repair-post-{token}",
        source_url=f"https://www.xiaohongshu.com/explore/{token}",
        title="canonical title",
        author="canonical author",
        text="canonical summary",
        published_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        content_type="text",
    )
    archive_path, _ = await ArchiveManager(settings).archive(content, slug)
    canonical_relative = str(archive_path.relative_to(settings.archive_root.resolve()))

    with SessionLocal() as db:
        account = Account(
            platform=content.platform,
            display_name=slug,
            slug=slug,
            source_url=f"https://www.xiaohongshu.com/user/profile/{token}",
        )
        db.add(account)
        db.flush()
        row = ContentIndex(
            account_id=account.id,
            platform=content.platform,
            remote_id=content.remote_id,
            title="stale title",
            author="stale author",
            content_type="unknown",
            source_url="https://invalid.test/stale",
            published_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            archive_path="stale/noncanonical/path",
            summary="stale summary",
            media_count=9,
            expected_media_count=9,
            verified_media_count=0,
            integrity_status=CompletenessStatus.unknown,
        )
        db.add(row)
        db.commit()

        rebuild_index(db, settings)
        db.refresh(row)
        assert row.title == content.title
        assert row.author == content.author
        assert row.source_url == content.source_url
        assert row.published_at.replace(tzinfo=timezone.utc) == content.published_at
        assert row.archive_path == canonical_relative
        assert row.summary == content.text
        assert row.media_count == 0
        assert row.expected_media_count == 0
        assert row.verified_media_count == 0
        assert row.integrity_status == CompletenessStatus.complete


@pytest.mark.parametrize("damage", ["missing", "hash", "mime"])
def test_legacy_reconcile_never_marks_damaged_media_complete(tmp_path, damage):
    settings = get_settings()
    token = uuid.uuid4().hex[:10]
    staging = tmp_path / f"job-{damage}"
    media_dir = staging / "media"
    media_dir.mkdir(parents=True)
    media_file = media_dir / "clip.mp4"
    payload = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
    media_file.write_bytes(payload)
    content = NormalizedContent(
        platform=Platform.douyin,
        remote_id=f"damaged-{damage}-{token}",
        source_url=f"https://www.douyin.com/video/{token}",
        title="damaged legacy",
        author="legacy",
        text="",
        published_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        content_type="unknown",
    )
    archive_path, _ = ArchiveManager(settings).archive_from_files(
        content,
        f"damaged-{token}",
        staging,
        [
            {
                "local_path": "media/clip.mp4",
                "kind": "video",
                "mime_type": "video/mp4",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    )
    metadata_path = archive_path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    archived_media = archive_path / metadata["media"][0]["local_path"]
    if damage == "missing":
        archived_media.unlink()
    elif damage == "hash":
        metadata["media"][0]["sha256"] = "0" * 64
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    else:
        del metadata["media"][0]["mime_type"]
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with SessionLocal() as db:
        account = Account(
            platform=content.platform,
            display_name="legacy",
            slug=f"damaged-{token}",
            source_url=f"https://www.douyin.com/user/{token}",
        )
        db.add(account)
        db.flush()
        row = ContentIndex(
            account_id=account.id,
            platform=content.platform,
            remote_id=content.remote_id,
            title=content.title,
            author=content.author,
            content_type="unknown",
            source_url=content.source_url,
            published_at=content.published_at,
            archive_path=str(archive_path.relative_to(settings.archive_root.resolve())),
            media_count=1,
            expected_media_count=1,
            verified_media_count=1,
            integrity_status=CompletenessStatus.complete,
        )
        db.add(row)
        db.commit()

        assert reconcile_legacy_content_index(db, settings) == 1
        db.refresh(row)
        assert row.integrity_status == CompletenessStatus.unknown
        assert row.verified_media_count == 0


def test_startup_merge_never_shrinks_newer_canonical_ledger():
    settings = get_settings()
    token = uuid.uuid4().hex[:10]
    slug = f"merge-{token}"
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="merge",
            slug=slug,
            source_url=f"https://space.bilibili.com/{int(token, 16)}",
            baseline_established=True,
            seen_ids=["old"],
        )
        db.add(account)
        db.flush()
        write_account_ledger(settings, account, ["old", "ledger-newer"])
        db.commit()

        account.seen_ids = ["old"]
        db.execute(delete(ObservedContent))
        db.commit()
        assert sync_all_account_ledgers(db, settings) == 0

        restored = restore_account_ledgers(db, settings)[(Platform.bilibili, slug)]
        db.commit()
        observed = set(
            db.scalars(
                select(ObservedContent.remote_id).where(
                    ObservedContent.account_id == restored.id,
                    ObservedContent.retry_pending.is_(False),
                )
            )
        )

    assert {"old", "ledger-newer"} <= observed


def test_rebuild_restores_pending_refs_and_honors_tombstones():
    settings = get_settings()
    token = uuid.uuid4().hex[:10]
    pending_slug = f"pending-{token}"
    deleted_slug = f"deleted-{token}"
    with SessionLocal() as db:
        pending_account = Account(
            platform=Platform.weibo,
            display_name="pending",
            slug=pending_slug,
            source_url=f"https://weibo.com/u/{int(token, 16)}",
            baseline_established=True,
        )
        deleted_account = Account(
            platform=Platform.bilibili,
            display_name="deleted",
            slug=deleted_slug,
            source_url=f"https://space.bilibili.com/{int(token, 16) + 1}",
        )
        db.add_all([pending_account, deleted_account])
        db.flush()
        write_account_ledger(
            settings,
            pending_account,
            ["terminal"],
            [
                {
                    "remote_id": "retry-me",
                    "source_url": "https://m.weibo.cn/detail/retry-me",
                    "attempt_count": 3,
                    "last_error": "temporary media failure",
                }
            ],
        )
        write_account_tombstone(settings, deleted_account)
        db.commit()
        db.execute(delete(Account))
        db.commit()

        rebuild_index(db, settings)
        restored = db.scalar(select(Account).where(Account.slug == pending_slug))
        deleted_row = db.scalar(select(Account).where(Account.slug == deleted_slug))
        assert restored is not None
        pending = db.scalar(
            select(ObservedContent).where(
                ObservedContent.account_id == restored.id,
                ObservedContent.remote_id == "retry-me",
            )
        )
        completeness = restored.completeness_status

    assert pending is not None
    assert pending.retry_pending is True
    assert pending.attempt_count == 3
    assert completeness == CompletenessStatus.pending_retry
    assert deleted_row is None
