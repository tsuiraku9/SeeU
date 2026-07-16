from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Account, AccountStatus, ContentIndex, CrawlRun, JobStatus, Platform


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def login(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"token": "test-webui-login-token-long-enough-123456"},
    )
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def create_account(*, next_poll_at: datetime, enabled: bool = True) -> int:
    with SessionLocal() as db:
        account = Account(
            platform=Platform.bilibili,
            display_name="原名称",
            slug="account-update",
            source_url="https://space.bilibili.com/10001",
            enabled=enabled,
            interval_minutes=60,
            status=AccountStatus.healthy if enabled else AccountStatus.paused,
            next_poll_at=next_poll_at,
        )
        db.add(account)
        db.commit()
        return account.id


def test_patch_only_reschedules_for_interval_change_or_reenable() -> None:
    scheduled = datetime.now(timezone.utc) + timedelta(hours=12)
    account_id = create_account(next_poll_at=scheduled)
    with TestClient(app) as client:
        headers = login(client)
        renamed = client.patch(
            f"/api/accounts/{account_id}",
            headers=headers,
            json={"display_name": "  新名称  "},
        )
        assert renamed.status_code == 200
        assert renamed.json()["display_name"] == "新名称"
        assert renamed.json()["created_at"].endswith(("Z", "+00:00"))
        assert renamed.json()["next_poll_at"].endswith(("Z", "+00:00"))

        unchanged_interval = client.patch(
            f"/api/accounts/{account_id}",
            headers=headers,
            json={"interval_minutes": 60},
        )
        assert unchanged_interval.status_code == 200

        with SessionLocal() as db:
            account = db.get(Account, account_id)
            assert account is not None
            assert account.next_poll_at is not None
            assert as_utc(account.next_poll_at) == scheduled

        before_interval_change = datetime.now(timezone.utc)
        changed_interval = client.patch(
            f"/api/accounts/{account_id}",
            headers=headers,
            json={"interval_minutes": 120},
        )
        assert changed_interval.status_code == 200
        interval_poll_at = as_utc(datetime.fromisoformat(changed_interval.json()["next_poll_at"]))
        assert interval_poll_at >= before_interval_change

        paused = client.patch(
            f"/api/accounts/{account_id}",
            headers=headers,
            json={"enabled": False},
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"

        with SessionLocal() as db:
            account = db.get(Account, account_id)
            assert account is not None
            account.next_poll_at = scheduled
            db.commit()

        before_reenable = datetime.now(timezone.utc)
        reenabled = client.patch(
            f"/api/accounts/{account_id}",
            headers=headers,
            json={"enabled": True},
        )
        assert reenabled.status_code == 200
        assert reenabled.json()["status"] == "pending"
        assert as_utc(datetime.fromisoformat(reenabled.json()["next_poll_at"])) >= before_reenable


def test_delete_account_without_archive_removes_account_and_runs() -> None:
    account_id = create_account(next_poll_at=datetime.now(timezone.utc))
    with SessionLocal() as db:
        db.add(CrawlRun(account_id=account_id, status=JobStatus.complete))
        db.commit()

    with TestClient(app) as client:
        headers = login(client)
        response = client.delete(f"/api/accounts/{account_id}", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"message": "账号已删除"}
    with SessionLocal() as db:
        assert db.get(Account, account_id) is None
        assert db.scalar(select(CrawlRun).where(CrawlRun.account_id == account_id)) is None


def test_delete_account_with_archive_only_pauses_and_preserves_data() -> None:
    settings = get_settings()
    account_id = create_account(next_poll_at=datetime.now(timezone.utc))
    relative_archive = "bilibili/account-update/2026/07/protected-post"
    metadata_path = settings.archive_root / relative_archive / "metadata.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text("{}", encoding="utf-8")
    with SessionLocal() as db:
        db.add(
            ContentIndex(
                account_id=account_id,
                platform=Platform.bilibili,
                remote_id="protected-post",
                title="受保护归档",
                author="作者",
                content_type="video",
                source_url="https://www.bilibili.com/video/BV1PROTECTED",
                published_at=datetime.now(timezone.utc),
                archive_path=relative_archive,
            )
        )
        db.commit()

    with TestClient(app) as client:
        headers = login(client)
        response = client.delete(f"/api/accounts/{account_id}", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"message": "账号已有归档，已停用而未删除"}
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        assert account is not None
        assert account.enabled is False
        assert account.status == AccountStatus.paused
        assert account.last_error == "账号存在归档内容，为保护文件仅执行停用"
        assert db.scalar(select(ContentIndex).where(ContentIndex.account_id == account_id)) is not None
    assert metadata_path.read_text(encoding="utf-8") == "{}"
