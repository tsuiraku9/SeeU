from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest

from app.adapters.base import NormalizedContent
from app.archive import ArchiveError, ArchiveManager
from app.config import get_settings
from app.models import Platform


MP4_PAYLOAD = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


def content() -> NormalizedContent:
    remote_id = f"stage-{uuid.uuid4().hex}"
    return NormalizedContent(
        platform=Platform.douyin,
        remote_id=remote_id,
        source_url=f"https://www.douyin.com/video/{remote_id}",
        title="staged", author="creator", text="body",
        published_at=datetime(2026, 7, 11, tzinfo=timezone.utc), content_type="video",
    )


def record(path, relative="media/video.mp4") -> dict:
    payload = path.read_bytes()
    return {"local_path": relative, "kind": "video", "mime_type": "video/mp4",
            "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def test_staged_archive_validates_and_promotes_atomically(tmp_path):
    root = tmp_path / "job"; media = root / "media"; media.mkdir(parents=True)
    source = media / "video.mp4"; source.write_bytes(MP4_PAYLOAD)
    target, metadata = ArchiveManager(get_settings()).archive_from_files(content(), "creator", root, [record(source)])
    assert (target / "metadata.json").is_file()
    assert (target / "media" / "01-video.mp4").read_bytes() == MP4_PAYLOAD
    assert metadata["media"][0]["sha256"] == hashlib.sha256(MP4_PAYLOAD).hexdigest()
    assert not list(target.parent.glob(".*.tmp-*"))


@pytest.mark.parametrize("relative", ["../escape.mp4", "/absolute.mp4"])
def test_staged_archive_rejects_unsafe_paths(tmp_path, relative):
    root = tmp_path / "job"; root.mkdir(); source = root / "video.mp4"; source.write_bytes(b"x")
    item = record(source, relative)
    with pytest.raises(ArchiveError, match="safe relative path|escapes staging"):
        ArchiveManager(get_settings()).archive_from_files(content(), "creator", root, [item])


def test_staged_archive_rejects_hash_mismatch_without_partial_archive(tmp_path):
    root = tmp_path / "job"; media = root / "media"; media.mkdir(parents=True)
    source = media / "video.mp4"; source.write_bytes(b"x")
    item = record(source); item["sha256"] = "0" * 64
    manager = ArchiveManager(get_settings())
    with pytest.raises(ArchiveError, match="SHA-256"):
        manager.archive_from_files(content(), "creator", root, [item])
    assert not list(get_settings().archive_root.rglob("*.tmp-*"))


def test_staged_archive_rejects_incomplete_provider_manifest(tmp_path):
    root = tmp_path / "job"; media = root / "media"; media.mkdir(parents=True)
    source = media / "video.mp4"; source.write_bytes(MP4_PAYLOAD)
    manager = ArchiveManager(get_settings())
    item = content()
    with pytest.raises(ArchiveError, match="incomplete"):
        manager.archive_from_files(
            item,
            "creator",
            root,
            [record(source)],
            expected_media_count=2,
            provider_complete=False,
        )
    assert not manager._target_directory(item, "creator").exists()


def test_staged_archive_rejects_duplicate_manifest_paths(tmp_path):
    root = tmp_path / "job"; media = root / "media"; media.mkdir(parents=True)
    source = media / "video.mp4"; source.write_bytes(MP4_PAYLOAD)
    manager = ArchiveManager(get_settings())
    item = content()
    duplicate = record(source)
    with pytest.raises(ArchiveError, match="duplicate"):
        manager.archive_from_files(item, "creator", root, [duplicate, duplicate])


def test_non_text_archive_cannot_publish_without_media(tmp_path):
    root = tmp_path / "job"; root.mkdir()
    item = content()
    with pytest.raises(ArchiveError, match="no downloadable media"):
        ArchiveManager(get_settings()).archive_from_files(item, "creator", root, [])
