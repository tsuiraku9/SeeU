from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import signal
import shutil
import socket
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import parse_qs, urljoin, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
import httpx


PLATFORMS = ("bilibili", "weibo", "douyin", "xiaohongshu")
ROOT = Path(os.getenv("DATA_ROOT", "/data"))
BROWSER_ROOT = ROOT / "browser" / "mediacrawler"
STAGING_ROOT = ROOT / "provider-staging"
STATE_ROOT = ROOT / "provider-state"
MAX_STAGE_FILES = int(os.getenv("MAX_STAGE_FILES", "100"))
MAX_STAGE_BYTES = int(os.getenv("MAX_STAGE_BYTES", str(2 * 1024**3)))
MIN_STAGE_FREE_BYTES = int(os.environ["MIN_STAGE_FREE_BYTES"]) if "MIN_STAGE_FREE_BYTES" in os.environ else int(
    float(os.getenv("MIN_STAGE_FREE_GB", os.getenv("MIN_FREE_DISK_GB", "5"))) * 1024**3
)
STAGE_MONITOR_INTERVAL_SECONDS = float(os.getenv("STAGE_MONITOR_INTERVAL_SECONDS", "0.25"))
STALE_STAGE_AGE_SECONDS = float(os.getenv("STALE_STAGE_AGE_SECONDS", str(24 * 60 * 60)))
DISCOVER_TIMEOUT_SECONDS = float(os.getenv("DISCOVER_TIMEOUT_SECONDS", "840"))
STAGE_TIMEOUT_SECONDS = float(os.getenv("STAGE_TIMEOUT_SECONDS", "840"))
LOGIN_TIMEOUT_SECONDS = float(os.getenv("LOGIN_TIMEOUT_SECONDS", "600"))
PROCESS_TERMINATE_GRACE_SECONDS = float(os.getenv("PROCESS_TERMINATE_GRACE_SECONDS", "10"))
NOVNC_PORT = int(os.getenv("NOVNC_PORT", "7900"))
NOVNC_BIND_ADDRESS = os.getenv("NOVNC_BIND_ADDRESS", "127.0.0.1").strip() or "127.0.0.1"
NOVNC_URL_HOST = (
    f"[{NOVNC_BIND_ADDRESS}]" if ":" in NOVNC_BIND_ADDRESS else NOVNC_BIND_ADDRESS
)
NOVNC_URL = f"http://{NOVNC_URL_HOST}:{NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale"
# websockify always listens on this container-internal port; NOVNC_PORT is the
# independently configurable host-published port shown to the administrator.
INTERNAL_NOVNC_PORT = 7900
PROVIDER_CONTRACT_FILENAME = "bridge-contract.json"
ALLOWED_HOSTS = {
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
    "douyin": ("douyin.com", "iesdouyin.com"), "bilibili": ("bilibili.com", "b23.tv"),
    "weibo": ("weibo.com", "weibo.cn"),
}
locks = {platform: asyncio.Lock() for platform in PLATFORMS}
processes: dict[str, asyncio.subprocess.Process] = {}
active_processes: set[asyncio.subprocess.Process] = set()
logout_in_progress: set[str] = set()


class StageResourceLimitError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class DiscoverRequest(BaseModel):
    platform: Literal["bilibili", "weibo", "douyin", "xiaohongshu"]
    profile_url: HttpUrl
    limit: int = Field(default=20, ge=1, le=500)


class StageRequest(BaseModel):
    platform: Literal["bilibili", "weibo", "douyin", "xiaohongshu"]
    content_id: str = Field(min_length=1, max_length=256)
    source_url: HttpUrl



@asynccontextmanager
async def lifespan(_app: FastAPI):
    for path in (BROWSER_ROOT, STAGING_ROOT, STATE_ROOT):
        path.mkdir(parents=True, exist_ok=True)
    cleanup_stale_staging()
    try:
        yield
    finally:
        running = [process for process in active_processes if process.returncode is None]
        if running:
            await asyncio.gather(*(stop_process(process) for process in running), return_exceptions=True)
        active_processes.clear()


