import io
import json
import logging
import zipfile
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import FormData
from starlette.requests import Request as StarletteRequest

import app.main as main_module
from app.adapters.base import ContentRef
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.main import app, provider
from app.models import Account, ContentIndex, CrawlRun, JobStatus, Platform


def login(client: TestClient):
    return client.post(
        "/api/auth/login",
        json={"token": get_settings().webui_login_token},
    )


def text_archive_manifest(content_id: str = "import-security-test") -> dict:
    return {
        "schema_version": 2,
        "platform": "bilibili",
        "content_id": content_id,
        "source_url": f"https://www.bilibili.com/video/{content_id}",
        "published_at": "2026-07-15T00:00:00+00:00",
        "title": "Imported text",
        "author": "Import test",
        "text": "body",
        "content_type": "text",
        "status": "complete",
        "integrity_status": "complete",
        "expected_media_count": 0,
        "verified_media_count": 0,
        "media": [],
    }


def manifest_zip(manifest: object) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
    return payload.getvalue()


def text_archive_zip(content_id: str = "import-security-test") -> bytes:
    return manifest_zip(text_archive_manifest(content_id))


def test_health_reports_database_failure():
    class BrokenSession:
        def execute(self, _statement):
            raise SQLAlchemyError("database probe failed")

    def broken_database():
        yield BrokenSession()

    app.dependency_overrides[get_db] = broken_database
    try:
        with TestClient(app) as client:
            response = client.get("/api/health")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


def test_login_csrf_and_authenticated_api():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["service"] == "我会一直看着你"
        assert client.get("/api/accounts").status_code == 401
        response = login(client)
        assert response.status_code == 200
        csrf = response.json()["csrf_token"]
        no_csrf = client.post(
            "/api/accounts",
            json={"platform": "bilibili", "source_url": "https://space.bilibili.com/123", "interval_minutes": 60},
        )
        assert no_csrf.status_code == 403
        created = client.post(
            "/api/accounts",
            headers={"X-CSRF-Token": csrf},
            json={"platform": "bilibili", "source_url": "https://space.bilibili.com/123", "interval_minutes": 60},
        )
        assert created.status_code == 201
        assert client.get("/api/accounts").json()[0]["platform"] == "bilibili"


