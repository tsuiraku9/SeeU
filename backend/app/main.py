from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException

from .adapters import get_adapter
from .adapters.base import AdapterError
from .adapters.base import NormalizedContent, parse_datetime
from .account_state import (
    observed_ids_for_account,
    pending_refs_for_account,
    recover_interrupted_polls,
    sync_all_account_ledgers,
    write_account_ledger,
    write_account_tombstone,
)
from .archive import ArchiveError, ArchiveManager, sanitize_component
from .collector import CollectorService
from .config import get_settings
from .database import SessionLocal, get_db, init_database
from .models import (
    Account,
    AccountStatus,
    Admin,
    CompletenessStatus,
    ContentIndex,
    CrawlRun,
    JobStatus,
    ObservedContent,
    Platform,
    utcnow,
)
from .schemas import (
    AccountCreate,
    AccountOut,
    AccountTestOut,
    AccountUpdate,
    AuthResponse,
    ContentDetail,
    ContentOut,
    ContentPage,
    CrawlRunOut,
    CrawlRunPage,
    LoginRequest,
    MessageOut,
    StorageOut,
)
from .scheduler import create_scheduler
from .rebuild import reconcile_legacy_content_index, restore_account_ledgers
from .provider import HttpProvider, ProviderError, ProviderExecutionError, ProviderUnavailableError
from .security import (
    login_rate_limiter,
    new_csrf_token,
    require_csrf,
    require_session,
    verify_webui_login_token,
)


settings = get_settings()
logger = logging.getLogger(__name__)
collector = CollectorService(settings)
scheduler = create_scheduler(collector)
provider = HttpProvider(settings)

IMPORT_MULTIPART_OVERHEAD_BYTES = 2 * 1024 * 1024
IMPORT_TEXT_PART_MAX_BYTES = 16 * 1024
IMPORT_MAX_DIRECTORY_ENTRIES = 32
IMPORT_MAX_PATH_PARTS = 16
IMPORT_MAX_COMPRESSION_RATIO = 200


class _ImportBodyTooLarge(OSError):
    """Raised inside Starlette's multipart stream so open spools are closed."""


async def _parse_import_form(
    request: Request,
) -> tuple[FormData, StarletteUploadFile, str, int | None]:
    """Parse an authenticated import request without allowing an unbounded spool."""
    max_request_bytes = settings.import_max_bytes + IMPORT_MULTIPART_OVERHEAD_BYTES
    content_length = request.headers.get("content-length")
    if content_length:
        if not content_length.isdecimal():
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if int(content_length) > max_request_bytes:
            raise HTTPException(status_code=413, detail="Import request exceeds configured byte limit")

    received_bytes = 0
    original_receive = request.receive

    async def limited_receive():
        nonlocal received_bytes
        message = await original_receive()
        if message["type"] == "http.request":
            received_bytes += len(message.get("body", b""))
            if received_bytes > max_request_bytes:
                # MultiPartParser closes all files when its stream raises OSError.
                raise _ImportBodyTooLarge("Import request exceeds configured byte limit")
        return message

    bounded_request = Request(request.scope, receive=limited_receive)
    try:
        form_data = await bounded_request.form(
            max_files=1,
            max_fields=2,
            max_part_size=IMPORT_TEXT_PART_MAX_BYTES,
        )
    except _ImportBodyTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except MultiPartException as exc:
        raise HTTPException(status_code=422, detail=f"Invalid multipart import: {exc}") from exc

    try:
        items = list(form_data.multi_items())
        names = [name for name, _value in items]
        allowed_names = {"file", "account_mode", "target_account_id"}
        if any(name not in allowed_names for name in names):
            raise HTTPException(status_code=422, detail="Import contains an unexpected form field")
        if names.count("file") != 1:
            raise HTTPException(status_code=422, detail="Exactly one ZIP file is required")
        if names.count("account_mode") > 1 or names.count("target_account_id") > 1:
            raise HTTPException(status_code=422, detail="Import form fields must not be repeated")

        file_value = form_data.get("file")
        if not isinstance(file_value, StarletteUploadFile):
            raise HTTPException(status_code=422, detail="A ZIP file is required")
        if file_value.size is None or file_value.size > settings.import_max_bytes:
            raise HTTPException(status_code=413, detail="Import exceeds configured byte limit")
        account_mode = str(form_data.get("account_mode") or "new")
        if account_mode not in {"existing", "new"}:
            raise HTTPException(status_code=422, detail="account_mode must be existing or new")
        target_value = str(form_data.get("target_account_id") or "").strip()
        try:
            target_account_id = int(target_value) if target_value else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="target_account_id must be an integer") from exc
        if not file_value.filename or not file_value.filename.lower().endswith(".zip"):
            raise HTTPException(status_code=422, detail="A ZIP file is required")
        return form_data, file_value, account_mode, target_account_id
    except BaseException:
        await form_data.close()
        raise


