from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .base import (
    AdapterError,
    ContentRef,
    MediaCandidate,
    NonOriginalContentError,
    NormalizedContent,
    PublicPageAdapter,
    StructureChangedError,
    clean_text,
    parse_datetime,
)
from ..models import Platform


def safe_slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._")
    return value[:100] or "account"


class BilibiliAdapter(PublicPageAdapter):
    platform = Platform.bilibili
    allowed_hosts = ("bilibili.com", "b23.tv")
    content_patterns = (
        re.compile(r"https?://(?:www\.)?bilibili\.com/video/(?P<id>BV[\w]+)"),
        re.compile(r"https?://(?:www\.)?bilibili\.com/opus/(?P<id>\d+)"),
        re.compile(r"https?://(?:www\.)?bilibili\.com/read/cv(?P<id>\d+)"),
    )

    def normalize_profile_url(self, url: str) -> str:
        normalized = super().normalize_profile_url(url)
        parsed = urlparse(normalized)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host != "space.bilibili.com" or not re.fullmatch(r"/\d+/?", parsed.path):
            raise AdapterError("Bilibili URL must be a numeric space.bilibili.com creator homepage")
        return normalized

    def account_slug(self, profile_url: str) -> str:
        match = re.search(r"space\.bilibili\.com/(\d+)", profile_url)
        return safe_slug(match.group(1) if match else urlparse(profile_url).path)

    async def _require_original_video(
        self,
        ref: ContentRef,
        *,
        creator_id: str | None = None,
    ) -> None:
        if "/video/" not in ref.source_url or not re.fullmatch(r"BV[\w]+", ref.remote_id):
            raise NonOriginalContentError(
                "Bilibili fallback cannot verify this content type as an original submission"
            )
        query = urlencode({"bvid": ref.remote_id})
        payload, _ = await self._http_json(
            f"https://api.bilibili.com/x/web-interface/view?{query}"
        )
        if (
            not isinstance(payload, dict)
            or payload.get("code") != 0
            or not isinstance(payload.get("data"), dict)
        ):
            raise StructureChangedError(
                "Bilibili fallback video metadata has an unknown structure"
            )
        data = payload["data"]
        if str(data.get("bvid") or "") != ref.remote_id:
            raise StructureChangedError("Bilibili fallback video identity did not match")
        copyright_value = data.get("copyright")
        if not (
            (type(copyright_value) is int and copyright_value in {1, 2})
            or (type(copyright_value) is str and copyright_value in {"1", "2"})
        ):
            raise StructureChangedError(
                "Bilibili fallback video metadata omitted its originality marker"
            )
        owner = data.get("owner")
        if creator_id is not None:
            if not isinstance(owner, dict) or str(owner.get("mid") or "") != creator_id:
                raise NonOriginalContentError(
                    "Bilibili fallback reference does not belong to the monitored creator"
                )
        if copyright_value not in {1, "1"}:
            raise NonOriginalContentError(
                "Bilibili video is marked as a repost rather than an original submission"
            )

    async def fetch_latest(self, profile_url: str) -> list[ContentRef]:
        normalized = self.normalize_profile_url(profile_url)
        creator_id = self.account_slug(normalized)
        refs = await super().fetch_latest(normalized)
        semaphore = asyncio.Semaphore(4)

        async def verify(ref: ContentRef) -> ContentRef | None:
            async with semaphore:
                try:
                    await self._require_original_video(ref, creator_id=creator_id)
                except NonOriginalContentError:
                    return None
                return ref

        verified = await asyncio.gather(*(verify(ref) for ref in refs))
        return [ref for ref in verified if ref is not None]

    async def fetch_detail(self, ref: ContentRef) -> NormalizedContent:
        await self._require_original_video(ref)
        item = await super().fetch_detail(ref)
        if "/video/" in ref.source_url:
            item.content_type = "video"
            if not any(media.kind == "video" for media in item.media):
                item.media.append(MediaCandidate("video", ref.source_url, via_ytdlp=True))
        return item


