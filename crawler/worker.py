from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import hashlib
import ipaddress
import json
import os
import random
import signal
import socket
import sys
import re
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode, urljoin, urlparse

import httpx


PLATFORM_CONFIG = {
    "xiaohongshu": ("xhs", "XHS_CREATOR_ID_LIST"),
    "douyin": ("dy", "DY_CREATOR_ID_LIST"),
    "bilibili": ("bili", "BILI_CREATOR_ID_LIST"),
    "weibo": ("wb", "WEIBO_CREATOR_ID_LIST"),
}
PROVIDER_CONTRACT_FILENAME = "bridge-contract.json"
MAX_STAGE_FILES = int(os.getenv("MAX_STAGE_FILES", "100"))
MAX_STAGE_BYTES = int(os.getenv("MAX_STAGE_BYTES", str(2 * 1024**3)))


def write_qr_image(path: Path, value: str | bytes) -> None:
    """Decode MediaCrawler's QR payload and publish it atomically for the bridge."""
    raw = value.decode("ascii") if isinstance(value, bytes) else str(value)
    raw = raw.split(",", 1)[-1]
    image = base64.b64decode("".join(raw.split()), validate=True)
    if not image:
        raise ValueError("MediaCrawler returned an empty QR code")

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(image)
    os.replace(temporary, path)


def install_qr_sink(path: Path) -> None:
    from tools import crawler_util, utils

    def save_qr(value: str | bytes) -> None:
        write_qr_image(path, value)

    crawler_util.show_qrcode = save_qr
    utils.show_qrcode = save_qr


BrowserMethod = Callable[..., Awaitable[Any]]
SLIDER_MANUAL_VERIFICATION_MESSAGE = (
    "Manual verification required: slider CAPTCHA detected; "
    "open the local noVNC session to complete it; automatic slider solving is disabled"
)
NOVNC_PORT = int(os.getenv("NOVNC_PORT", "7900"))
NOVNC_BIND_ADDRESS = os.getenv("NOVNC_BIND_ADDRESS", "127.0.0.1").strip() or "127.0.0.1"
NOVNC_URL_HOST = (
    f"[{NOVNC_BIND_ADDRESS}]" if ":" in NOVNC_BIND_ADDRESS else NOVNC_BIND_ADDRESS
)
NOVNC_URL = f"http://{NOVNC_URL_HOST}:{NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale"


def _with_system_browser(original: BrowserMethod, executable_path: str) -> BrowserMethod:
    """Use the packaged Chromium when upstream does not select a browser itself."""
    @functools.wraps(original)
    async def wrapped(self, *args, **kwargs):
        if not kwargs.get("channel") and not kwargs.get("executable_path"):
            kwargs["executable_path"] = executable_path
        return await original(self, *args, **kwargs)

    setattr(wrapped, "_archive_system_browser", True)
    return wrapped


def install_system_browser_launcher(executable_path: str) -> None:
    """Patch Playwright once so all MediaCrawler platform implementations use Chromium.

    The pinned MediaCrawler version passes ``channel='chrome'`` for Bilibili and
    Weibo, but Xiaohongshu and Douyin rely on Playwright's downloaded browser.
    The crawler image intentionally packages Debian Chromium instead, so inject
    that executable only when upstream did not make an explicit selection.
    """
    browser = Path(executable_path)
    if not browser.is_file():
        raise RuntimeError(f"Configured Chromium executable does not exist: {browser}")

    from playwright.async_api import BrowserType

    for method_name in ("launch", "launch_persistent_context"):
        original = getattr(BrowserType, method_name)
        if getattr(original, "_archive_system_browser", False):
            continue
        setattr(BrowserType, method_name, _with_system_browser(original, str(browser)))


async def _reject_automatic_slider(*_args, **_kwargs) -> None:
    raise RuntimeError(SLIDER_MANUAL_VERIFICATION_MESSAGE)


def write_worker_state(path: Path, platform: str, status: str, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "platform": platform,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "manual_verification_url": NOVNC_URL,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_douyin_slider_guard(
    login_class: type | None = None,
    *,
    state_path: Path | None = None,
    platform: str = "douyin",
    timeout_seconds: float = 480,
    poll_interval: float = 1,
) -> None:
    """Disable automatic slider movement while leaving the browser usable.

    The pinned upstream invokes ``check_page_display_slider`` only after it has
    decided a slider flow needs handling during QR login. Replacing both that
    entry point and the lower-level movement method prevents image recognition
    and mouse automation. The replacement publishes an actionable state and
    waits for the administrator to finish verification in noVNC.
    """
    if login_class is None:
        from media_platform.douyin.login import DouYinLogin

        login_class = DouYinLogin

    async def wait_for_manual_verification(self, *_args, **_kwargs) -> None:
        if state_path is not None:
            write_worker_state(
                state_path,
                platform,
                "manual_verification_required",
                "请打开本机 noVNC 手动完成滑块验证；浏览器会在等待期间保持打开",
            )
        checker = getattr(self, "check_login_state", None)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if checker is not None:
                try:
                    if await checker():
                        return
                except Exception:
                    # Navigation and cookie reads can transiently fail while
                    # the administrator is interacting with the page.
                    pass
            await asyncio.sleep(poll_interval)
        if state_path is not None:
            write_worker_state(
                state_path,
                platform,
                "expired",
                "人工验证等待超时，请重新发起登录",
            )
        raise RuntimeError("Human confirmation window expired")

    login_class.check_page_display_slider = wait_for_manual_verification
    login_class.move_slider = _reject_automatic_slider


def profile_is_in_use(profile: Path) -> bool:
    marker = str(profile).encode()
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            value = cmdline.read_bytes()
        except OSError:
            continue
        if marker in value and (b"chromium" in value.lower() or b"chrome" in value.lower()):
            return True
    return False


def clear_chromium_singleton_files(profile: Path) -> None:
    """Remove Chromium's process-coordination artifacts, never profile data."""
    if profile_is_in_use(profile):
        raise RuntimeError(f"Browser profile is still in use: {profile}")
    profile.mkdir(parents=True, exist_ok=True)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / name).unlink(missing_ok=True)
        except OSError:
            pass