def bootstrap_admin() -> None:
    with SessionLocal() as db:
        admin = db.scalar(select(Admin).limit(1))
        if not admin:
            db.add(Admin(username="admin", password_hash="token-login-disabled-password-field"))
            db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initial_poll_task: asyncio.Task[None] | None = None
    generated_webui_token = settings.ensure_webui_login_token()
    settings.validate_secrets()
    settings.ensure_directories()
    if generated_webui_token is not None:
        generated_token_path = settings.publish_generated_webui_login_token(generated_webui_token)
        logger.warning(
            "WEBUI_LOGIN_TOKEN is not configured; the generated token was written to %s",
            generated_token_path,
        )
    else:
        settings.clear_generated_webui_login_token()
    init_database()
    bootstrap_admin()
    with SessionLocal() as db:
        restore_account_ledgers(db, settings)
        db.commit()
        reconcile_legacy_content_index(db, settings)
        recover_interrupted_polls(db, settings)
        sync_all_account_ledgers(db, settings)
        db.commit()
    if settings.scheduler_enabled:
        scheduler.start()
        initial_poll_task = asyncio.create_task(
            collector.poll_due_accounts(), name="initial-due-account-poll"
        )
    try:
        yield
    finally:
        if settings.scheduler_enabled and scheduler.running:
            scheduler.shutdown(wait=False)
        if initial_poll_task is not None and not initial_poll_task.done():
            initial_poll_task.cancel()
            await asyncio.gather(initial_poll_task, return_exceptions=True)