app = FastAPI(title="MediaCrawler bridge", version="1.0.0", lifespan=lifespan)


def state_path(platform: str) -> Path: return STATE_ROOT / f"{platform}.json"
def qr_path(platform: str) -> Path: return STATE_ROOT / f"{platform}-qr.png"


def cleanup_stale_staging(now: float | None = None) -> int:
    """Remove only expired bridge-owned jobs; imports use a different prefix."""
    current_time = datetime.now(timezone.utc).timestamp() if now is None else now
    removed = 0
    for candidate in STAGING_ROOT.iterdir():
        if not candidate.is_dir() or not re.fullmatch(
            r"(?:discover-)?[0-9a-f]{32}", candidate.name
        ):
            continue
        try:
            expired = current_time - candidate.stat().st_mtime >= STALE_STAGE_AGE_SECONDS
        except OSError:
            continue
        if expired:
            shutil.rmtree(candidate, ignore_errors=True)
            if not candidate.exists():
                removed += 1
    return removed


def read_state(platform: str) -> dict:
    default = {"platform": platform, "status": "logged_out", "updated_at": None, "message": None,
               "manual_verification_url": NOVNC_URL}
    try:
        return {**default, **json.loads(state_path(platform).read_text("utf-8")), "manual_verification_url": NOVNC_URL}
    except (OSError, json.JSONDecodeError):
        return default


def write_state(platform: str, status: str, message: str | None = None) -> dict:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    value = {"platform": platform, "status": status, "updated_at": datetime.now(timezone.utc).isoformat(),
             "message": message, "manual_verification_url": NOVNC_URL}
    destination = state_path(platform)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def refresh_session_state(platform: str) -> dict:
    """Promote a starting session once its atomically-written QR is available."""
    state = read_state(platform)
    if state["status"] == "starting" and qr_path(platform).is_file():
        return write_state(platform, "qr_ready")
    return state