def normalize_stage_value(platform: str, value: str) -> str:
    patterns = {
        "douyin": r"/(?:video|note)/(\d+)",
        "weibo": r"/(?:detail|status)/([^/?#]+)",
    }
    pattern = patterns.get(platform)
    if pattern:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return value


def _non_empty_csv(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _enabled_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _write_provider_contract(output: Path, contract: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    destination = output / PROVIDER_CONTRACT_FILENAME
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    payload = {"schema_version": 1, **contract}
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _record_contract_item(
    contract: dict[str, Any],
    provider_id: Any,
    *,
    canonical_id: Any | None = None,
    source_url: str,
    original: bool,
    content_type: str,
    media_sources: list[tuple[str, str]],
    unsupported_media: bool = False,
    pinned: bool = False,
    aliases: list[Any] | None = None,
    slot_ids: list[str] | None = None,
) -> None:
    provider_key = str(provider_id or "").strip()
    canonical_key = str(canonical_id or provider_key).strip()
    if not provider_key or not canonical_key:
        return
    normalized_aliases = []
    for alias in aliases or []:
        value = str(alias or "").strip()
        if value and value != canonical_key and value not in normalized_aliases:
            normalized_aliases.append(value)
    kind_counts: dict[str, int] = {}
    media_slots: list[dict[str, Any]] = []
    for ordinal, (kind, source_url_value) in enumerate(media_sources, start=1):
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        slot_id = (
            slot_ids[ordinal - 1]
            if slot_ids is not None and ordinal <= len(slot_ids)
            else f"{kind}-{kind_counts[kind]:03d}"
        )
        slot: dict[str, Any] = {
            "ordinal": ordinal,
            "kind": kind,
            "slot_id": slot_id,
        }
        if source_url_value:
            slot["source_sha256"] = hashlib.sha256(
                source_url_value.encode("utf-8")
            ).hexdigest()
        media_slots.append(slot)
    contract.setdefault("items", {})[provider_key] = {
        "provider_id": provider_key,
        "canonical_id": canonical_key,
        "source_url": source_url,
        "original": bool(original),
        "content_type": content_type,
        "expected_media_count": len(media_slots),
        "media_slots": media_slots,
        "unsupported_media": bool(unsupported_media),
        "pinned": bool(pinned),
        "aliases": normalized_aliases,
    }


def _set_contract_media(
    contract: dict[str, Any],
    provider_id: Any,
    media_slots: list[dict[str, Any]],
    *,
    unsupported_media: bool,
) -> None:
    item = contract.get("items", {}).get(str(provider_id or ""))
    if not isinstance(item, dict):
        return
    item["expected_media_count"] = len(media_slots)
    item["media_slots"] = media_slots
    item["unsupported_media"] = bool(unsupported_media)


def _bind_contract_media_path(
    contract: dict[str, Any],
    provider_id: Any,
    *,
    kind: str,
    source_url: str,
    staged_path: str,
) -> bool:
    """Bind one successful download to its exact expected contract slot."""
    item = contract.get("items", {}).get(str(provider_id or ""))
    if not isinstance(item, dict):
        return False
    source_sha256 = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    for slot in item.get("media_slots") or []:
        if (
            isinstance(slot, dict)
            and slot.get("kind") == kind
            and slot.get("source_sha256") == source_sha256
            and not slot.get("staged_path")
        ):
            slot["staged_path"] = Path(staged_path).as_posix()
            return True
    return False


def _xhs_media_sources(note_item: dict[str, Any]) -> tuple[list[str], list[str]]:
    images: list[str] = []
    for image in note_item.get("image_list") or []:
        if not isinstance(image, dict):
            continue
        url_default = image.get("url_default")
        url = url_default if url_default != "" else image.get("url")
        if url:
            images.append(str(url))
    return images, []


async def _validate_public_media_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider media URL is not HTTP(S)")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await asyncio.to_thread(
        socket.getaddrinfo, parsed.hostname, port, 0, socket.SOCK_STREAM
    )
    resolved = {
        ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in addresses
    }
    if not resolved or any(not address.is_global for address in resolved):
        raise ValueError("Provider media URL resolves to a non-public address")


async def _stream_http_media(
    client: httpx.AsyncClient,
    url: str,
    destination: Path,
    *,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
) -> tuple[bool, int, bool]:
    """Stream one signed CDN object with per-hop SSRF and byte-limit checks."""
    if max_bytes <= 0:
        return False, 0, True
    current = url
    for _redirect in range(6):
        try:
            await _validate_public_media_url(current)
            async with client.stream(
                "GET", current, headers=headers, timeout=timeout
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return False, 0, False
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            return False, 0, True
                    except ValueError:
                        pass
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
                written = 0
                try:
                    with temporary.open("wb") as target:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if not chunk:
                                continue
                            if written + len(chunk) > max_bytes:
                                return False, written, True
                            target.write(chunk)
                            written += len(chunk)
                    if written <= 0:
                        return False, 0, False
                    os.replace(temporary, destination)
                    return True, written, False
                finally:
                    temporary.unlink(missing_ok=True)
        except (httpx.HTTPError, OSError, ValueError):
            return False, 0, False
    return False, 0, False


def _weibo_picture_url(picture: Any) -> str:
    if isinstance(picture, str):
        return picture.strip()
    if not isinstance(picture, dict):
        return ""
    large = picture.get("large") or {}
    large_url = large.get("url") if isinstance(large, dict) else ""
    return str(large_url or picture.get("url") or "").strip()


def _weibo_media_contract(
    mblog: dict[str, Any],
) -> tuple[list[tuple[str, str]], bool]:
    pictures: list[tuple[str, str]] = []
    for picture in mblog.get("pics") or []:
        url = _weibo_picture_url(picture)
        if url:
            pictures.append(("image", url))
    page_info = mblog.get("page_info") or {}
    media_info = page_info.get("media_info") or {} if isinstance(page_info, dict) else {}
    unsupported_video = bool(
        isinstance(page_info, dict)
        and (
            str(page_info.get("type") or "").lower() in {"video", "live"}
            or any(
                media_info.get(key)
                for key in (
                    "stream_url",
                    "stream_url_hd",
                    "h5_url",
                    "mp4_sd_url",
                    "mp4_hd_url",
                )
            )
        )
    )
    return pictures, unsupported_video


def install_provider_contract(args, crawler, config, contract: dict[str, Any]) -> None:
    """Capture the pinned provider's raw media contract before JSONL drops fields.

    The bridge intentionally fails closed when this side manifest is absent. The
    wrappers below are pinned-commit integration code, not heuristic parsing of
    the normalized JSONL output.
    """

    if args.platform == "xiaohongshu":
        import store.xhs as xhs_store

        original = xhs_store.update_xhs_note

        async def update_xhs_note(note_item: dict[str, Any]):
            image_urls, _ = _xhs_media_sources(note_item)
            video_urls = [
                str(url) for url in xhs_store.get_video_url_arr(note_item) if url
            ]
            provider_id = note_item.get("note_id")
            xsec_token = str(note_item.get("xsec_token") or "").strip()
            source_url = f"https://www.xiaohongshu.com/explore/{provider_id}"
            if xsec_token:
                source_url += "?" + urlencode(
                    {
                        "xsec_token": xsec_token,
                        "xsec_source": str(note_item.get("xsec_source") or "pc_search"),
                    }
                )
            _record_contract_item(
                contract,
                provider_id,
                source_url=source_url,
                original=True,
                content_type=(
                    "video" if video_urls else "image" if image_urls else "unknown"
                ),
                media_sources=[
                    *(("image", url) for url in image_urls),
                    *(("video", url) for url in video_urls),
                ],
                pinned=_enabled_flag(note_item.get("is_top"))
                or _enabled_flag(note_item.get("is_pinned")),
            )
            await original(note_item)

        xhs_store.update_xhs_note = update_xhs_note

        if args.mode == "stage":
            async def get_bound_xhs_images(self, note_item: dict[str, Any]):
                if not config.ENABLE_GET_MEIDAS:
                    return
                note_id = str(note_item.get("note_id") or "")
                image_urls, _ = _xhs_media_sources(note_item)
                file_number = 0
                for url in image_urls:
                    content = await self.xhs_client.get_note_media(url)
                    await asyncio.sleep(random.random())
                    if content is None:
                        continue
                    extension_file_name = f"{file_number}.jpg"
                    file_number += 1
                    await xhs_store.update_xhs_note_image(
                        note_id, content, extension_file_name
                    )
                    _bind_contract_media_path(
                        contract,
                        note_id,
                        kind="image",
                        source_url=url,
                        staged_path=(
                            Path("xhs") / "images" / note_id / extension_file_name
                        ).as_posix(),
                    )

            async def get_bound_xhs_video(self, note_item: dict[str, Any]):
                if not config.ENABLE_GET_MEIDAS:
                    return
                note_id = str(note_item.get("note_id") or "")
                video_urls = [
                    str(url) for url in xhs_store.get_video_url_arr(note_item) if url
                ]
                file_number = 0
                for url in video_urls:
                    content = await self.xhs_client.get_note_media(url)
                    await asyncio.sleep(random.random())
                    if content is None:
                        continue
                    extension_file_name = f"{file_number}.mp4"
                    file_number += 1
                    await xhs_store.update_xhs_note_video(
                        note_id, content, extension_file_name
                    )
                    _bind_contract_media_path(
                        contract,
                        note_id,
                        kind="video",
                        source_url=url,
                        staged_path=(
                            Path("xhs") / "videos" / note_id / extension_file_name
                        ).as_posix(),
                    )

            crawler.get_note_images = MethodType(get_bound_xhs_images, crawler)
            crawler.get_notice_video = MethodType(get_bound_xhs_video, crawler)

        if args.mode == "discover":
            from media_platform.xhs.client import XiaoHongShuClient

            original_get_notes = XiaoHongShuClient.get_notes_by_creator
            selected_count = 0

            async def tracked_get_notes(client_self, *call_args, **call_kwargs):
                nonlocal selected_count
                response = await original_get_notes(client_self, *call_args, **call_kwargs)
                if not isinstance(response, dict) or not isinstance(response.get("notes"), list):
                    raise RuntimeError("Xiaohongshu creator response has an unknown structure")
                remaining = max(0, config.CRAWLER_MAX_NOTES_COUNT - selected_count)
                notes = response["notes"]
                selected_count += min(remaining, len(notes))
                contract.setdefault("discovery", {})["truncated"] = bool(
                    response.get("has_more") or len(notes) > remaining
                )
                return response

            XiaoHongShuClient.get_notes_by_creator = tracked_get_notes

    elif args.platform == "douyin":
        import store.douyin as douyin_store

        original = douyin_store.update_douyin_aweme

        async def update_douyin_aweme(aweme_item: dict[str, Any]):
            image_urls = [
                str(url)
                for url in douyin_store._extract_note_image_list(aweme_item)
                if url
            ]
            video_url = str(
                douyin_store._extract_video_download_url(aweme_item) or ""
            )
            media_sources = (
                [("image", url) for url in image_urls]
                if image_urls
                else ([('video', video_url)] if video_url else [])
            )
            provider_id = aweme_item.get("aweme_id")
            source_kind = "note" if image_urls else "video"
            _record_contract_item(
                contract,
                provider_id,
                source_url=f"https://www.douyin.com/{source_kind}/{provider_id}",
                original=not (
                    _enabled_flag(aweme_item.get("is_repost"))
                    or _enabled_flag(aweme_item.get("is_forward"))
                ),
                content_type="image" if image_urls else "video" if video_url else "unknown",
                media_sources=media_sources,
                pinned=_enabled_flag(aweme_item.get("is_top"))
                or _enabled_flag(aweme_item.get("is_pinned")),
            )
            await original(aweme_item)

        douyin_store.update_douyin_aweme = update_douyin_aweme

        if args.mode == "stage":
            async def get_bound_douyin_images(self, aweme_item: dict[str, Any]):
                if not config.ENABLE_GET_MEIDAS:
                    return
                aweme_id = str(aweme_item.get("aweme_id") or "")
                image_urls = [
                    str(url)
                    for url in douyin_store._extract_note_image_list(aweme_item)
                    if url
                ]
                file_number = 0
                for url in image_urls:
                    content = await self.dy_client.get_aweme_media(url)
                    await asyncio.sleep(random.random())
                    if content is None:
                        continue
                    extension_file_name = f"{file_number:>03d}.jpeg"
                    file_number += 1
                    await douyin_store.update_dy_aweme_image(
                        aweme_id, content, extension_file_name
                    )
                    _bind_contract_media_path(
                        contract,
                        aweme_id,
                        kind="image",
                        source_url=url,
                        staged_path=(
                            Path("douyin")
                            / "images"
                            / aweme_id
                            / extension_file_name
                        ).as_posix(),
                    )

            async def get_bound_douyin_video(self, aweme_item: dict[str, Any]):
                if not config.ENABLE_GET_MEIDAS:
                    return
                aweme_id = str(aweme_item.get("aweme_id") or "")
                video_url = str(
                    douyin_store._extract_video_download_url(aweme_item) or ""
                )
                if not video_url:
                    return
                content = await self.dy_client.get_aweme_media(video_url)
                await asyncio.sleep(random.random())
                if content is None:
                    return
                extension_file_name = "video.mp4"
                await douyin_store.update_dy_aweme_video(
                    aweme_id, content, extension_file_name
                )
                _bind_contract_media_path(
                    contract,
                    aweme_id,
                    kind="video",
                    source_url=video_url,
                    staged_path=(
                        Path("douyin")
                        / "videos"
                        / aweme_id
                        / extension_file_name
                    ).as_posix(),
                )

            crawler.get_aweme_images = MethodType(get_bound_douyin_images, crawler)
            crawler.get_aweme_video = MethodType(get_bound_douyin_video, crawler)

        if args.mode == "discover":
            from media_platform.douyin.client import DouYinClient

            async def get_limited_user_aweme_posts(client_self, sec_user_id: str, callback=None):
                result: list[dict[str, Any]] = []
                seen_ids: set[str] = set()
                cursor = ""
                seen_cursors: set[str] = set()
                truncated = False
                while len(result) < config.CRAWLER_MAX_NOTES_COUNT:
                    response = await client_self.get_user_aweme_posts(sec_user_id, cursor)
                    if not isinstance(response, dict) or not isinstance(
                        response.get("aweme_list"), list
                    ):
                        raise RuntimeError("Douyin creator response has an unknown structure")
                    page_items: list[dict[str, Any]] = []
                    for aweme in response["aweme_list"]:
                        if not isinstance(aweme, dict) or not str(aweme.get("aweme_id") or ""):
                            raise RuntimeError("Douyin creator response contains an item without an id")
                        remote_id = str(aweme["aweme_id"])
                        is_original = not (
                            _enabled_flag(aweme.get("is_repost"))
                            or _enabled_flag(aweme.get("is_forward"))
                        )
                        if is_original and remote_id not in seen_ids:
                            seen_ids.add(remote_id)
                            page_items.append(aweme)
                    remaining = config.CRAWLER_MAX_NOTES_COUNT - len(result)
                    selected = page_items[:remaining]
                    if callback and selected:
                        await callback(selected)
                    result.extend(selected)
                    has_more = response.get("has_more") in {1, True, "1"}
                    next_cursor = str(response.get("max_cursor") or "")
                    truncated = len(page_items) > len(selected) or (
                        len(result) >= config.CRAWLER_MAX_NOTES_COUNT and has_more
                    )
                    if truncated or not has_more:
                        break
                    if not response["aweme_list"] or not next_cursor or next_cursor in seen_cursors:
                        raise RuntimeError("Douyin creator pagination did not advance")
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                contract.setdefault("discovery", {})["truncated"] = truncated
                return result

            DouYinClient.get_all_user_aweme_posts = get_limited_user_aweme_posts

    elif args.platform == "bilibili":
        import store.bilibili as bilibili_store
        from media_platform.bilibili.help import parse_video_info_from_url

        original = bilibili_store.update_bilibili_video

        async def update_bilibili_video(video_item: dict[str, Any]):
            view = video_item.get("View") or {}
            aid = view.get("aid")
            bvid = str(view.get("bvid") or "").strip()
            canonical_id = bvid or (f"av{aid}" if aid else "")
            pages = view.get("pages")
            expected_pages = len(pages) if isinstance(pages, list) and pages else 1
            _record_contract_item(
                contract,
                aid,
                canonical_id=canonical_id,
                source_url=f"https://www.bilibili.com/video/{canonical_id}",
                original=view.get("copyright") == 1,
                content_type="video",
                media_sources=[("video", "")] * expected_pages,
                slot_ids=[
                    f"video-p{page_number:03d}"
                    for page_number in range(1, expected_pages + 1)
                ],
                pinned=_enabled_flag(view.get("is_top"))
                or _enabled_flag(view.get("is_pinned")),
                aliases=[aid],
            )
            await original(video_item)

        bilibili_store.update_bilibili_video = update_bilibili_video

        async def get_complete_bilibili_video(self, video_item: dict[str, Any], semaphore):
            if not config.ENABLE_GET_MEIDAS:
                return
            view = video_item.get("View") or {}
            aid = view.get("aid")
            raw_pages = view.get("pages")
            if isinstance(raw_pages, list) and raw_pages:
                page_cids = [page.get("cid") if isinstance(page, dict) else None for page in raw_pages]
            else:
                page_cids = [view.get("cid")]

            output_root = Path(config.SAVE_DATA_PATH)
            try:
                existing_bytes = sum(
                    path.stat().st_size
                    for path in output_root.rglob("*")
                    if path.is_file() and not path.is_symlink()
                )
            except OSError:
                existing_bytes = MAX_STAGE_BYTES
            remaining_bytes = max(0, MAX_STAGE_BYTES - existing_bytes)
            media_slots: list[dict[str, Any]] = []
            unsupported_media = False
            file_number = 0

            media_headers = {
                key: value
                for key, value in self.bili_client.headers.items()
                if key.lower() not in {"authorization", "cookie"}
            }
            async with httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
            ) as media_client:
                for page_number, raw_cid in enumerate(page_cids, start=1):
                    try:
                        cid = int(raw_cid)
                    except (TypeError, ValueError):
                        media_slots.append(
                            {
                                "ordinal": len(media_slots) + 1,
                                "kind": "video",
                                "slot_id": f"video-p{page_number:03d}-segment001",
                            }
                        )
                        unsupported_media = True
                        continue
                    play_result = await self.get_video_play_url_task(aid, cid, semaphore)
                    durls = play_result.get("durl") if isinstance(play_result, dict) else None
                    if not isinstance(durls, list) or not durls:
                        media_slots.append(
                            {
                                "ordinal": len(media_slots) + 1,
                                "kind": "video",
                                "slot_id": f"video-p{page_number:03d}-segment001",
                            }
                        )
                        unsupported_media = True
                        continue
                    for segment_number, segment in enumerate(durls, start=1):
                        file_number += 1
                        url = str(segment.get("url") or "") if isinstance(segment, dict) else ""
                        slot: dict[str, Any] = {
                            "ordinal": len(media_slots) + 1,
                            "kind": "video",
                            "slot_id": (
                                f"video-p{page_number:03d}-segment{segment_number:03d}"
                            ),
                        }
                        if url:
                            slot["source_sha256"] = hashlib.sha256(
                                url.encode("utf-8")
                            ).hexdigest()
                        media_slots.append(slot)
                        if not url or file_number > MAX_STAGE_FILES:
                            unsupported_media = True
                            continue
                        destination = (
                            output_root
                            / "bili"
                            / "videos"
                            / str(aid)
                            / f"p{page_number:03d}-segment{segment_number:03d}.mp4"
                        )
                        downloaded, written, limit_exceeded = await _stream_http_media(
                            media_client,
                            url,
                            destination,
                            headers=media_headers,
                            timeout=self.bili_client.timeout,
                            max_bytes=remaining_bytes,
                        )
                        if downloaded:
                            remaining_bytes -= written
                            slot["staged_path"] = destination.relative_to(
                                output_root
                            ).as_posix()
                        else:
                            unsupported_media = True
                        if limit_exceeded:
                            remaining_bytes = 0
                    await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)

            if not media_slots:
                media_slots = [
                    {
                        "ordinal": 1,
                        "kind": "video",
                        "slot_id": "video-p001-segment001",
                    }
                ]
                unsupported_media = True
            _set_contract_media(
                contract,
                aid,
                media_slots,
                unsupported_media=unsupported_media,
            )

        crawler.get_bilibili_video = MethodType(get_complete_bilibili_video, crawler)

        async def get_specified_videos(self, video_values: list[str]):
            identities: list[tuple[int, str]] = []
            for value in video_values:
                text = str(value)
                aid_match = re.search(r"(?:/video/)?av(?P<aid>\d+)", text, re.IGNORECASE)
                if aid_match:
                    identities.append((int(aid_match.group("aid")), ""))
                    continue
                try:
                    identities.append((0, parse_video_info_from_url(text).video_id))
                except ValueError:
                    continue
            semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
            details = await asyncio.gather(
                *(
                    self.get_video_info_task(aid=aid, bvid=bvid, semaphore=semaphore)
                    for aid, bvid in identities
                )
            )
            aids: list[str] = []
            for detail in details:
                if not detail:
                    continue
                view = detail.get("View") or {}
                if view.get("aid"):
                    aids.append(str(view["aid"]))
                await bilibili_store.update_bilibili_video(detail)
                await bilibili_store.update_up_info(detail)
                await self.get_bilibili_video(detail, semaphore)
            await self.batch_get_video_comments(aids)

        crawler.get_specified_videos = MethodType(get_specified_videos, crawler)

        if args.mode == "discover":
            async def get_creator_videos(self, creator_id: int):
                page_size = 30
                page = 1
                fetched = 0
                total = 0
                seen_bvids: set[str] = set()
                truncated = False
                while fetched < config.CRAWLER_MAX_NOTES_COUNT:
                    result = await self.bili_client.get_creator_videos(creator_id, page, page_size)
                    if not isinstance(result, dict):
                        raise RuntimeError("Bilibili creator response has an unknown structure")
                    raw_videos = result.get("list", {}).get("vlist")
                    total_value = result.get("page", {}).get("count")
                    if not isinstance(raw_videos, list) or total_value is None:
                        raise RuntimeError("Bilibili creator response has an unknown structure")
                    if any(
                        not isinstance(video, dict) or not str(video.get("bvid") or "")
                        for video in raw_videos
                    ):
                        raise RuntimeError("Bilibili creator response contains a video without a BV id")
                    videos = []
                    for video in raw_videos:
                        bvid = str(video["bvid"])
                        if bvid not in seen_bvids:
                            seen_bvids.add(bvid)
                            videos.append(video)
                    total = int(total_value)
                    remaining = config.CRAWLER_MAX_NOTES_COUNT - fetched
                    selected = videos[:remaining]
                    await self.get_specified_videos(
                        [str(video.get("bvid") or "") for video in selected if video.get("bvid")]
                    )
                    fetched += len(selected)
                    has_more_pages = page * page_size < total
                    truncated = len(selected) < len(videos) or (
                        fetched >= config.CRAWLER_MAX_NOTES_COUNT and has_more_pages
                    )
                    if truncated or not has_more_pages or not raw_videos:
                        break
                    page += 1
                    await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                contract.setdefault("discovery", {})["truncated"] = truncated

            crawler.get_creator_videos = MethodType(get_creator_videos, crawler)

    elif args.platform == "weibo":
        import store.weibo as weibo_store

        original = weibo_store.update_weibo_note

        async def update_weibo_note(note_item: dict[str, Any]):
            mblog = note_item.get("mblog") or {}
            provider_id = mblog.get("id")
            bid = mblog.get("bid")
            media_sources, unsupported_video = _weibo_media_contract(mblog)
            _record_contract_item(
                contract,
                provider_id,
                source_url=f"https://m.weibo.cn/detail/{provider_id}",
                original=not bool(mblog.get("retweeted_status")),
                content_type=(
                    "video"
                    if unsupported_video
                    else "image"
                    if media_sources
                    else "text"
                ),
                media_sources=media_sources,
                unsupported_media=unsupported_video,
                pinned=_enabled_flag(mblog.get("isTop"))
                or _enabled_flag(mblog.get("is_top")),
                aliases=[bid],
            )
            await original(note_item)

        weibo_store.update_weibo_note = update_weibo_note

        if args.mode == "stage":
            async def get_bound_weibo_images(self, mblog: dict[str, Any]):
                if not config.ENABLE_GET_MEIDAS:
                    return
                provider_id = str(mblog.get("id") or "")
                pictures = mblog.get("pics") or []
                for picture in pictures:
                    if isinstance(picture, str):
                        url = picture
                        pic_id = urlparse(url).path.rsplit("/", 1)[-1].split(".", 1)[0]
                    elif isinstance(picture, dict):
                        url = str(picture.get("url") or "")
                        pic_id = str(picture.get("pid") or "")
                    else:
                        continue
                    if not url:
                        continue
                    content = await self.wb_client.get_note_image(url)
                    await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                    if content is None:
                        continue
                    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
                    extension_file_name = (
                        suffix if re.fullmatch(r"[a-z0-9]{1,8}", suffix) else "jpg"
                    )
                    await weibo_store.update_weibo_note_image(
                        pic_id, content, extension_file_name
                    )
                    _bind_contract_media_path(
                        contract,
                        provider_id,
                        kind="image",
                        source_url=url,
                        staged_path=(
                            Path("weibo")
                            / "images"
                            / f"{pic_id}.{extension_file_name}"
                        ).as_posix(),
                    )

            if hasattr(weibo_store, "update_weibo_note_image"):
                crawler.get_note_images = MethodType(get_bound_weibo_images, crawler)

            async def get_specified_notes(self):
                semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                details = await asyncio.gather(
                    *(
                        self.get_note_info_task(note_id=note_id, semaphore=semaphore)
                        for note_id in config.WEIBO_SPECIFIED_ID_LIST
                    )
                )
                note_ids: list[str] = []
                for note_item in details:
                    if not note_item:
                        continue
                    mblog = note_item.get("mblog") or {}
                    if mblog.get("id"):
                        note_ids.append(str(mblog["id"]))
                    await weibo_store.update_weibo_note(note_item)
                    download_mblog = {**mblog}
                    download_pictures = []
                    for picture in mblog.get("pics") or []:
                        url = _weibo_picture_url(picture)
                        if not url:
                            continue
                        if isinstance(picture, dict):
                            download_pictures.append({"url": url, "pid": picture.get("pid", "")})
                        else:
                            download_pictures.append(url)
                    download_mblog["pics"] = download_pictures
                    await self.get_note_images(download_mblog)
                await self.batch_get_notes_comments(note_ids)

            crawler.get_specified_notes = MethodType(get_specified_notes, crawler)

        if args.mode == "discover":
            from media_platform.weibo.client import WeiboClient

            async def get_limited_creator_notes(
                client_self,
                creator_id: str,
                container_id: str,
                crawl_interval: float = 1.0,
                callback=None,
            ):
                result: list[dict[str, Any]] = []
                since_id = ""
                seen_ids: set[str] = set()
                seen_cursors: set[str] = set()
                truncated = False
                while len(result) < config.CRAWLER_MAX_NOTES_COUNT:
                    response = await client_self.get_notes_by_creator(
                        creator_id, container_id, since_id
                    )
                    if not isinstance(response, dict) or not isinstance(
                        response.get("cards"), list
                    ):
                        raise RuntimeError("Weibo creator response has an unknown structure")
                    info = response.get("cardlistInfo") or {}
                    next_since_id = str(info.get("since_id") or "0")
                    notes: list[dict[str, Any]] = []
                    for card in response["cards"]:
                        if not isinstance(card, dict) or card.get("card_type") != 9:
                            continue
                        mblog = card.get("mblog")
                        remote_id = str(mblog.get("id") or "") if isinstance(mblog, dict) else ""
                        if not remote_id:
                            raise RuntimeError("Weibo creator response contains a post without an id")
                        if not mblog.get("retweeted_status") and remote_id not in seen_ids:
                            seen_ids.add(remote_id)
                            notes.append(card)
                    remaining = config.CRAWLER_MAX_NOTES_COUNT - len(result)
                    selected = notes[:remaining]
                    if callback and selected:
                        await callback(selected)
                    result.extend(selected)
                    truncated = len(notes) > len(selected) or (
                        len(result) >= config.CRAWLER_MAX_NOTES_COUNT
                        and next_since_id not in {"", "0"}
                    )
                    if truncated or next_since_id in {"", "0"}:
                        break
                    if next_since_id == since_id or next_since_id in seen_cursors:
                        raise RuntimeError("Weibo creator pagination did not advance")
                    seen_cursors.add(next_since_id)
                    since_id = next_since_id
                    await asyncio.sleep(crawl_interval)
                contract.setdefault("discovery", {})["truncated"] = truncated
                return result

            WeiboClient.get_all_notes_by_creator_id = get_limited_creator_notes


