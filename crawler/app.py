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
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urljoin, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl
import httpx

try:
    from .contract_validation import (
        PROVIDER_CONTRACT_FILENAME,
        apply_provider_contract,
        bind_staged_media_to_slots,
        contract_identity_matches,
        provider_contract,
    )
    from .network_policy import address_is_allowed, contains_proxy_fake_ip, env_flag
    from .session_state import (
        read_session_state,
        session_qr_path,
        session_state_path,
        write_session_state,
    )
    from .upstream_compatibility import verify_upstream_compatibility
    from .worker_protocol import (
        LOGIN_REQUIRED_CODE,
        MANUAL_VERIFICATION_CODE,
        PROVIDER_EXECUTION_CODE,
        read_worker_result,
        write_worker_request,
    )
except ImportError:  # Deployed as top-level modules in /bridge.
    from contract_validation import (
        PROVIDER_CONTRACT_FILENAME,
        apply_provider_contract,
        bind_staged_media_to_slots,
        contract_identity_matches,
        provider_contract,
    )
    from network_policy import address_is_allowed, contains_proxy_fake_ip, env_flag
    from session_state import (
        read_session_state,
        session_qr_path,
        session_state_path,
        write_session_state,
    )
    from upstream_compatibility import verify_upstream_compatibility
    from worker_protocol import (
        LOGIN_REQUIRED_CODE,
        MANUAL_VERIFICATION_CODE,
        PROVIDER_EXECUTION_CODE,
        read_worker_result,
        write_worker_request,
    )


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
NOVNC_HTML = Path("/usr/share/novnc/vnc.html")
X11_SOCKET = Path("/tmp/.X11-unix/X99")
PROVIDER_BUILD_METADATA = Path(
    os.getenv(
        "PROVIDER_BUILD_METADATA",
        "/opt/MediaCrawler/.bridge-build.json",
    )
)
MEDIACRAWLER_ROOT = Path(os.getenv("MEDIACRAWLER_ROOT", "/opt/MediaCrawler"))
ALLOW_FAKE_IP_DNS = env_flag("ALLOW_FAKE_IP_DNS")
ALLOWED_HOSTS = {
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
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


def bridge_error(
    status_code: int,
    code: str,
    message: str,
    *,
    phase: str,
    retryable: bool,
) -> HTTPException:
    return HTTPException(
        status_code,
        {
            "code": code,
            "message": message,
            "phase": phase,
            "retryable": retryable,
        },
    )


def contract_error(message: str, phase: str) -> HTTPException:
    return bridge_error(
        502,
        "provider_contract_invalid",
        message,
        phase=phase,
        retryable=False,
    )


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


def state_path(platform: str) -> Path:
    return session_state_path(STATE_ROOT, platform)


def qr_path(platform: str) -> Path:
    return session_qr_path(STATE_ROOT, platform)


def provider_build_metadata() -> dict[str, str]:
    defaults = {
        "mediacrawler_commit": "unknown",
        "xhshow_version": "unknown",
        "xhs_sign_override_sha256": "unknown",
    }
    try:
        value = json.loads(PROVIDER_BUILD_METADATA.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(value, dict):
        return defaults
    return {
        key: str(value.get(key) or default)
        for key, default in defaults.items()
    }


def process_has_command(*expected_parts: str) -> bool:
    for command_path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = command_path.read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
        except OSError:
            continue
        if all(part in command for part in expected_parts):
            return True
    return False


def cleanup_stale_staging(now: float | None = None) -> int:
    """Remove abandoned discovery jobs and expired staged-content jobs.

    Discovery directories have no consumer after the bridge process restarts,
    while a completed staging directory may still be copied by the main service
    and therefore keeps the normal TTL.
    """
    current_time = datetime.now(timezone.utc).timestamp() if now is None else now
    removed = 0
    for candidate in STAGING_ROOT.iterdir():
        if not candidate.is_dir() or not re.fullmatch(
            r"(?:discover-)?[0-9a-f]{32}", candidate.name
        ):
            continue
        try:
            expired = candidate.name.startswith("discover-") or (
                current_time - candidate.stat().st_mtime
                >= STALE_STAGE_AGE_SECONDS
            )
        except OSError:
            continue
        if expired:
            shutil.rmtree(candidate, ignore_errors=True)
            if not candidate.exists():
                removed += 1
    return removed


def read_state(platform: str) -> dict:
    return read_session_state(
        state_path(platform),
        platform=platform,
        manual_verification_url=NOVNC_URL,
    )


def write_state(platform: str, status: str, message: str | None = None) -> dict:
    return write_session_state(
        state_path(platform),
        platform=platform,
        status=status,
        message=message,
        manual_verification_url=NOVNC_URL,
    )


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
    write_worker_request(
        output,
        platform=platform,
        mode=mode,
        value=value,
        limit=max(1, min(limit, 500)),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "/bridge/worker.py", mode, platform,
        "--output", str(output), "--qr", str(qr_path(platform)),
        "--browser-root", str(BROWSER_ROOT), "--state", str(state_path(platform)),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
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
            raise StageResourceLimitError(
                f"Provider staging exceeded the {max_bytes}-byte limit", 413
            )
        if free_bytes < min_free_bytes:
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
    except StageResourceLimitError:
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
    output_root = STATE_ROOT / f"login-{platform}"
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
        if process.returncode == 0:
            qr_path(platform).unlink(missing_ok=True)
            write_state(platform, "authenticated")
            return
        failure = worker_failure(output_root, output, "login")
        if failure["code"] == MANUAL_VERIFICATION_CODE:
            write_state(
                platform,
                "manual_verification_required",
                failure["message"],
            )
        elif failure["code"] == LOGIN_REQUIRED_CODE:
            write_state(platform, "expired", failure["message"])
        else:
            write_state(
                platform,
                "expired" if qr_path(platform).exists() else "error",
                failure["message"],
            )
    finally:
        shutil.rmtree(output_root, ignore_errors=True)
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


def worker_failure(root: Path, output: bytes, phase: str) -> dict:
    try:
        result = read_worker_result(root, expected_phase=phase)
    except ValueError as exc:
        return {
            "code": "worker_result_invalid",
            "message": str(exc),
            "phase": phase,
            "retryable": False,
        }
    if result is not None:
        return result
    return {
        "code": PROVIDER_EXECUTION_CODE,
        "message": safe_process_error(output, "Provider worker failed"),
        "phase": phase,
        "retryable": True,
    }


def raise_worker_failure(
    platform: str,
    root: Path,
    output: bytes,
    phase: str,
) -> None:
    failure = worker_failure(root, output, phase)
    if failure["code"] == MANUAL_VERIFICATION_CODE:
        write_state(platform, "manual_verification_required", failure["message"])
        status_code = 401
    elif failure["code"] == LOGIN_REQUIRED_CODE:
        write_state(platform, "expired", failure["message"])
        status_code = 401
    else:
        status_code = 502
    raise bridge_error(
        status_code,
        failure["code"],
        failure["message"],
        phase=failure["phase"],
        retryable=failure["retryable"],
    )


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
    if (
        not ALLOW_FAKE_IP_DNS
        and contains_proxy_fake_ip(resolved)
    ):
        raise HTTPException(
            422,
            "Platform URL resolved to a Clash/Mihomo Fake-IP; set "
            "ALLOW_FAKE_IP_DNS=true only when that proxy DNS mode is intentional",
        )
    if not resolved or any(
        not address_is_allowed(address, allow_fake_ip_dns=ALLOW_FAKE_IP_DNS)
        for address in resolved
    ):
        raise HTTPException(422, "Platform URL resolves to a non-public address")


async def creator_value(platform: str, value: HttpUrl) -> str:
    await validate_public_platform_url(platform, value)
    url = str(value)
    short_hosts = {
        "xiaohongshu": ("xhslink.com", "xhslink.cn"),
        "douyin": ("v.douyin.com",),
        "bilibili": ("b23.tv",),
    }
    platform_short_hosts = short_hosts.get(platform, ())
    if any(
        value.host == domain
        or str(value.host or "").endswith("." + domain)
        for domain in platform_short_hosts
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
    desktop = (
        "ok"
        if X11_SOCKET.exists()
        and process_has_command("x11vnc", "-rfbport", "5900")
        else "unavailable"
    )
    websocket_proxy = (
        "ok"
        if NOVNC_HTML.is_file()
        and process_has_command(
            "websockify",
            str(INTERNAL_NOVNC_PORT),
            "127.0.0.1:5900",
        )
        else "unavailable"
    )
    compatibility = verify_upstream_compatibility(MEDIACRAWLER_ROOT)
    if desktop != "ok" or websocket_proxy != "ok":
        raise HTTPException(503, "Interactive browser desktop is unavailable")
    if not compatibility["compatible"]:
        missing = [
            item
            for value in compatibility["platforms"].values()
            for item in value["missing"]
        ]
        raise bridge_error(
            503,
            "upstream_incompatible",
            "Pinned MediaCrawler compatibility check failed: " + ", ".join(missing),
            phase="startup",
            retryable=False,
        )
    build = provider_build_metadata()
    return {
        "status": "ok",
        "upstream": "MediaCrawler",
        "commit": build["mediacrawler_commit"],
        "xhshow_version": build["xhshow_version"],
        "xhs_sign_override_sha256": build["xhs_sign_override_sha256"],
        "desktop": desktop,
        "websocket_proxy": websocket_proxy,
        "fake_ip_dns_enabled": ALLOW_FAKE_IP_DNS,
        "platform_compatibility": {
            platform: value["compatible"]
            for platform, value in compatibility["platforms"].items()
        },
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
    if read_state(platform)["status"] != "authenticated":
        raise bridge_error(
            401,
            "login_required",
            "Platform session is not authenticated; scan the QR code or open manual verification",
            phase="session",
            retryable=True,
        )


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
                raise bridge_error(
                    504,
                    "discovery_timeout",
                    "Creator discovery timed out",
                    phase="discovery",
                    retryable=True,
                )
            if process.returncode:
                raise_worker_failure(
                    request.platform,
                    job,
                    output,
                    "discovery",
                )
            try:
                contract = provider_contract(
                    job,
                    expected_platform=request.platform,
                    expected_mode="discover",
                )
            except ValueError as exc:
                raise bridge_error(
                    502,
                    "provider_contract_invalid",
                    str(exc),
                    phase="discovery_contract",
                    retryable=False,
                ) from exc
            items, seen = [], set()
            for raw in jsonl_items(job):
                try:
                    item, _metadata = apply_provider_contract(
                        normalize(request.platform, raw), contract
                    )
                except (TypeError, ValueError) as exc:
                    raise contract_error(str(exc), "discovery_contract") from exc
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
                    raise contract_error(
                        "Provider contract has invalid content identities",
                        "discovery_contract",
                    )
                expected_original_ids.append(canonical_id)
                for identity in (canonical_id, *(str(alias).strip() for alias in aliases)):
                    if not identity:
                        raise contract_error(
                            "Provider contract has invalid content identities",
                            "discovery_contract",
                        )
                    owner = identity_owners.get(identity)
                    if owner is not None and owner != canonical_id:
                        raise contract_error(
                            "Provider contract has colliding content identities",
                            "discovery_contract",
                        )
                    identity_owners[identity] = canonical_id
            if (
                len(expected_original_ids) != len(set(expected_original_ids))
                or seen != set(expected_original_ids)
            ):
                raise contract_error(
                    "Provider contract and discovery records do not match",
                    "discovery_contract",
                )
            if not items:
                raise bridge_error(
                    502,
                    "provider_output_empty",
                    "Provider completed but returned no recognizable creator content",
                    phase="discovery_contract",
                    retryable=True,
                )
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
                raise bridge_error(
                    504,
                    "staging_timeout",
                    "Content staging timed out",
                    phase="staging",
                    retryable=True,
                )
            except StageResourceLimitError as exc:
                raise bridge_error(
                    exc.status_code,
                    "staging_resource_limit",
                    str(exc),
                    phase="staging",
                    retryable=True,
                ) from exc
            if process.returncode:
                raise_worker_failure(
                    request.platform,
                    job,
                    output,
                    "staging",
                )
            raw_items = jsonl_items(job)
            if not raw_items:
                raise bridge_error(
                    502,
                    "provider_output_empty",
                    "Provider returned no content detail",
                    phase="staging_contract",
                    retryable=True,
                )
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
                raise contract_error(str(exc), "staging_contract") from exc
            if not contract_identity_matches(request.content_id, item):
                raise contract_error(
                    "Provider returned content that does not match the requested id",
                    "staging_contract",
                )
            if not item["original"]:
                raise contract_error(
                    "Provider returned a repost for an original-content request",
                    "staging_contract",
                )

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