async def spawn(
    platform: str,
    mode: str,
    output: Path,
    value: str = "",
    limit: int = 20,
) -> asyncio.subprocess.Process:
    output.mkdir(parents=True, exist_ok=True)
    qr_path(platform).unlink(missing_ok=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "/bridge/worker.py", mode, platform, "--value", value,
        "--output", str(output), "--qr", str(qr_path(platform)),
        "--browser-root", str(BROWSER_ROOT), "--state", str(state_path(platform)),
        "--limit", str(max(1, min(limit, 500))), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    active_processes.add(process)
    return process


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix" and getattr(process, "pid", None):
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    try:
        if os.name == "posix" and getattr(process, "pid", None):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass
    await process.wait()


def stage_resource_snapshot(root: Path) -> tuple[int, int]:
    """Return current job bytes and free bytes without following symlinks."""
    total_bytes = 0
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total_bytes += path.stat().st_size
        except OSError:
            # A provider file can be atomically replaced while this snapshot is
            # walking the job. The next interval observes its final path.
            continue
    return total_bytes, shutil.disk_usage(root).free


async def monitor_stage_resources(
    process: asyncio.subprocess.Process,
    root: Path,
    *,
    max_bytes: int,
    min_free_bytes: int,
    interval_seconds: float,
) -> None:
    while process.returncode is None:
        try:
            total_bytes, free_bytes = await asyncio.to_thread(stage_resource_snapshot, root)
        except OSError as exc:
            await stop_process(process)
            raise StageResourceLimitError(
                "Unable to inspect provider staging disk capacity", 507
            ) from exc
        if total_bytes > max_bytes:
            await stop_process(process)
            raise StageResourceLimitError(
                f"Provider staging exceeded the {max_bytes}-byte limit", 413
            )
        if free_bytes < min_free_bytes:
            await stop_process(process)
            raise StageResourceLimitError(
                "Provider staging stopped because free disk space is below the safety reserve",
                507,
            )
        await asyncio.sleep(max(0, interval_seconds))


async def communicate(
    process: asyncio.subprocess.Process,
    timeout_seconds: float,
    *,
    monitor_root: Path | None = None,
    max_bytes: int = MAX_STAGE_BYTES,
    min_free_bytes: int = MIN_STAGE_FREE_BYTES,
    monitor_interval_seconds: float = STAGE_MONITOR_INTERVAL_SECONDS,
) -> tuple[bytes, bytes | None]:
    output_task: asyncio.Task | None = None
    monitor_task: asyncio.Task | None = None
    try:
        if monitor_root is None:
            return await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)

        output_task = asyncio.create_task(process.communicate())
        monitor_task = asyncio.create_task(
            monitor_stage_resources(
                process,
                monitor_root,
                max_bytes=max_bytes,
                min_free_bytes=min_free_bytes,
                interval_seconds=monitor_interval_seconds,
            )
        )
        done, _pending = await asyncio.wait_for(
            asyncio.wait(
                {output_task, monitor_task},
                return_when=asyncio.FIRST_COMPLETED,
            ),
            timeout=timeout_seconds,
        )
        # A limit failure wins even if the worker exits at the same instant.
        if monitor_task in done:
            monitor_error = monitor_task.exception()
            if monitor_error is not None:
                raise monitor_error
        if output_task not in done:
            # The monitor only returns normally after observing process exit;
            # stdout must then reach EOF promptly.
            return await output_task
        return output_task.result()
    except asyncio.TimeoutError:
        await stop_process(process)
        raise
    except asyncio.CancelledError:
        await stop_process(process)
        raise
    finally:
        for task in (output_task, monitor_task):
            if task is not None and not task.done():
                task.cancel()
        if output_task is not None or monitor_task is not None:
            await asyncio.gather(
                *(task for task in (output_task, monitor_task) if task is not None),
                return_exceptions=True,
            )
        active_processes.discard(process)


async def finish_login(platform: str, process: asyncio.subprocess.Process) -> None:
    try:
        try:
            output, _ = await communicate(process, LOGIN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            if processes.get(platform) is process:
                current = read_state(platform)
                if current["status"] == "manual_verification_required":
                    write_state(platform, "expired", "人工验证等待超时，请重新发起登录")
                else:
                    write_state(platform, "error", "Platform login timed out")
            return
        # Logout or a replacement task owns the state now; a terminated older
        # process must not overwrite it with "expired" after cleanup.
        if processes.get(platform) is not process:
            return
        text = output.decode("utf-8", "replace")[-1000:]
        lowered = text.lower()
        if process.returncode == 0:
            qr_path(platform).unlink(missing_ok=True)
            write_state(platform, "authenticated")
        elif "slider" in lowered:
            write_state(
                platform,
                "manual_verification_required",
                "请通过本机 noVNC 手动完成滑块验证；系统不会自动处理",
            )
        elif any(word in lowered for word in ("captcha", "verify", "verification")):
            write_state(platform, "manual_verification_required", "平台要求额外的人工验证")
        else:
            write_state(
                platform,
                "expired" if qr_path(platform).exists() else "error",
                safe_process_error(output, "Login did not complete"),
            )
    finally:
        shutil.rmtree(STATE_ROOT / f"login-{platform}", ignore_errors=True)
        if processes.get(platform) is process:
            processes.pop(platform, None)
        if locks[platform].locked():
            locks[platform].release()


def jsonl_items(root: Path) -> list[dict]:
    result: list[dict] = []
    for path in root.rglob("*contents*.jsonl"):
        for line in path.read_text("utf-8", errors="replace").splitlines():
            try: result.append(json.loads(line))
            except json.JSONDecodeError: pass
    return result


def pick(item: dict, *keys: str, default=""):
    for key in keys:
        if item.get(key) not in (None, ""): return item[key]
    return default


def normalize(platform: str, item: dict) -> dict:
    remote = str(pick(item, "note_id", "aweme_id", "video_id", "bvid", "dynamic_id", "id"))
    url = str(pick(item, "note_url", "aweme_url", "video_url", "source_url", "url"))
    if not url:
        templates = {"xiaohongshu": "https://www.xiaohongshu.com/explore/{}", "douyin": "https://www.douyin.com/video/{}",
                     "bilibili": "https://www.bilibili.com/video/{}", "weibo": "https://m.weibo.cn/detail/{}"}
        url = templates[platform].format(remote)
    return {"remote_id": remote, "source_url": url, "original": not bool(pick(item, "is_repost", "is_forward", default=False)),
            "title": str(pick(item, "title", "desc", "content", "text")),
            "author": str(pick(item, "nickname", "user_nickname", "author_name", "screen_name")),
            "text": str(pick(item, "desc", "content", "text", "title")),
            "published_at": pick(item, "time", "create_time", "publish_time", "last_modify_ts", default=0),
            "content_type": str(pick(item, "type", "note_type", default="unknown"))}


MEDIA_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
}