async def run(args) -> None:
    system_browser = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/usr/bin/chromium")
    install_system_browser_launcher(system_browser)
    sys.path.insert(0, "/opt/MediaCrawler")
    os.chdir("/opt/MediaCrawler")
    import config
    import main as mediacrawler_main

    upstream, creator_key = PLATFORM_CONFIG[args.platform]
    if args.platform == "douyin":
        install_douyin_slider_guard(
            state_path=Path(args.state),
            platform=args.platform,
            timeout_seconds=float(os.getenv("MANUAL_VERIFICATION_TIMEOUT_SECONDS", "480")),
        )
    config.PLATFORM = upstream
    config.LOGIN_TYPE = "qrcode"
    config.CRAWLER_TYPE = "creator" if args.mode in {"login", "discover"} else "detail"
    config.ENABLE_IP_PROXY = False
    config.HEADLESS = False
    config.CDP_HEADLESS = False
    config.ENABLE_CDP_MODE = False
    config.CDP_CONNECT_EXISTING = False
    config.CUSTOM_BROWSER_PATH = system_browser
    config.AUTO_CLOSE_BROWSER = True
    config.SAVE_LOGIN_STATE = True
    # Upstream formats USER_DATA_DIR with its short platform key. Keeping the
    # placeholder below yields one persistent profile beneath each full-name
    # platform directory on the shared data volume.
    config.USER_DATA_DIR = str(Path(args.browser_root) / args.platform / "%s")
    profile = Path(config.USER_DATA_DIR % upstream)
    clear_chromium_singleton_files(profile)
    config.SAVE_DATA_OPTION = "jsonl"
    config.SAVE_DATA_PATH = args.output
    config.ENABLE_GET_MEIDAS = args.mode == "stage"
    config.ENABLE_GET_COMMENTS = False
    config.ENABLE_GET_SUB_COMMENTS = False
    config.ENABLE_GET_WORDCLOUD = False
    config.MAX_CONCURRENCY_NUM = 1
    config.CRAWLER_MAX_NOTES_COUNT = max(1, min(args.limit, 500))
    config.CRAWLER_MAX_SLEEP_SEC = 2
    setattr(config, creator_key, [] if args.mode == "login" else [args.value])
    if args.mode == "stage":
        stage_value = normalize_stage_value(args.platform, args.value)
        if upstream == "xhs": config.XHS_SPECIFIED_NOTE_URL_LIST = [stage_value]
        elif upstream == "dy": config.DY_SPECIFIED_ID_LIST = [stage_value]
        elif upstream == "bili": config.BILI_SPECIFIED_ID_LIST = [stage_value]
        else: config.WEIBO_SPECIFIED_ID_LIST = [stage_value]
    install_qr_sink(Path(args.qr))
    crawler = mediacrawler_main.CrawlerFactory.create_crawler(upstream)
    mediacrawler_main.crawler = crawler
    provider_contract: dict[str, Any] = {
        "platform": args.platform,
        "mode": args.mode,
        "items": {},
        "discovery": {"requested_limit": max(1, min(args.limit, 500))},
    }
    install_provider_contract(args, crawler, config, provider_contract)
    try:
        await crawler.start()
        _write_provider_contract(Path(args.output), provider_contract)
    finally:
        await mediacrawler_main.async_cleanup()
        if not profile_is_in_use(profile):
            clear_chromium_singleton_files(profile)


async def run_with_signal_cleanup(args) -> None:
    task = asyncio.create_task(run(args))
    loop = asyncio.get_running_loop()
    installed_signals = []
    for signal_value in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if signal_value is None:
            continue
        try:
            loop.add_signal_handler(signal_value, task.cancel)
            installed_signals.append(signal_value)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await task
    except asyncio.CancelledError:
        # Cancellation enters run()'s finally block, allowing MediaCrawler to
        # close Playwright before the bridge escalates to killing the process
        # group after its grace period.
        pass
    finally:
        for signal_value in installed_signals:
            loop.remove_signal_handler(signal_value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["login", "discover", "stage"])
    parser.add_argument("platform", choices=list(PLATFORM_CONFIG))
    parser.add_argument("--value", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--qr", required=True)
    parser.add_argument("--browser-root", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--limit", type=int, default=20)
    asyncio.run(run_with_signal_cleanup(parser.parse_args()))