class DouyinAdapter(PublicPageAdapter):
    platform = Platform.douyin
    allowed_hosts = ("douyin.com", "iesdouyin.com")
    content_patterns = (
        re.compile(r"https?://(?:www\.)?douyin\.com/video/(?P<id>\d+)"),
        re.compile(r"https?://(?:www\.)?douyin\.com/note/(?P<id>\d+)"),
    )

    @staticmethod
    def _is_profile_url(url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.hostname.lower() if parsed.hostname else ""
        if host == "v.douyin.com":
            return bool(re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path))
        return bool(re.fullmatch(r"/(?:share/)?user/[\w.-]+/?", parsed.path))

    def normalize_profile_url(self, url: str) -> str:
        normalized = super().normalize_profile_url(url)
        if not self._is_profile_url(normalized):
            raise AdapterError("Douyin URL must be a public user profile or profile share link")
        return normalized

    async def _wait_for_browser_page(self, page: Page) -> None:
        await page.wait_for_timeout(1800)
        selector = (
            '[data-e2e="user-post-list"] a[href*="/video/"], '
            '[data-e2e="user-post-list"] a[href*="/note/"]'
        )
        try:
            await page.wait_for_selector(selector, timeout=8_500)
        except PlaywrightTimeoutError:
            # fetch_latest will distinguish an empty/changed page from a block.
            pass

    def extract_refs(self, html: str, base_url: str) -> list[ContentRef]:
        if not self._is_profile_url(base_url):
            raise AdapterError("Douyin share link did not resolve to a public user profile")
        soup = BeautifulSoup(html, "html.parser")
        post_list = soup.find(attrs={"data-e2e": "user-post-list"})
        if post_list is not None:
            return super().extract_refs(str(post_list), base_url)
        return super().extract_refs(html, base_url)

    def account_slug(self, profile_url: str) -> str:
        match = re.search(r"/user/([\w-]+)", profile_url)
        return safe_slug(match.group(1) if match else urlparse(profile_url).path)

    async def fetch_detail(self, ref: ContentRef) -> NormalizedContent:
        item = await super().fetch_detail(ref)
        if "/video/" in ref.source_url:
            item.content_type = "video"
            item.media = [media for media in item.media if media.kind == "image"]
            item.media.append(MediaCandidate("video", ref.source_url, via_ytdlp=True))
        return item


class XiaohongshuAdapter(PublicPageAdapter):
    platform = Platform.xiaohongshu
    allowed_hosts = ("xiaohongshu.com", "xhslink.com")
    content_patterns = (
        re.compile(r"https?://(?:www\.)?xiaohongshu\.com/explore/(?P<id>[a-fA-F0-9]+)"),
        re.compile(r"https?://(?:www\.)?xiaohongshu\.com/discovery/item/(?P<id>[a-fA-F0-9]+)"),
    )

    def normalize_profile_url(self, url: str) -> str:
        normalized = super().normalize_profile_url(url)
        parsed = urlparse(normalized)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host == "xhslink.com" or host.endswith(".xhslink.com"):
            if parsed.path in {"", "/"}:
                raise AdapterError("Xiaohongshu short URL must contain a link token")
            return normalized
        if not re.fullmatch(r"/user/profile/[A-Za-z0-9_-]+/?", parsed.path):
            raise AdapterError("Xiaohongshu URL must be a public user profile or xhslink short URL")
        return normalized

    def account_slug(self, profile_url: str) -> str:
        match = re.search(r"/user/profile/([\w-]+)", profile_url)
        return safe_slug(match.group(1) if match else urlparse(profile_url).path)

    async def fetch_detail(self, ref: ContentRef) -> NormalizedContent:
        item = await super().fetch_detail(ref)
        if item.content_type == "video" and not any(media.via_ytdlp for media in item.media):
            item.media = [media for media in item.media if media.kind == "image"]
            item.media.append(MediaCandidate("video", ref.source_url, via_ytdlp=True))
        return item