def provider_contract(
    root: Path,
    *,
    expected_platform: str | None = None,
    expected_mode: str | None = None,
) -> dict:
    path = root / PROVIDER_CONTRACT_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Pinned provider did not produce its bridge contract") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("items"), dict):
        raise ValueError("Pinned provider bridge contract has an unknown structure")
    if expected_platform is not None and payload.get("platform") != expected_platform:
        raise ValueError("Pinned provider bridge contract platform does not match the job")
    if expected_mode is not None and payload.get("mode") != expected_mode:
        raise ValueError("Pinned provider bridge contract mode does not match the job")
    return payload


def apply_provider_contract(item: dict, contract: dict) -> tuple[dict, dict]:
    provider_id = str(item.get("remote_id") or "")
    metadata = contract["items"].get(provider_id)
    if not isinstance(metadata, dict):
        raise ValueError(f"Provider contract is missing content {provider_id or '<unknown>'}")
    canonical_id = str(metadata.get("canonical_id") or "").strip()
    source_url = str(metadata.get("source_url") or "").strip()
    if not canonical_id or not source_url:
        raise ValueError("Provider contract is missing canonical content identity")
    slots = metadata.get("media_slots")
    if not isinstance(slots, list):
        raise ValueError("Provider contract contains invalid media slots")
    slot_ids: set[str] = set()
    staged_paths: set[str] = set()
    for ordinal, slot in enumerate(slots, start=1):
        if not isinstance(slot, dict):
            raise ValueError("Provider contract contains invalid media slots")
        slot_id = str(slot.get("slot_id") or "")
        source_sha256 = slot.get("source_sha256")
        staged_path = slot.get("staged_path")
        if (
            slot.get("kind") not in {"image", "video", "audio"}
            or slot.get("ordinal") != ordinal
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", slot_id)
            or slot_id in slot_ids
            or (
                source_sha256 is not None
                and not re.fullmatch(r"[0-9a-f]{64}", str(source_sha256))
            )
        ):
            raise ValueError("Provider contract contains invalid media slots")
        slot_ids.add(slot_id)
        if staged_path is not None:
            path_value = str(staged_path)
            relative = PurePosixPath(path_value)
            if (
                not path_value
                or "\\" in path_value
                or relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != path_value
                or path_value.casefold() in staged_paths
            ):
                raise ValueError("Provider contract contains invalid staged media paths")
            staged_paths.add(path_value.casefold())
    expected = int(metadata.get("expected_media_count", -1))
    if expected < 0 or expected != len(slots):
        raise ValueError("Provider contract media count does not match its slots")
    aliases = metadata.get("aliases", [])
    if not isinstance(aliases, list) or any(
        not isinstance(alias, str) or not alias.strip() for alias in aliases
    ):
        raise ValueError("Provider contract contains invalid content aliases")
    item.update(
        {
            "remote_id": canonical_id,
            "source_url": source_url,
            "original": metadata.get("original") is True,
            "pinned": metadata.get("pinned") is True,
            "content_type": str(
                metadata.get("content_type") or item.get("content_type") or "unknown"
            ),
            "aliases": list(dict.fromkeys(alias.strip() for alias in aliases)),
        }
    )
    return item, metadata


