from __future__ import annotations

import asyncio
import html as html_lib
import ipaddress
import json
import os
import re
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from ..config import Settings
from ..models import Platform


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 PublicArchiveMonitor/1.0"
)
MAX_HTML_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class AdapterError(RuntimeError):
    pass


class AccessBlockedError(AdapterError):
    pass


class StructureChangedError(AdapterError):
    pass


class NonOriginalContentError(AdapterError):
    """A reference is known not to satisfy the original-content boundary."""


@dataclass(slots=True)
class ContentRef:
    remote_id: str
    source_url: str
    pinned: bool = False
    window_truncated: bool = False
    aliases: tuple[str, ...] = ()


@dataclass(slots=True)
class MediaCandidate:
    kind: str
    url: str
    filename_hint: str = ""
    via_ytdlp: bool = False


@dataclass(slots=True)
class NormalizedContent:
    platform: Platform
    remote_id: str
    source_url: str
    title: str
    author: str
    text: str
    published_at: datetime
    content_type: str
    media: list[MediaCandidate] = field(default_factory=list)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def is_challenge_page(html: str, url: str = "", visible_text: str | None = None) -> bool:
    """Detect an actual visible verification page, not words inside bundled scripts."""
    path = urlparse(url).path.lower()
    if any(marker in path for marker in ("/captcha/", "/verify/", "/verifycenter/")):
        return True

    if visible_text is None:
        soup = BeautifulSoup(html, "html.parser")
        for node in soup(["script", "style", "noscript", "template"]):
            node.decompose()
        visible_text = soup.get_text(" ", strip=True)

    lowered = clean_text(visible_text).lower()
    markers = (
        "\u9a8c\u8bc1\u7801",
        "\u8bbf\u95ee\u8fc7\u4e8e\u9891\u7e41",
        "\u8bf7\u5b8c\u6210\u9a8c\u8bc1",
        "\u5b89\u5168\u9a8c\u8bc1",
        "complete the security check",
        "complete the verification",
    )
    return any(marker in lowered for marker in markers) or bool(re.search(r"\bcaptcha\b", lowered))