def test_system_settings_exposes_tuning_without_secrets():
    with TestClient(app) as client:
        assert login(client).status_code == 200
        response = client.get("/api/system-settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["database_journal_mode"] == "wal"
    assert payload["scheduler_batch_size"] >= 1
    assert payload["archive_size_cache_seconds"] >= 5
    assert "provider_api_token" not in payload
    assert "webui_login_token" not in payload


def test_unicode_login_token_uses_utf8_constant_time_comparison(monkeypatch):
    settings = get_settings()
    unicode_token = "本地登录令牌" * 5
    monkeypatch.setattr(settings, "webui_login_token", unicode_token)

    with TestClient(app) as client:
        accepted = client.post("/api/auth/login", json={"token": unicode_token})
        rejected = client.post("/api/auth/login", json={"token": "另一个可打印令牌" * 5})

    assert accepted.status_code == 200
    assert rejected.status_code == 401


def test_unconfigured_login_token_is_written_to_state_file_without_log_exposure(
    caplog, monkeypatch, tmp_path
):
    settings = get_settings()
    monkeypatch.setattr(settings, "_webui_login_token_configured", False)
    monkeypatch.setattr(settings, "webui_login_token", "")
    monkeypatch.setattr(settings, "database_path", tmp_path / "state" / "app.db")

    with caplog.at_level(logging.WARNING, logger="app.main"):
        with TestClient(app) as client:
            token_path = settings.generated_webui_token_path
            generated_token = token_path.read_text(encoding="utf-8").strip()
            response = client.post("/api/auth/login", json={"token": generated_token})

    assert response.status_code == 200
    messages = [record.getMessage() for record in caplog.records if record.name == "app.main"]
    assert any(str(token_path) in message for message in messages)
    token_was_logged = any(generated_token in message for message in messages)
    assert token_was_logged is False


def test_configured_login_token_removes_stale_generated_token_file(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_path", tmp_path / "state" / "app.db")
    stale_path = settings.generated_webui_token_path
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text("stale-generated-token\n", encoding="utf-8")

    with TestClient(app):
        assert not stale_path.exists()


def test_import_authentication_and_csrf_run_before_multipart_parsing(monkeypatch):
    parse_calls = 0

    async def unexpected_form_parse(*_args, **_kwargs):
        nonlocal parse_calls
        parse_calls += 1
        raise AssertionError("multipart body was parsed before authorization")

    monkeypatch.setattr(StarletteRequest, "form", unexpected_form_parse)
    with TestClient(app) as client:
        unauthenticated = client.post(
            "/api/imports",
            files={"file": ("archive.zip", b"not-read", "application/zip")},
        )
        login_response = login(client)
        csrf_failure = client.post(
            "/api/imports",
            files={"file": ("archive.zip", b"not-read", "application/zip")},
        )

    assert unauthenticated.status_code == 401
    assert login_response.status_code == 200
    assert csrf_failure.status_code == 403
    assert parse_calls == 0


def test_import_rejects_declared_oversize_before_multipart_parsing(monkeypatch):
    settings = get_settings()
    parse_calls = 0

    async def unexpected_form_parse(*_args, **_kwargs):
        nonlocal parse_calls
        parse_calls += 1
        raise AssertionError("oversize multipart body was parsed")

    monkeypatch.setattr(StarletteRequest, "form", unexpected_form_parse)
    with TestClient(app) as client:
        auth = login(client).json()
        response = client.post(
            "/api/imports",
            headers={
                "X-CSRF-Token": auth["csrf_token"],
                "Content-Length": str(
                    settings.import_max_bytes + main_module.IMPORT_MULTIPART_OVERHEAD_BYTES + 1
                ),
            },
            files={"file": ("archive.zip", b"not-read", "application/zip")},
        )

    assert response.status_code == 413
    assert parse_calls == 0


def test_import_stream_limit_covers_chunked_requests_without_content_length(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "import_max_bytes", 1024)
    monkeypatch.setattr(main_module, "IMPORT_MULTIPART_OVERHEAD_BYTES", 128)
    boundary = "import-stream-limit"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="archive.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode() + b"x" * 2048 + f"\r\n--{boundary}--\r\n".encode()

    with TestClient(app) as client:
        auth = login(client).json()
        response = client.post(
            "/api/imports",
            headers={
                "X-CSRF-Token": auth["csrf_token"],
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            content=(body[offset : offset + 256] for offset in range(0, len(body), 256)),
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Import request exceeds configured byte limit"


def test_import_rejects_file_larger_than_configured_limit(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "import_max_bytes", 1024)

    with TestClient(app) as client:
        auth = login(client).json()
        response = client.post(
            "/api/imports",
            headers={"X-CSRF-Token": auth["csrf_token"]},
            files={"file": ("archive.zip", b"x" * 1025, "application/zip")},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Import exceeds configured byte limit"


def test_import_closes_form_spool_on_validation_failure(monkeypatch):
    close_calls = 0
    original_close = FormData.close

    async def tracking_close(self):
        nonlocal close_calls
        close_calls += 1
        await original_close(self)

    monkeypatch.setattr(FormData, "close", tracking_close)
    with TestClient(app) as client:
        auth = login(client).json()
        response = client.post(
            "/api/imports",
            headers={"X-CSRF-Token": auth["csrf_token"]},
            data={"account_mode": "invalid"},
            files={"file": ("archive.zip", b"not-a-zip", "application/zip")},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "account_mode must be existing or new"
    assert close_calls == 1


def test_valid_text_import_closes_form_and_removes_work_directory(monkeypatch):
    settings = get_settings()
    close_calls = 0
    original_close = FormData.close

    async def tracking_close(self):
        nonlocal close_calls
        close_calls += 1
        await original_close(self)

    monkeypatch.setattr(FormData, "close", tracking_close)
    with TestClient(app) as client:
        auth = login(client).json()
        response = client.post(
            "/api/imports",
            headers={"X-CSRF-Token": auth["csrf_token"]},
            data={"account_mode": "new"},
            files={"file": ("archive.zip", text_archive_zip(), "application/zip")},
        )

    assert response.status_code == 201
    assert response.json()["remote_id"] == "import-security-test"
    assert close_calls == 1
    assert list(settings.provider_staging_root.iterdir()) == []


def test_import_rejects_non_object_manifest_and_non_object_media_records():
    missing_media = text_archive_manifest("missing-media")
    missing_media.pop("media")
    invalid_manifests: list[tuple[object, str]] = [
        ([], "manifest.json must contain a JSON object"),
        (None, "manifest.json must contain a JSON object"),
        ("manifest", "manifest.json must contain a JSON object"),
        (missing_media, "media must be an array of objects"),
        ({**text_archive_manifest("media-object"), "media": {}}, "media must be an array of objects"),
        ({**text_archive_manifest("media-string"), "media": "media/file.jpg"}, "media must be an array of objects"),
        ({**text_archive_manifest("media-null-record"), "media": [None]}, "media must be an array of objects"),
        ({**text_archive_manifest("media-array-record"), "media": [[]]}, "media must be an array of objects"),
        ({**text_archive_manifest("media-number-record"), "media": [1]}, "media must be an array of objects"),
    ]

    with TestClient(app) as client:
        auth = login(client).json()
        for index, (manifest, diagnostic) in enumerate(invalid_manifests):
            response = client.post(
                "/api/imports",
                headers={"X-CSRF-Token": auth["csrf_token"]},
                files={
                    "file": (
                        f"invalid-manifest-{index}.zip",
                        manifest_zip(manifest),
                        "application/zip",
                    )
                },
            )
            assert response.status_code == 422
            assert diagnostic in response.json()["detail"]


def test_import_rejects_conflicting_zip_paths_before_extraction():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr("manifest.json", "not-used")
        bundle.writestr("media", b"file")
        bundle.writestr("media/image.jpg", b"nested-under-a-file")

    with TestClient(app) as client:
        auth = login(client).json()
        response = client.post(
            "/api/imports",
            headers={"X-CSRF-Token": auth["csrf_token"]},
            files={"file": ("archive.zip", payload.getvalue(), "application/zip")},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "ZIP paths conflict"


def test_import_rejects_extreme_compression_ratio_before_extraction():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", "not-used")
        bundle.writestr("media/bomb.bin", b"0" * (11 * 1024 * 1024))

    with TestClient(app) as client:
        auth = login(client).json()
        response = client.post(
            "/api/imports",
            headers={"X-CSRF-Token": auth["csrf_token"]},
            files={"file": ("archive.zip", payload.getvalue(), "application/zip")},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "ZIP compression ratio is unsafe"


def test_authenticated_media_range_and_path_guard():
    settings = get_settings()
    relative = "bilibili/tester/2026/07/post-1"
    media_dir = settings.archive_root / relative / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "clip.mp4").write_bytes(b"0123456789")
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="tester",
            slug="tester",
            source_url="https://space.bilibili.com/123",
        )
        db.add(account); db.flush()
        content = ContentIndex(
            account_id=account.id,
            platform=Platform.bilibili,
            remote_id="post-1",
            title="range",
            author="tester",
            content_type="video",
            source_url="https://www.bilibili.com/video/BV1TEST",
            published_at=datetime.now(timezone.utc),
            archive_path=relative,
        )
        db.add(content); db.commit(); content_id = content.id
    with TestClient(app) as client:
        auth = login(client).json()
        response = client.get(
            f"/api/media/{content_id}/clip.mp4", headers={"Range": "bytes=2-5"}
        )
        assert response.status_code == 206
        assert response.content == b"2345"
        assert response.headers["content-range"] == "bytes 2-5/10"
        traversal = client.get(f"/api/media/{content_id}/..%2Fmetadata.json")
        assert traversal.status_code in {400, 404}


def test_account_test_reports_discovered_published_items(monkeypatch):
    refs = [ContentRef(f"post-{index}", f"https://www.bilibili.com/video/BV{index}") for index in range(6)]

    async def discover(_platform, _source_url):
        return refs

    monkeypatch.setattr(provider, "discover", discover)
    with TestClient(app) as client:
        auth = login(client).json()
        headers = {"X-CSRF-Token": auth["csrf_token"]}
        created = client.post(
            "/api/accounts",
            headers=headers,
            json={"platform": "bilibili", "source_url": "https://space.bilibili.com/123", "interval_minutes": 60},
        ).json()

        response = client.post(f"/api/accounts/{created['id']}/test", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "found": 6,
        "latest_ids": ["post-0", "post-1", "post-2", "post-3", "post-4"],
    }


def test_platform_session_and_qr_responses_are_not_cached(monkeypatch):
    async def sessions():
        return [{"platform": "douyin", "status": "starting"}]

    async def qr(_platform):
        return {
            "platform": "douyin",
            "status": "qr_ready",
            "image_data_url": "data:image/png;base64,cXI=",
        }

    monkeypatch.setattr(provider, "sessions", sessions)
    monkeypatch.setattr(provider, "qr", qr)
    with TestClient(app) as client:
        login(client)

        session_response = client.get("/api/platform-sessions")
        qr_response = client.get("/api/platform-sessions/douyin/qr")

    assert session_response.status_code == 200
    assert session_response.headers["cache-control"] == "no-store, max-age=0"
    assert qr_response.status_code == 200
    assert qr_response.headers["cache-control"] == "no-store, max-age=0"
    assert qr_response.json()["image_data_url"] == "data:image/png;base64,cXI="


def test_token_rotation_revokes_existing_sessions_and_legacy_password_api_is_gone(monkeypatch):
    settings = get_settings()
    with TestClient(app) as client:
        auth = login(client)
        assert auth.status_code == 200
        assert auth.headers["x-content-type-options"] == "nosniff"
        assert auth.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in auth.headers["content-security-policy"]

        monkeypatch.setattr(
            settings,
            "webui_login_token",
            "rotated-test-webui-login-token-long-enough",
        )
        assert client.get("/api/accounts").status_code == 401
        assert client.post("/api/auth/password", json={}).status_code == 404
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "legacy-password"},
        ).status_code == 422


def test_content_and_run_lists_return_stable_page_metadata():
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="paged",
            slug="paged",
            source_url="https://space.bilibili.com/9988",
        )
        db.add(account)
        db.flush()
        for index in range(3):
            db.add(
                ContentIndex(
                    account_id=account.id,
                    platform=Platform.bilibili,
                    remote_id=f"paged-{index}",
                    title=f"item {index}",
                    author="paged",
                    content_type="text",
                    source_url=f"https://www.bilibili.com/video/BV{index}",
                    published_at=datetime(2026, 7, 10 + index, tzinfo=timezone.utc),
                    archive_path=f"bilibili/paged/2026/07/paged-{index}",
                )
            )
            db.add(CrawlRun(account_id=account.id, status=JobStatus.complete))
        db.commit()

    with TestClient(app) as client:
        login(client)
        contents = client.get("/api/contents?offset=1&limit=1").json()
        runs = client.get("/api/runs?offset=0&limit=2").json()

    assert contents["total"] == 3
    assert contents["offset"] == 1
    assert contents["limit"] == 1
    assert contents["has_more"] is True
    assert len(contents["items"]) == 1
    assert runs["total"] == 3
    assert runs["has_more"] is True
    assert len(runs["items"]) == 2