def contract_identity_matches(requested_id: str, item: dict) -> bool:
    return requested_id == item.get("remote_id") or requested_id in item.get("aliases", [])


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def staged_media(job: Path) -> tuple[list[dict], int, int]:
    root = job.resolve()
    data_files: list[Path] = []
    invalid_count = 0
    for candidate in job.rglob("*"):
        if candidate.is_symlink():
            invalid_count += 1
            continue
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() == ".jsonl" or candidate.name == PROVIDER_CONTRACT_FILENAME:
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            invalid_count += 1
            continue
        data_files.append(resolved)
    data_files.sort()
    recognized = [
        path
        for path in data_files
        if path.suffix.lower() in MEDIA_MIME_BY_SUFFIX and path.stat().st_size > 0
    ]
    media: list[dict] = []
    for path in recognized[:MAX_STAGE_FILES]:
        mime = MEDIA_MIME_BY_SUFFIX[path.suffix.lower()]
        media.append({
            "local_path": path.relative_to(job.resolve()).as_posix(),
            "kind": mime.split("/", 1)[0],
            "mime_type": mime,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return media, len(recognized), invalid_count + len(data_files) - len(recognized)


def bind_staged_media_to_slots(
    media: list[dict], item_contract: dict
) -> tuple[list[dict], bool]:
    """Return media in contract-slot order only when every file binds exactly once."""
    slots = item_contract.get("media_slots") or []
    media_by_path = {
        str(record.get("local_path") or "").casefold(): record for record in media
    }
    if len(media_by_path) != len(media):
        return [], False
    bound: list[dict] = []
    bound_paths: set[str] = set()
    for slot in slots:
        staged_path = str(slot.get("staged_path") or "")
        path_key = staged_path.casefold()
        record = media_by_path.get(path_key)
        if (
            not staged_path
            or path_key in bound_paths
            or record is None
            or record.get("kind") != slot.get("kind")
        ):
            return [], False
        bound_paths.add(path_key)
        bound.append({**record, "slot_id": slot["slot_id"]})
    if bound_paths != set(media_by_path):
        return [], False
    return bound, True


def safe_process_error(output: bytes, fallback: str) -> str:
    text = output.decode("utf-8", "replace")
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[query-redacted]", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    meaningful = [
        line for line in lines
        if not re.fullmatch(r"[╔╗╚╝═║\s]+", line)
        and line != "Traceback (most recent call last):"
        and not line.startswith("<3 Playwright Team")
    ]
    diagnostic = [
        line for line in meaningful
        if re.search(r"error|exception|failed|timeout|doesn't exist|not found", line, re.IGNORECASE)
    ]
    return ((diagnostic[-1] if diagnostic else meaningful[-1]) if meaningful else fallback)[:1000]


def validate_platform_url(platform: str, value: HttpUrl) -> None:
    host = (value.host or "").lower()
    if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS[platform]):
        raise HTTPException(422, "URL does not belong to the selected platform")


async def validate_public_platform_url(platform: str, value) -> None:
    validate_platform_url(platform, value)
    if value.username or value.password:
        raise HTTPException(422, "Platform URL must not contain embedded credentials")
    host = str(value.host or "").rstrip(".").lower()
    port = value.port or (443 if value.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
        )
        resolved = {
            ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in addresses
        }
    except (OSError, ValueError) as exc:
        raise HTTPException(422, "Platform URL host could not be resolved") from exc
    if not resolved or any(not address.is_global for address in resolved):
        raise HTTPException(422, "Platform URL resolves to a non-public address")