app = FastAPI(title=settings.app_name, version="0.4.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or "configuration-required-before-startup",
    same_site="lax",
    https_only=settings.cookie_secure,
    max_age=60 * 60 * 12,
    session_cookie="archive_session",
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if settings.cookie_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; media-src 'self'; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    if request.url.path.startswith("/api/auth"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response

DbSession = Annotated[Session, Depends(get_db)]


@app.get("/api/health")
def health(db: DbSession) -> dict:
    try:
        db.execute(select(1)).scalar_one()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc
    return {"status": "ok", "service": settings.app_name, "version": app.version}


@app.get("/api/capabilities")
def capabilities(request: Request, db: DbSession) -> dict:
    require_session(request, db)
    return {
        "provider": {
            "mode": "external_http",
            "configured": settings.provider_configured,
            "contract_version": 1,
        },
        "discovery_limit": settings.provider_discovery_limit,
        "strict_media_completeness": True,
        "account_ledger_recovery": True,
        "archive_manifest_schema": 2,
        "fake_ip_dns_enabled": settings.allow_fake_ip_dns,
    }


def provider_http_error(exc: ProviderError) -> HTTPException:
    status_code = 503 if isinstance(exc, ProviderUnavailableError) else 502 if isinstance(exc, ProviderExecutionError) else 409
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": str(exc)})


@app.get("/api/platform-sessions")
async def platform_sessions(request: Request, response: Response, db: DbSession) -> list[dict]:
    require_session(request, db)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    if not settings.provider_configured:
        return []
    try:
        return await provider.sessions()
    except ProviderError as exc:
        raise provider_http_error(exc) from exc


@app.post("/api/platform-sessions/{platform}/login")
async def platform_login(platform: Platform, request: Request, db: DbSession) -> dict:
    require_session(request, db); require_csrf(request)
    try:
        return await provider.login(platform)
    except ProviderError as exc:
        raise provider_http_error(exc) from exc


@app.get("/api/platform-sessions/{platform}/qr")
async def platform_qr(platform: Platform, request: Request, response: Response, db: DbSession) -> dict:
    require_session(request, db)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    try:
        return await provider.qr(platform)
    except ProviderError as exc:
        raise provider_http_error(exc) from exc


@app.delete("/api/platform-sessions/{platform}")
async def platform_logout(platform: Platform, request: Request, db: DbSession) -> dict:
    require_session(request, db); require_csrf(request)
    try:
        return await provider.logout(platform)
    except ProviderError as exc:
        raise provider_http_error(exc) from exc


@app.post("/api/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, db: DbSession) -> AuthResponse:
    client = request.client.host if request.client else "unknown"
    login_rate_limiter.check(client)
    admin = db.scalar(select(Admin).limit(1))
    if not admin or not verify_webui_login_token(payload.token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="WebUI 登录 Token 错误")
    login_rate_limiter.reset(client)
    csrf_token = new_csrf_token()
    request.session.clear()
    request.session.update(
        {
            "admin_id": admin.id,
            "session_version": admin.session_version,
            "auth_fingerprint": settings.webui_auth_fingerprint,
            "csrf_token": csrf_token,
        }
    )
    return AuthResponse(username=admin.username, csrf_token=csrf_token)


@app.get("/api/auth/me", response_model=AuthResponse)
def me(request: Request, db: DbSession) -> AuthResponse:
    admin_id = require_session(request, db)
    admin = db.get(Admin, admin_id)
    if not admin:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Authentication required")
    csrf_token = str(request.session.get("csrf_token") or new_csrf_token())
    request.session["csrf_token"] = csrf_token
    return AuthResponse(username=admin.username, csrf_token=csrf_token)


@app.post("/api/auth/logout", response_model=MessageOut)
def logout(request: Request, db: DbSession) -> MessageOut:
    require_session(request, db)
    require_csrf(request)
    request.session.clear()
    return MessageOut(message="已退出登录")


@app.get("/api/accounts", response_model=list[AccountOut])
def list_accounts(request: Request, db: DbSession) -> list[Account]:
    require_session(request, db)
    return list(db.scalars(select(Account).order_by(Account.created_at)))


@app.post("/api/accounts", response_model=AccountOut, status_code=201)
def create_account(payload: AccountCreate, request: Request, db: DbSession) -> Account:
    require_session(request, db)
    require_csrf(request)
    adapter = get_adapter(payload.platform, settings)
    try:
        normalized = adapter.normalize_profile_url(str(payload.source_url))
    except AdapterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if db.scalar(select(Account).where(Account.source_url == normalized)):
        raise HTTPException(status_code=409, detail="该账号已经存在")
    slug = adapter.account_slug(normalized)
    account = Account(
        platform=payload.platform,
        display_name=(payload.display_name or slug).strip(),
        slug=slug,
        source_url=normalized,
        interval_minutes=payload.interval_minutes,
        status=AccountStatus.pending,
        next_poll_at=utcnow(),
    )
    db.add(account)
    db.flush()
    write_account_ledger(settings, account, [], [])
    db.commit()
    db.refresh(account)
    return account


@app.patch("/api/accounts/{account_id}", response_model=AccountOut)
def update_account(account_id: int, payload: AccountUpdate, request: Request, db: DbSession) -> Account:
    require_session(request, db)
    require_csrf(request)
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    was_enabled = account.enabled
    previous_interval = account.interval_minutes
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        if key == "display_name" and value is not None:
            value = value.strip()
            if not value:
                raise HTTPException(status_code=422, detail="显示名称不能为空")
        setattr(account, key, value)
    if account.enabled:
        if not was_enabled or account.interval_minutes != previous_interval:
            account.next_poll_at = utcnow()
        if account.status == AccountStatus.paused:
            account.status = AccountStatus.pending
    else:
        account.status = AccountStatus.paused
    db.flush()
    write_account_ledger(
        settings,
        account,
        observed_ids_for_account(db, account),
        pending_refs_for_account(db, account),
    )
    db.commit()
    db.refresh(account)
    return account


@app.delete("/api/accounts/{account_id}", response_model=MessageOut)
def delete_account(account_id: int, request: Request, db: DbSession) -> MessageOut:
    require_session(request, db)
    require_csrf(request)
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    if account.contents:
        account.enabled = False
        account.status = AccountStatus.paused
        account.last_error = "账号存在归档内容，为保护文件仅执行停用"
        db.flush()
        write_account_ledger(
            settings,
            account,
            observed_ids_for_account(db, account),
            pending_refs_for_account(db, account),
        )
        db.commit()
        return MessageOut(message="账号已有归档，已停用而未删除")
    write_account_tombstone(settings, account)
    db.delete(account)
    db.commit()
    return MessageOut(message="账号已删除")


@app.post("/api/accounts/{account_id}/test", response_model=AccountTestOut)
async def test_account(account_id: int, request: Request, db: DbSession) -> AccountTestOut:
    require_session(request, db)
    require_csrf(request)
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    adapter = get_adapter(account.platform, settings)
    async with collector.provider_slot():
        try:
            refs = await provider.discover(account.platform, account.source_url)
        except (ProviderUnavailableError, ProviderExecutionError) as exc:
            if account.platform not in {Platform.bilibili, Platform.weibo}:
                raise provider_http_error(exc) from exc
            try:
                refs = await adapter.fetch_latest(account.source_url)
            except AdapterError as adapter_exc:
                raise HTTPException(status_code=422, detail=str(adapter_exc)) from adapter_exc
        except ProviderError as exc:
            raise provider_http_error(exc) from exc
    return AccountTestOut(ok=True, found=len(refs), latest_ids=[ref.remote_id for ref in refs[:5]])


@app.post("/api/imports", status_code=201)
async def import_archive(
    request: Request,
    db: DbSession,
) -> dict:
    """Import a versioned archive ZIP through the same strict atomic boundary."""
    # Do not declare UploadFile/Form parameters here. FastAPI parses those
    # before entering the handler, which would let an unauthenticated request
    # fill the multipart spool directory before session and CSRF checks run.
    require_session(request, db)
    require_csrf(request)
    form_data, file, account_mode, target_account_id = await _parse_import_form(request)
    try:
        work = Path(
            tempfile.mkdtemp(prefix="archive-import-", dir=str(settings.provider_staging_root))
        )
    except Exception:
        await form_data.close()
        raise
    upload = work / "upload.zip"
    extracted = work / "files"
    try:
        size = 0
        with upload.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.import_max_bytes:
                    raise HTTPException(status_code=413, detail="Import exceeds configured byte limit")
                handle.write(chunk)
        try:
            with zipfile.ZipFile(upload) as bundle:
                members = bundle.infolist()
                files = [member for member in members if not member.is_dir()]
                directories = [member for member in members if member.is_dir()]
                if len(files) > settings.import_max_files + 1:
                    raise HTTPException(status_code=422, detail="Import contains too many files")
                if len(directories) > IMPORT_MAX_DIRECTORY_ENTRIES:
                    raise HTTPException(status_code=422, detail="Import contains too many directories")
                uncompressed_bytes = sum(member.file_size for member in files)
                if uncompressed_bytes > settings.import_max_bytes:
                    raise HTTPException(
                        status_code=413, detail="Uncompressed import exceeds configured byte limit"
                    )
                minimum_free = int(settings.min_free_disk_gb * 1024**3)
                if shutil.disk_usage(settings.provider_staging_root).free < (
                    minimum_free + uncompressed_bytes
                ):
                    raise HTTPException(
                        status_code=507,
                        detail="Insufficient free space to extract the import safely",
                    )
                seen_paths: set[str] = set()
                path_kinds: dict[str, bool] = {}
                for member in members:
                    path = Path(member.filename.replace("\\", "/"))
                    unix_type = (member.external_attr >> 16) & 0o170000
                    is_special = unix_type not in {0, 0o040000, 0o100000}
                    normalized = path.as_posix().rstrip("/").casefold()
                    if (
                        not normalized
                        or path.is_absolute()
                        or ".." in path.parts
                        or any(
                            ":" in part or any(ord(character) < 32 for character in part)
                            for part in path.parts
                        )
                        or len(path.parts) > IMPORT_MAX_PATH_PARTS
                        or len(path.as_posix()) > 1024
                        or is_special
                        or member.flag_bits & 0x1
                    ):
                        raise HTTPException(status_code=422, detail="ZIP contains an unsafe path")
                    if normalized in seen_paths:
                        raise HTTPException(status_code=422, detail="ZIP contains duplicate paths")
                    seen_paths.add(normalized)
                    path_kinds[normalized] = member.is_dir()
                    if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                        raise HTTPException(status_code=422, detail="ZIP uses an unsupported compression method")
                    if (
                        member.file_size > 10 * 1024 * 1024
                        and member.file_size
                        > max(1, member.compress_size) * IMPORT_MAX_COMPRESSION_RATIO
                    ):
                        raise HTTPException(status_code=422, detail="ZIP compression ratio is unsafe")
                for normalized, is_directory in path_kinds.items():
                    parts = normalized.split("/")
                    parents = ["/".join(parts[:index]) for index in range(1, len(parts))]
                    if any(parent in path_kinds and not path_kinds[parent] for parent in parents):
                        raise HTTPException(status_code=422, detail="ZIP paths conflict")
                    if not is_directory and any(
                        other.startswith(normalized + "/") for other in path_kinds
                    ):
                        raise HTTPException(status_code=422, detail="ZIP paths conflict")
                extracted.mkdir()
                extracted_bytes = 0
                for member in members:
                    relative = Path(member.filename.replace("\\", "/"))
                    destination = extracted / relative
                    if member.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as source, destination.open("xb") as target:
                        while chunk := source.read(1024 * 1024):
                            extracted_bytes += len(chunk)
                            if extracted_bytes > settings.import_max_bytes:
                                raise HTTPException(
                                    status_code=413,
                                    detail="Uncompressed import exceeds configured byte limit",
                                )
                            if shutil.disk_usage(settings.provider_staging_root).free < (
                                minimum_free + len(chunk)
                            ):
                                raise HTTPException(
                                    status_code=507,
                                    detail="Free space fell below the configured safety threshold",
                                )
                            target.write(chunk)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=422, detail="Invalid ZIP file") from exc
        manifest_path = extracted / "manifest.json"
        if not manifest_path.is_file():
            raise HTTPException(status_code=422, detail="manifest.json is required")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest.json must contain a JSON object")
            schema_version = int(manifest.get("schema_version", 0))
            if schema_version not in {1, 2}:
                raise ValueError("schema_version must be 1 or 2")
            platform = Platform(str(manifest["platform"]))
            remote_id = str(manifest["content_id"]).strip()
            source_url = str(manifest["source_url"]).strip()
            published_at = parse_datetime(manifest["published_at"])
            raw_media = manifest.get("media")
            if not isinstance(raw_media, list) or any(
                not isinstance(record, dict) for record in raw_media
            ):
                raise ValueError("media must be an array of objects")
            media = list(raw_media)
            expected_media_count = int(manifest.get("expected_media_count", len(media)))
            provider_complete = (
                str(manifest.get("status", "complete")) == "complete"
                and str(manifest.get("integrity_status", "complete")) == "complete"
                and int(manifest.get("verified_media_count", len(media))) == len(media)
            )
            if not remote_id or not source_url.startswith(("http://", "https://")):
                raise ValueError("content_id and HTTP(S) source_url are required")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid manifest: {exc}") from exc
        if db.scalar(select(ContentIndex).where(ContentIndex.platform == platform, ContentIndex.remote_id == remote_id)):
            raise HTTPException(status_code=409, detail="Content is already archived")
        if account_mode == "existing":
            if target_account_id is None:
                raise HTTPException(status_code=422, detail="target_account_id is required")
            account = db.get(Account, target_account_id)
            if not account:
                raise HTTPException(status_code=404, detail="目标账号不存在")
            if account.platform != platform:
                raise HTTPException(status_code=422, detail="目标账号平台与归档平台不一致")
        else:
            slug = sanitize_component(str(manifest.get("author") or f"import-{platform.value}"))
            candidate_slug = slug
            suffix = 1
            while db.scalar(
                select(Account).where(Account.platform == platform, Account.slug == candidate_slug)
            ):
                suffix += 1
                candidate_slug = f"{slug[:110]}-{suffix}"
            account = Account(
                platform=platform,
                display_name=str(manifest.get("author") or f"Imported {platform.value}")[:160],
                slug=candidate_slug,
                source_url=f"https://import.invalid/{platform.value}/{candidate_slug}",
                enabled=False,
                status=AccountStatus.paused,
                baseline_established=True,
                completeness_status=CompletenessStatus.unknown,
            )
            db.add(account)
            db.flush()
        content = NormalizedContent(
            platform=platform, remote_id=remote_id, source_url=source_url,
            title=str(manifest.get("title") or remote_id), author=str(manifest.get("author") or ""),
            text=str(manifest.get("text") or manifest.get("body") or ""), published_at=published_at,
            content_type=str(manifest.get("content_type") or "unknown"),
        )
        try:
            archive_path, metadata = ArchiveManager(settings).archive_from_files(
                content,
                account.slug,
                extracted,
                media,
                expected_media_count=expected_media_count,
                provider_complete=provider_complete,
            )
        except (ArchiveError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        index = ContentIndex(
            account_id=account.id, platform=platform, remote_id=remote_id,
            title=content.title[:500], author=content.author[:160],
            content_type=str(metadata.get("content_type") or content.content_type),
            source_url=source_url, published_at=published_at,
            collected_at=datetime.fromisoformat(metadata["collected_at"]),
            archive_path=str(archive_path.relative_to(settings.archive_root.resolve())),
            summary=content.text[:500], media_count=len(metadata["media"]),
            expected_media_count=int(metadata.get("expected_media_count", len(metadata["media"]))),
            verified_media_count=int(metadata.get("verified_media_count", len(metadata["media"]))),
            integrity_status=CompletenessStatus.complete, status=JobStatus.complete,
        )
        db.add(index)
        observation = db.scalar(
            select(ObservedContent).where(
                ObservedContent.account_id == account.id,
                ObservedContent.remote_id == remote_id,
            )
        )
        if observation is None:
            observation = ObservedContent(
                account_id=account.id, remote_id=remote_id, source_url=source_url
            )
            db.add(observation)
        observation.retry_pending = False
        observation.last_error = None
        observation.archived_at = datetime.fromisoformat(metadata["collected_at"])
        db.flush()
        write_account_ledger(
            settings,
            account,
            list(dict.fromkeys([remote_id, *observed_ids_for_account(db, account)])),
            pending_refs_for_account(db, account),
        )
        db.commit(); db.refresh(index)
        return {"ok": True, "content_id": index.id, "remote_id": remote_id}
    finally:
        try:
            await form_data.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)


@app.post("/api/accounts/{account_id}/poll", response_model=CrawlRunOut)
async def poll_account(account_id: int, request: Request, db: DbSession) -> CrawlRun:
    require_session(request, db)
    require_csrf(request)
    if not db.get(Account, account_id):
        raise HTTPException(status_code=404, detail="账号不存在")
    try:
        return await collector.poll_account(account_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/contents", response_model=ContentPage)
def list_contents(
    request: Request,
    db: DbSession,
    platform: Platform | None = None,
    account_id: int | None = None,
    content_type: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    require_session(request, db)
    statement = select(ContentIndex)
    if platform:
        statement = statement.where(ContentIndex.platform == platform)
    if account_id:
        statement = statement.where(ContentIndex.account_id == account_id)
    if content_type:
        statement = statement.where(ContentIndex.content_type == content_type)
    if q:
        term = f"%{q}%"
        statement = statement.where(
            or_(ContentIndex.title.ilike(term), ContentIndex.summary.ilike(term), ContentIndex.author.ilike(term))
        )
    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    items = list(
        db.scalars(statement.order_by(desc(ContentIndex.published_at)).offset(offset).limit(limit))
    )
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
    }


def read_archive_record(content: ContentIndex) -> tuple[str, dict]:
    archive_dir = (settings.archive_root / content.archive_path).resolve()
    root = settings.archive_root.resolve()
    if root not in archive_dir.parents:
        raise HTTPException(status_code=400, detail="Invalid archive path")
    try:
        markdown = (archive_dir / "content.md").read_text(encoding="utf-8")
        metadata = json.loads((archive_dir / "metadata.json").read_text(encoding="utf-8"))
        return markdown, metadata
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"归档文件不可读: {exc}") from exc


@app.get("/api/contents/{content_id}", response_model=ContentDetail)
def content_detail(content_id: int, request: Request, db: DbSession) -> ContentDetail:
    require_session(request, db)
    content = db.get(ContentIndex, content_id)
    if not content:
        raise HTTPException(status_code=404, detail="内容不存在")
    markdown, metadata = read_archive_record(content)
    base = ContentOut.model_validate(content).model_dump()
    return ContentDetail(**base, markdown=markdown, metadata=metadata)


def file_iterator(path: Path, start: int, length: int, chunk_size: int = 1024 * 1024):
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.get("/api/media/{content_id}/{media_path:path}")
def media_file(content_id: int, media_path: str, request: Request, db: DbSession) -> Response:
    require_session(request, db)
    content = db.get(ContentIndex, content_id)
    if not content:
        raise HTTPException(status_code=404, detail="内容不存在")
    media_root = (settings.archive_root / content.archive_path / "media").resolve()
    target = (media_root / media_path).resolve()
    if media_root != target.parent and media_root not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid media path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    file_size = target.stat().st_size
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(target, media_type=media_type, headers={"Accept-Ranges": "bytes"})
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    start_text, end_text = match.groups()
    if not start_text:
        suffix = int(end_text or "0")
        start = max(0, file_size - suffix)
        end = file_size - 1
    else:
        start = int(start_text)
        end = min(int(end_text) if end_text else file_size - 1, file_size - 1)
    if start > end or start >= file_size:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    length = end - start + 1
    return StreamingResponse(
        file_iterator(target, start, length),
        status_code=206,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        },
    )


@app.get("/api/runs", response_model=CrawlRunPage)
def list_runs(
    request: Request,
    db: DbSession,
    account_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    require_session(request, db)
    statement = select(CrawlRun)
    if account_id:
        statement = statement.where(CrawlRun.account_id == account_id)
    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    items = list(
        db.scalars(statement.order_by(desc(CrawlRun.started_at)).offset(offset).limit(limit))
    )
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
    }


@app.get("/api/storage", response_model=StorageOut)
def storage_info(request: Request, db: DbSession) -> dict:
    require_session(request, db)
    return ArchiveManager(settings).storage_status()


@app.get("/api/summary")
def summary(request: Request, db: DbSession) -> dict:
    require_session(request, db)
    return {
        "accounts": db.scalar(select(func.count(Account.id))) or 0,
        "healthy_accounts": db.scalar(
            select(func.count(Account.id)).where(Account.status == AccountStatus.healthy)
        )
        or 0,
        "contents": db.scalar(select(func.count(ContentIndex.id))) or 0,
        "failed_runs": db.scalar(
            select(func.count(CrawlRun.id)).where(CrawlRun.status == JobStatus.failed)
        )
        or 0,
    }


@app.api_route(
    "/api/{unknown_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
def unknown_api_route(unknown_path: str) -> None:
    """Keep unknown API paths out of the SPA fallback and return a real 404."""
    raise HTTPException(status_code=404, detail="API endpoint not found")


frontend_candidates = [Path("frontend/dist"), Path("/app/frontend_dist")]
frontend_dist = next((path for path in frontend_candidates if path.exists()), None)
if frontend_dist:
    assets = frontend_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}")
    def frontend(full_path: str):
        target = (frontend_dist / full_path).resolve()
        if full_path and frontend_dist.resolve() in target.parents and target.is_file():
            return FileResponse(target)
        return FileResponse(frontend_dist / "index.html")