def first_string(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in keys and isinstance(child, (str, int)):
                return clean_text(str(child))
        for child in value.values():
            found = first_string(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = first_string(child, keys)
            if found:
                return found
    return ""


def walk_json(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (ValueError, OSError):
            pass
    if isinstance(value, str) and value:
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class PublicPageAdapter(ABC):
    platform: Platform
    allowed_hosts: tuple[str, ...]
    content_patterns: tuple[re.Pattern[str], ...]
    max_html_response_bytes = MAX_HTML_RESPONSE_BYTES
    max_json_response_bytes = MAX_JSON_RESPONSE_BYTES
    max_redirects = MAX_REDIRECTS

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _validate_platform_url_shape(self, url: str) -> tuple[Any, str, int]:
        if not url or any(ord(character) < 32 or ord(character) == 127 for character in url):
            raise AdapterError("URL contains invalid control characters")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise AdapterError("Profile URL must use HTTP or HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise AdapterError("Platform URLs must not contain credentials")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise AdapterError("Platform URL contains an invalid port") from exc
        if port not in {80, 443}:
            raise AdapterError("Platform URLs may use only ports 80 and 443")
        try:
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as exc:
            raise AdapterError("Platform URL contains an invalid hostname") from exc
        if not host or host == "localhost" or host.endswith(".localhost"):
            raise AdapterError("Platform URL must use a public hostname")
        if not any(host == allowed or host.endswith(f".{allowed}") for allowed in self.allowed_hosts):
            raise AdapterError(f"URL does not belong to {self.platform.value}")
        try:
            literal_address = ipaddress.ip_address(host)
        except ValueError:
            literal_address = None
        if literal_address is not None and not literal_address.is_global:
            raise AdapterError("Platform URL resolves to a non-public address")
        return parsed, host, port

    @staticmethod
    def _resolve_host_addresses(host: str, port: int) -> set[str]:
        results = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        return {str(result[4][0]).split("%", 1)[0] for result in results}

    async def _validate_public_platform_url(self, url: str) -> str:
        parsed, host, port = self._validate_platform_url_shape(url)
        try:
            addresses = await asyncio.to_thread(self._resolve_host_addresses, host, port)
        except (OSError, UnicodeError) as exc:
            raise AdapterError(f"Platform hostname could not be resolved: {host}") from exc
        if not addresses:
            raise AdapterError(f"Platform hostname did not resolve: {host}")
        try:
            parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
        except ValueError as exc:
            raise AdapterError(f"Platform hostname returned an invalid address: {host}") from exc
        if any(not address.is_global for address in parsed_addresses):
            raise AdapterError(f"Platform hostname resolved to a non-public address: {host}")
        return parsed._replace(fragment="").geturl()

    def normalize_profile_url(self, url: str) -> str:
        parsed, _host, _port = self._validate_platform_url_shape(url)
        return parsed._replace(fragment="").geturl()

    def _make_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=False,
        )

    async def _http_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, str, str | None]:
        current_url = url
        async with self._make_http_client() as client:
            for redirect_count in range(self.max_redirects + 1):
                current_url = await self._validate_public_platform_url(current_url)
                async with client.stream("GET", current_url, headers=headers) as response:
                    response_url = await self._validate_public_platform_url(str(response.url))
                    if response.status_code in {401, 403, 418, 429}:
                        raise AccessBlockedError(f"Public page returned HTTP {response.status_code}")
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise AdapterError("Platform redirect omitted its destination")
                        if redirect_count >= self.max_redirects:
                            raise AdapterError("Platform response exceeded the redirect limit")
                        # Validate every next hop before httpx is allowed to issue it.
                        current_url = await self._validate_public_platform_url(urljoin(response_url, location))
                        continue
                    response.raise_for_status()
                    declared_length = response.headers.get("content-length")
                    if declared_length is not None:
                        try:
                            declared_bytes = int(declared_length)
                        except ValueError as exc:
                            raise AdapterError("Platform response has an invalid Content-Length") from exc
                        if declared_bytes < 0 or declared_bytes > max_bytes:
                            raise AdapterError("Platform response exceeds the configured size limit")
                    chunks: list[bytes] = []
                    received_bytes = 0
                    async for chunk in response.aiter_bytes():
                        received_bytes += len(chunk)
                        if received_bytes > max_bytes:
                            raise AdapterError("Platform response exceeds the configured size limit")
                        chunks.append(chunk)
                    return b"".join(chunks), response_url, response.encoding
        raise AdapterError("Platform response exceeded the redirect limit")

    async def _http_html(self, url: str) -> tuple[str, str]:
        body, final_url, encoding = await self._http_bytes(
            url,
            max_bytes=self.max_html_response_bytes,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        try:
            html = body.decode(encoding or "utf-8", errors="replace")
        except LookupError:
            html = body.decode("utf-8", errors="replace")
        if is_challenge_page(html, final_url):
            raise AccessBlockedError("Platform presented a challenge page")
        # Legacy raw-HTML markers below are intentionally bypassed: bundled
        # application scripts commonly contain captcha route names.
        lowered = ""
        if any(marker in lowered for marker in ("captcha", "验证码", "访问过于频繁")):
            raise AccessBlockedError("Platform presented a challenge page")
        return html, final_url

    async def _http_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, str]:
        body, final_url, encoding = await self._http_bytes(
            url,
            max_bytes=self.max_json_response_bytes,
            headers={"Accept": "application/json", **(headers or {})},
        )
        try:
            text = body.decode(encoding or "utf-8", errors="strict")
        except (LookupError, UnicodeDecodeError) as exc:
            raise AdapterError("Platform returned invalid JSON encoding") from exc
        try:
            return json.loads(text), final_url
        except json.JSONDecodeError as exc:
            raise AdapterError("Platform returned invalid JSON") from exc

    async def _wait_for_browser_page(self, page: Page) -> None:
        await page.wait_for_timeout(1800)

    async def _guard_browser_navigation(
        self,
        route: Any,
        page: Page,
        blocked_navigation: list[AdapterError],
    ) -> None:
        request = route.request
        if request.is_navigation_request() and request.frame == page.main_frame:
            try:
                await self._validate_public_platform_url(request.url)
            except AdapterError as exc:
                blocked_navigation.append(exc)
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    async def _browser_html(self, url: str) -> tuple[str, str]:
        url = await self._validate_public_platform_url(url)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                executable_path=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None,
            )
            context = await browser.new_context(user_agent=USER_AGENT, locale="zh-CN")
            page = await context.new_page()
            blocked_navigation: list[AdapterError] = []

            async def guard_navigation(route: Any) -> None:
                await self._guard_browser_navigation(route, page, blocked_navigation)

            await context.route("**/*", guard_navigation)
            try:
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                except Exception:
                    if blocked_navigation:
                        raise AdapterError("Browser navigation to an unsafe redirect was blocked") from blocked_navigation[-1]
                    raise
                if blocked_navigation:
                    raise AdapterError("Browser navigation to an unsafe redirect was blocked") from blocked_navigation[-1]
                await self._validate_public_platform_url(page.url)
                if response and response.status in {401, 403, 418, 429}:
                    raise AccessBlockedError(f"Rendered public page returned HTTP {response.status}")
                await self._wait_for_browser_page(page)
                if blocked_navigation:
                    raise AdapterError("Browser navigation to an unsafe redirect was blocked") from blocked_navigation[-1]
                await self._validate_public_platform_url(page.url)
                content = await page.content()
                if (
                    len(content) > self.max_html_response_bytes
                    or len(content.encode("utf-8")) > self.max_html_response_bytes
                ):
                    raise AdapterError("Rendered platform page exceeds the configured size limit")
                try:
                    body_text = await page.locator("body").inner_text(timeout=3_000)
                except PlaywrightTimeoutError:
                    body_text = ""
                title = await page.title()
                if is_challenge_page(content, page.url, f"{title}\n{body_text}"):
                    raise AccessBlockedError("Platform presented a challenge page")
                # Keep the old compatibility branch inert for the same reason as
                # the HTTP path above.
                lowered = ""
                if any(marker in lowered for marker in ("captcha", "验证码", "访问过于频繁")):
                    raise AccessBlockedError("Platform presented a challenge page")
                return content, page.url
            finally:
                await browser.close()

    async def get_html(self, url: str) -> tuple[str, str]:
        http_error: Exception | None = None
        try:
            html, final_url = await self._http_html(url)
            if len(html) >= 800:
                return html, final_url
        except (httpx.HTTPError, AdapterError) as exc:
            http_error = exc
        try:
            return await self._browser_html(url)
        except Exception as browser_error:
            raise AdapterError(
                f"HTTP and browser retrieval failed: {http_error or 'empty response'}; {browser_error}"
            ) from browser_error

    def extract_refs(self, html: str, base_url: str) -> list[ContentRef]:
        refs: list[ContentRef] = []
        seen: set[str] = set()
        soup = BeautifulSoup(html, "html.parser")
        candidates = [tag.get("href", "") for tag in soup.find_all("a")]
        decoded_html = html.replace("\\/", "/").replace("\\u002F", "/")
        candidates.extend(re.findall(r"https?://[^\s\"'<>\\]+", decoded_html))
        for candidate in candidates:
            candidate = candidate.replace("\\/", "/")
            absolute = urljoin(base_url, candidate)
            for pattern in self.content_patterns:
                match = pattern.search(absolute)
                if match:
                    remote_id = match.group("id")
                    if remote_id not in seen:
                        seen.add(remote_id)
                        refs.append(ContentRef(remote_id=remote_id, source_url=match.group(0)))
                    break
            if len(refs) >= 20:
                break
        return refs

    async def fetch_latest(self, profile_url: str) -> list[ContentRef]:
        normalized = self.normalize_profile_url(profile_url)
        http_error: Exception | None = None
        try:
            html, final_url = await self._http_html(normalized)
            refs = self.extract_refs(html, final_url)
            if refs:
                return refs[:20]
            http_error = StructureChangedError("HTTP page contained no recognizable content references")
        except (httpx.HTTPError, AdapterError) as exc:
            http_error = exc
        try:
            html, final_url = await self._browser_html(normalized)
            refs = self.extract_refs(html, final_url)
            if refs:
                return refs[:20]
        except Exception as exc:
            if isinstance(exc, AccessBlockedError):
                raise
            raise AdapterError(f"HTTP and browser discovery failed: {http_error}; {exc}") from exc
        raise StructureChangedError(
            f"No public content references were found after HTTP and browser rendering: {http_error}"
        )

    def _script_json(self, soup: BeautifulSoup) -> list[Any]:
        values: list[Any] = []
        for script in soup.find_all("script"):
            raw = script.string or script.get_text()
            if not raw or len(raw) > 8_000_000:
                continue
            raw = raw.strip()
            if raw.startswith("{") or raw.startswith("["):
                try:
                    values.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return values

    def parse_generic_detail(self, html: str, ref: ContentRef) -> NormalizedContent:
        soup = BeautifulSoup(html, "html.parser")

        def meta(*names: str) -> str:
            for name in names:
                node = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
                if node and node.get("content"):
                    return clean_text(str(node["content"]))
            return ""

        title = meta("og:title", "twitter:title") or clean_text(soup.title.string if soup.title else "")
        text = meta("og:description", "description", "twitter:description")
        author = meta("article:author", "author")
        published = meta("article:published_time", "datePublished", "uploadDate")
        media: list[MediaCandidate] = []
        for node in soup.find_all("meta", attrs={"property": re.compile(r"^og:image")})[:20]:
            if node.get("content"):
                media.append(MediaCandidate("image", urljoin(ref.source_url, str(node["content"]))))
        video = meta("og:video:url", "og:video")
        if video:
            media.append(MediaCandidate("video", video))
        scripts = self._script_json(soup)
        if not title:
            title = first_string(scripts, {"title", "desc", "description"})
        if not text:
            text = first_string(scripts, {"desc", "description", "content", "text", "text_raw"})
        if not author:
            author = first_string(scripts, {"nickname", "name", "uname", "screen_name"})
        return NormalizedContent(
            platform=self.platform,
            remote_id=ref.remote_id,
            source_url=ref.source_url,
            title=title or f"{self.platform.value} {ref.remote_id}",
            author=author,
            text=text,
            published_at=parse_datetime(published),
            content_type="video" if any(item.kind == "video" for item in media) else "image" if media else "text",
            media=self._dedupe_media(media),
        )

    @staticmethod
    def _dedupe_media(items: list[MediaCandidate]) -> list[MediaCandidate]:
        result: list[MediaCandidate] = []
        seen: set[str] = set()
        for item in items:
            if item.url and item.url not in seen:
                seen.add(item.url)
                result.append(item)
        return result

    async def fetch_detail(self, ref: ContentRef) -> NormalizedContent:
        http_error: Exception | None = None
        try:
            html, _ = await self._http_html(ref.source_url)
            item = self.parse_generic_detail(html, ref)
            if item.text or item.media or not item.title.endswith(ref.remote_id):
                return item
            http_error = StructureChangedError("HTTP detail page contained no usable metadata")
        except (httpx.HTTPError, AdapterError) as exc:
            http_error = exc
        try:
            html, _ = await self._browser_html(ref.source_url)
            item = self.parse_generic_detail(html, ref)
            if item.text or item.media or not item.title.endswith(ref.remote_id):
                return item
        except Exception as exc:
            if isinstance(exc, AccessBlockedError):
                raise
            raise AdapterError(f"HTTP and browser detail retrieval failed: {http_error}; {exc}") from exc
        raise StructureChangedError(f"Rendered detail page contained no usable metadata: {http_error}")

    @abstractmethod
    def account_slug(self, profile_url: str) -> str:
        raise NotImplementedError