async def creator_value(platform: str, value: HttpUrl) -> str:
    await validate_public_platform_url(platform, value)
    url = str(value)
    short_hosts = {
        "xiaohongshu": "xhslink.com",
        "douyin": "v.douyin.com",
        "bilibili": "b23.tv",
    }
    if short_hosts.get(platform) and (
        value.host == short_hosts[platform]
        or str(value.host or "").endswith("." + short_hosts[platform])
    ):
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            current = url
            try:
                for _redirect in range(6):
                    parsed = httpx.URL(current)
                    await validate_public_platform_url(platform, parsed)
                    async with client.stream("GET", current) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise HTTPException(422, "Short URL redirect is missing a destination")
                            current = urljoin(current, location)
                            continue
                        response.raise_for_status()
                        url = str(response.url)
                        break
                else:
                    raise HTTPException(422, "Short URL exceeded the redirect limit")
            except httpx.HTTPError as exc:
                raise HTTPException(502, "Short URL resolution failed") from exc
    if platform == "douyin":
        match = re.search(r"iesdouyin\.com/share/user/(?P<uid>[^/?#]+)", url)
        if match:
            return f"https://www.douyin.com/user/{match.group('uid')}"
    if platform == "bilibili":
        parsed = urlparse(url)
        if parsed.hostname != "space.bilibili.com" or not re.fullmatch(r"/\d+/?", parsed.path):
            raise HTTPException(422, "Bilibili creator URL must resolve to a public space profile")
        return url
    if platform == "weibo":
        match = re.search(r"/(?:u/|profile/)?(?P<uid>\d+)(?:/|$)", urlparse(url).path)
        uid = match.group("uid") if match else parse_qs(urlparse(url).query).get("uid", [""])[0]
        if not uid or not str(uid).isdigit():
            raise HTTPException(422, "Weibo creator URL does not contain a numeric user id")
        return str(uid)
    return url


@app.get("/v1/health")
def health():
    try:
        with socket.create_connection(("127.0.0.1", 5900), timeout=0.25):
            desktop = "ok"
        with socket.create_connection(("127.0.0.1", INTERNAL_NOVNC_PORT), timeout=0.25):
            websocket_proxy = "ok"
    except OSError:
        desktop = "unavailable"
        websocket_proxy = "unavailable"
    if desktop != "ok" or websocket_proxy != "ok":
        raise HTTPException(503, "Interactive browser desktop is unavailable")
    return {
        "status": "ok",
        "upstream": "MediaCrawler",
        "commit": "d280d22",
        "desktop": desktop,
        "websocket_proxy": websocket_proxy,
    }


@app.get("/v1/sessions")
def sessions(): return [refresh_session_state(platform) for platform in PLATFORMS]


@app.post("/v1/sessions/{platform}/login", status_code=202)
async def login(platform: str):
    if platform not in PLATFORMS: raise HTTPException(404, "Unknown platform")
    if platform in logout_in_progress: raise HTTPException(409, "Platform logout is in progress")
    if platform in processes: return refresh_session_state(platform)
    await locks[platform].acquire()
    if platform in logout_in_progress:
        locks[platform].release()
        raise HTTPException(409, "Platform logout is in progress")
    if read_state(platform)["status"] == "authenticated":
        locks[platform].release()
        return read_state(platform)
    write_state(platform, "starting")
    try:
        process = await spawn(platform, "login", STATE_ROOT / f"login-{platform}")
    except Exception:
        locks[platform].release()
        shutil.rmtree(STATE_ROOT / f"login-{platform}", ignore_errors=True)
        write_state(platform, "error", "Unable to start platform browser")
        raise
    if platform in logout_in_progress:
        try:
            await stop_process(process)
        finally:
            locks[platform].release()
            shutil.rmtree(STATE_ROOT / f"login-{platform}", ignore_errors=True)
        raise HTTPException(409, "Platform logout is in progress")
    processes[platform] = process
    asyncio.create_task(finish_login(platform, process))
    return read_state(platform)


