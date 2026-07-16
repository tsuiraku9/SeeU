import asyncio
import json
from datetime import datetime, timezone

import pytest

import app.archive as archive_module
from app.adapters.base import MediaCandidate, NormalizedContent
from app.archive import ArchiveError, ArchiveManager
from app.config import get_settings
from app.models import Platform


@pytest.mark.asyncio
async def test_atomic_file_archive_without_media():
    settings = get_settings()
    manager = ArchiveManager(settings)
    content = NormalizedContent(
        platform=Platform.weibo,
        remote_id="post-1",
        source_url="https://m.weibo.cn/status/post-1",
        title="测试文案",
        author="作者",
        text="正文",
        published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        content_type="text",
    )
    target, metadata = await manager.archive(content, "author-1")
    assert (target / "content.md").read_text(encoding="utf-8").startswith("# 测试文案")
    assert json.loads((target / "metadata.json").read_text(encoding="utf-8"))["content_id"] == "post-1"
    assert metadata["status"] == "complete"
    assert not list(target.parent.glob(".*.tmp-*"))


@pytest.mark.asyncio
async def test_download_budget_is_cumulative_across_media_files(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "media_max_bytes", 10)
    manager = ArchiveManager(settings)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "01-image.jpg").write_bytes(b"1234")
    received_budget = None

    async def fake_download(_candidate, _media_dir, _index, remaining_bytes):
        nonlocal received_budget
        received_budget = remaining_bytes
        return {
            "kind": "image",
            "local_path": "media/02-image.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1,
            "sha256": "0" * 64,
        }

    monkeypatch.setattr(manager, "_download_http", fake_download)
    await manager._download_candidate(
        MediaCandidate("image", "https://example.com/image.jpg"), media_dir, 2
    )

    assert received_budget == 6

    (media_dir / "full.bin").write_bytes(b"123456")
    with pytest.raises(ArchiveError, match="cumulative"):
        await manager._download_candidate(
            MediaCandidate("image", "https://example.com/another.jpg"), media_dir, 3
        )


@pytest.mark.asyncio
async def test_cancelled_archive_removes_temporary_directory(monkeypatch):
    settings = get_settings()
    manager = ArchiveManager(settings)
    content = NormalizedContent(
        platform=Platform.weibo,
        remote_id="cancelled-post",
        source_url="https://m.weibo.cn/status/cancelled-post",
        title="cancelled",
        author="author",
        text="body",
        published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        content_type="image",
        media=[MediaCandidate("image", "https://example.com/image.jpg")],
    )

    async def blocked_download(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_download_candidate", blocked_download)
    task = asyncio.create_task(manager.archive(content, "author-cancelled"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    target = manager._target_directory(content, "author-cancelled")
    assert not target.exists()
    assert not list(target.parent.glob(".*.tmp-*"))


@pytest.mark.asyncio
async def test_cancelled_ytdlp_download_kills_child_process(tmp_path, monkeypatch):
    settings = get_settings()
    manager = ArchiveManager(settings)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    started = asyncio.Event()

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.pid = None
            self.stopped = asyncio.Event()
            self.killed = False

        async def communicate(self):
            started.set()
            await self.stopped.wait()
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9
            self.stopped.set()

    process = FakeProcess()

    async def create_process(*_args, **_kwargs):
        return process

    async def allow_url(_url):
        return None

    monkeypatch.setattr(archive_module.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(manager, "_assert_public_media_url", allow_url)

    task = asyncio.create_task(
        manager._download_ytdlp(
            MediaCandidate("video", "https://example.com/video"),
            media_dir,
            1,
            settings.media_max_bytes,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True