class WeiboAdapter(PublicPageAdapter):
    platform = Platform.weibo
    allowed_hosts = ("weibo.com", "weibo.cn")
    content_patterns = (
        re.compile(r"https?://(?:www\.)?weibo\.com/\d+/(?P<id>[A-Za-z0-9]+)"),
        re.compile(r"https?://m\.weibo\.cn/status/(?P<id>[A-Za-z0-9]+)"),
    )

    def normalize_profile_url(self, url: str) -> str:
        normalized = super().normalize_profile_url(url)
        parsed = urlparse(normalized)
        path_match = re.fullmatch(r"/(?:u/|profile/)?(?P<uid>\d+)/?", parsed.path)
        query_uids = parse_qs(parsed.query).get("uid", [])
        if path_match is None and not (len(query_uids) == 1 and query_uids[0].isdigit()):
            raise AdapterError("Weibo URL must contain a numeric creator uid")
        return normalized

    def account_slug(self, profile_url: str) -> str:
        match = re.search(r"/(?:u/)?(\d{5,})", urlparse(profile_url).path)
        return safe_slug(match.group(1) if match else urlparse(profile_url).path)

    def _uid(self, profile_url: str) -> str | None:
        match = re.search(r"/(?:u/)?(\d{5,})", urlparse(profile_url).path)
        if match:
            return match.group(1)
        query = parse_qs(urlparse(profile_url).query)
        return query.get("uid", [None])[0]

    async def fetch_latest(self, profile_url: str) -> list[ContentRef]:
        normalized = self.normalize_profile_url(profile_url)
        await self._validate_public_platform_url(normalized)
        uid = self._uid(normalized)
        if not uid:
            return await super().fetch_latest(normalized)
        endpoint = f"https://m.weibo.cn/api/container/getIndex?containerid=107603{uid}"
        try:
            payload, _ = await self._http_json(endpoint, headers={"Referer": normalized})
            refs: list[ContentRef] = []
            for card in payload.get("data", {}).get("cards", []):
                mblog = card.get("mblog") or {}
                # MediaCrawler's pinned Weibo store uses the numeric mblog id.
                # Keep the fallback on that same canonical identity so provider
                # failover cannot replay existing posts under their short bid.
                remote_id = str(mblog.get("id") or mblog.get("bid") or "")
                if remote_id and not mblog.get("retweeted_status"):
                    refs.append(ContentRef(remote_id, f"https://m.weibo.cn/status/{remote_id}"))
            if refs:
                return refs[:20]
        except (httpx.HTTPError, AdapterError, ValueError, TypeError, AttributeError):
            pass
        return await super().fetch_latest(normalized)

    async def fetch_detail(self, ref: ContentRef) -> NormalizedContent:
        await self._validate_public_platform_url(ref.source_url)
        endpoint = f"https://m.weibo.cn/statuses/show?id={ref.remote_id}"
        try:
            data, _ = await self._http_json(endpoint, headers={"Referer": ref.source_url})
            text = clean_text(re.sub(r"<[^>]+>", " ", data.get("text", "")))
            author = clean_text((data.get("user") or {}).get("screen_name", ""))
            media: list[MediaCandidate] = []
            for picture in data.get("pics") or []:
                image_url = ((picture.get("large") or {}).get("url") or picture.get("url"))
                if image_url:
                    media.append(MediaCandidate("image", image_url))
            page = data.get("page_info") or {}
            media_info = page.get("media_info") or {}
            video_url = media_info.get("stream_url_hd") or media_info.get("stream_url")
            if video_url:
                media.append(MediaCandidate("video", video_url))
            created = data.get("created_at")
            published = datetime.now(timezone.utc)
            if created:
                try:
                    published = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
                except ValueError:
                    published = parse_datetime(created)
            return NormalizedContent(
                platform=self.platform,
                remote_id=ref.remote_id,
                source_url=ref.source_url,
                title=text[:120] or f"微博 {ref.remote_id}",
                author=author,
                text=text,
                published_at=published,
                content_type="video" if video_url else "image" if media else "text",
                media=self._dedupe_media(media),
            )
        except (httpx.HTTPError, AdapterError, ValueError, TypeError, AttributeError):
            return await super().fetch_detail(ref)


ADAPTER_CLASSES = {
    Platform.bilibili: BilibiliAdapter,
    Platform.weibo: WeiboAdapter,
    Platform.douyin: DouyinAdapter,
    Platform.xiaohongshu: XiaohongshuAdapter,
}