@app.get("/v1/sessions/{platform}/qr")
def qr(platform: str):
    if platform not in PLATFORMS: raise HTTPException(404, "Unknown platform")
    state = refresh_session_state(platform)
    if state["status"] == "authenticated":
        raise HTTPException(409, "Platform is already authenticated")
    path = qr_path(platform)
    if not path.is_file(): raise HTTPException(404, "QR code is not ready")
    payload = {**state, "image_data_url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()}
    return JSONResponse(payload, headers={"Cache-Control": "no-store, max-age=0"})


@app.delete("/v1/sessions/{platform}")
async def logout(platform: str):
    if platform not in PLATFORMS: raise HTTPException(404, "Unknown platform")
    if platform in logout_in_progress: raise HTTPException(409, "Platform logout is in progress")
    logout_in_progress.add(platform)
    try:
        process = processes.pop(platform, None)
        if process and process.returncode is None:
            await stop_process(process)
        # A discovery/staging worker also owns this lock while its browser uses
        # the persistent profile. Never remove that profile underneath it.
        async with locks[platform]:
            profile = (BROWSER_ROOT / platform).resolve()
            if profile.parent == BROWSER_ROOT.resolve():
                shutil.rmtree(profile, ignore_errors=True)
            qr_path(platform).unlink(missing_ok=True)
            return write_state(platform, "logged_out")
    finally:
        logout_in_progress.discard(platform)


async def require_session(platform: str) -> None:
    if read_state(platform)["status"] != "authenticated": raise HTTPException(401, "login_required")


@app.post("/v1/creators/discover")
async def discover(request: DiscoverRequest):
    await require_session(request.platform)
    value = await creator_value(request.platform, request.profile_url)
    async with locks[request.platform]:
        job = STAGING_ROOT / f"discover-{uuid.uuid4().hex}"
        try:
            process = await spawn(request.platform, "discover", job, value, request.limit)
            try:
                output, _ = await communicate(process, DISCOVER_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise HTTPException(504, "Creator discovery timed out")
            if process.returncode:
                detail = safe_process_error(output, "Creator discovery failed")
                if any(word in detail.lower() for word in ("login", "cookie", "verify", "captcha")):
                    write_state(request.platform, "expired", "Session expired or verification is required")
                    raise HTTPException(401, "login_required")
                raise HTTPException(502, detail)
            try:
                contract = provider_contract(
                    job,
                    expected_platform=request.platform,
                    expected_mode="discover",
                )
            except ValueError as exc:
                raise HTTPException(502, str(exc)) from exc
            items, seen = [], set()
            for raw in jsonl_items(job):
                try:
                    item, _metadata = apply_provider_contract(
                        normalize(request.platform, raw), contract
                    )
                except (TypeError, ValueError) as exc:
                    raise HTTPException(502, str(exc)) from exc
                if item["remote_id"] and item["remote_id"] not in seen and item["original"]:
                    seen.add(item["remote_id"]); items.append(item)
            expected_original_ids: list[str] = []
            identity_owners: dict[str, str] = {}
            for metadata in contract["items"].values():
                if not isinstance(metadata, dict) or metadata.get("original") is not True:
                    continue
                canonical_id = str(metadata.get("canonical_id") or "").strip()
                aliases = metadata.get("aliases") or []
                if not canonical_id or not isinstance(aliases, list):
                    raise HTTPException(502, "Provider contract has invalid content identities")
                expected_original_ids.append(canonical_id)
                for identity in (canonical_id, *(str(alias).strip() for alias in aliases)):
                    if not identity:
                        raise HTTPException(502, "Provider contract has invalid content identities")
                    owner = identity_owners.get(identity)
                    if owner is not None and owner != canonical_id:
                        raise HTTPException(502, "Provider contract has colliding content identities")
                    identity_owners[identity] = canonical_id
            if (
                len(expected_original_ids) != len(set(expected_original_ids))
                or seen != set(expected_original_ids)
            ):
                raise HTTPException(502, "Provider contract and discovery records do not match")
            if not items:
                raise HTTPException(502, "Provider completed but returned no recognizable creator content")
            discovery = contract.get("discovery") or {}
            truncated = bool(
                discovery.get("truncated", len(items) >= request.limit)
            )
            return {"items": items[:request.limit], "truncated": truncated}
        finally: shutil.rmtree(job, ignore_errors=True)


@app.post("/v1/content/stage")
async def stage(request: StageRequest):
    await require_session(request.platform)
    await validate_public_platform_url(request.platform, request.source_url)
    async with locks[request.platform]:
        job_id = uuid.uuid4().hex
        job = STAGING_ROOT / job_id
        preserve_job = False
        try:
            process = await spawn(request.platform, "stage", job, str(request.source_url), 1)
            try:
                output, _ = await communicate(
                    process,
                    STAGE_TIMEOUT_SECONDS,
                    monitor_root=job,
                )
            except asyncio.TimeoutError:
                raise HTTPException(504, "Content staging timed out")
            except StageResourceLimitError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
            if process.returncode:
                detail = safe_process_error(output, "Content staging failed")
                if any(word in detail.lower() for word in ("login", "cookie", "verify", "captcha")):
                    write_state(request.platform, "expired", "Session expired or verification is required")
                    raise HTTPException(401, "login_required")
                raise HTTPException(502, detail)
            raw_items = jsonl_items(job)
            if not raw_items:
                raise HTTPException(502, "Provider returned no content detail")
            raw_item = raw_items[-1]
            try:
                contract = provider_contract(
                    job,
                    expected_platform=request.platform,
                    expected_mode="stage",
                )
                if len(contract["items"]) != 1 or len(raw_items) != 1:
                    raise ValueError("Provider stage contract must contain exactly one content item")
                item, item_contract = apply_provider_contract(
                    normalize(request.platform, raw_item), contract
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(502, str(exc)) from exc
            if not contract_identity_matches(request.content_id, item):
                raise HTTPException(502, "Provider returned content that does not match the requested id")
            if not item["original"]:
                raise HTTPException(502, "Provider returned a repost for an original-content request")

            media, downloaded_count, unrecognized_count = staged_media(job)
            downloaded_bytes = sum(int(record["size_bytes"]) for record in media)
            expected_count = int(item_contract["expected_media_count"])
            unsupported_media = item_contract.get("unsupported_media") is True
            bound_media, media_slots_match = bind_staged_media_to_slots(
                media, item_contract
            )
            complete = (
                downloaded_count == expected_count
                and downloaded_count <= MAX_STAGE_FILES
                and downloaded_bytes <= MAX_STAGE_BYTES
                and unrecognized_count == 0
                and not unsupported_media
                and media_slots_match
            )
            preserve_job = complete
            result = {
                "job_id": job_id,
                "content_id": request.content_id,
                **item,
                "expected_media_count": expected_count,
                "downloaded_media_count": downloaded_count,
                "downloaded_bytes": downloaded_bytes,
                "complete": complete,
                "media": bound_media if media_slots_match else media,
            }
            if not complete:
                result["message"] = (
                    "Provider media staging was incomplete: "
                    f"expected {expected_count}, downloaded {downloaded_count}, "
                    f"bytes {downloaded_bytes}, invalid files {unrecognized_count}, "
                    f"unsupported media {unsupported_media}, "
                    f"media slots match {media_slots_match}"
                )
            return result
        finally:
            if not preserve_job:
                shutil.rmtree(job, ignore_errors=True)


@app.delete("/v1/staging/{job_id}")
def cleanup(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id): raise HTTPException(422, "Invalid job id")
    shutil.rmtree(STAGING_ROOT / job_id, ignore_errors=True)
    return {"ok": True}
