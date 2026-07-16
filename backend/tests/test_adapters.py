from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.adapters.base import (
    AdapterError,
    ContentRef,
    NonOriginalContentError,
    StructureChangedError,
    is_challenge_page,
)
from app.adapters.platforms import BilibiliAdapter, DouyinAdapter, WeiboAdapter, XiaohongshuAdapter
from app.config import get_settings


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("adapter_class", "base_url", "expected"),
    [
        (BilibiliAdapter, "https://space.bilibili.com/123", ["BV1TEST123", "998877"]),
        (XiaohongshuAdapter, "https://www.xiaohongshu.com/user/profile/abc", ["64abcdef123"]),
        (WeiboAdapter, "https://weibo.com/u/123456", ["Nabc123"]),
    ],
)
def test_extract_public_content_references(adapter_class, base_url, expected):
    adapter = adapter_class(get_settings())
    html = (FIXTURES / "profiles.html").read_text(encoding="utf-8")
    refs = adapter.extract_refs(html, base_url)
    assert [ref.remote_id for ref in refs] == expected


def test_script_captcha_endpoint_is_not_a_challenge_page():
    html = (FIXTURES / "douyin_profile.html").read_text(encoding="utf-8")

    assert not is_challenge_page(html, url="https://www.douyin.com/user/MS4w")


@pytest.mark.parametrize("visible_text", ["验证码", "请完成验证"])
def test_visible_verification_copy_is_a_challenge_page(visible_text):
    html = f"<html><body><main>{visible_text}</main></body></html>"

    assert is_challenge_page(html, url="https://www.douyin.com/user/MS4w")
    assert is_challenge_page("<html><body></body></html>", visible_text=visible_text)


def test_douyin_extracts_only_user_post_list_and_deduplicates_in_order():
    adapter = DouyinAdapter(get_settings())
    html = (FIXTURES / "douyin_profile.html").read_text(encoding="utf-8")

    refs = adapter.extract_refs(html, "https://www.douyin.com/user/MS4w")

    assert [(ref.remote_id, ref.source_url) for ref in refs] == [
        ("7234567890", "https://www.douyin.com/video/7234567890"),
        ("7234567891", "https://www.douyin.com/note/7234567891"),
    ]


def test_generic_detail_normalization():
    adapter = BilibiliAdapter(get_settings())
    html = (FIXTURES / "detail.html").read_text(encoding="utf-8")
    item = adapter.parse_generic_detail(html, ContentRef("BV1TEST123", "https://www.bilibili.com/video/BV1TEST123"))
    assert item.title == "公开内容标题"
    assert item.author == "测试作者"
    assert item.text == "这是公开页面中的完整文案。"
    assert item.media[0].kind == "image"


def test_rejects_cross_platform_url():
    adapter = DouyinAdapter(get_settings())
    with pytest.raises(AdapterError):
        adapter.normalize_profile_url("https://example.com/user/1")


@pytest.mark.parametrize(
    ("adapter_class", "url"),
    [
        (BilibiliAdapter, "https://space.bilibili.com/123456?from=search"),
        (WeiboAdapter, "https://weibo.com/u/123456"),
        (WeiboAdapter, "https://weibo.com/123456"),
        (WeiboAdapter, "https://m.weibo.cn/profile/123456"),
        (WeiboAdapter, "https://weibo.com/?uid=123456"),
        (XiaohongshuAdapter, "https://www.xiaohongshu.com/user/profile/abc_123"),
        (XiaohongshuAdapter, "https://xhslink.com/abc123"),
    ],
)
def test_accepts_only_supported_creator_url_shapes(adapter_class, url):
    assert adapter_class(get_settings()).normalize_profile_url(url) == url


@pytest.mark.parametrize(
    ("adapter_class", "url"),
    [
        (BilibiliAdapter, "https://www.bilibili.com/video/BV123"),
        (BilibiliAdapter, "https://b23.tv/creator-share"),
        (WeiboAdapter, "https://m.weibo.cn/status/Nabc123"),
        (WeiboAdapter, "https://weibo.com/profile/not-numeric"),
        (WeiboAdapter, "https://weibo.com/home"),
        (XiaohongshuAdapter, "https://www.xiaohongshu.com/explore/64abcdef"),
        (XiaohongshuAdapter, "https://xhslink.com/"),
    ],
)
def test_rejects_content_or_unsupported_creator_url_shapes(adapter_class, url):
    with pytest.raises(AdapterError):
        adapter_class(get_settings()).normalize_profile_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@space.bilibili.com/123",
        "https://space.bilibili.com:8443/123",
        "https://space.bilibili.com/123\nInjected: value",
    ],
)
def test_rejects_credentials_non_web_ports_and_control_characters(url):
    with pytest.raises(AdapterError):
        BilibiliAdapter(get_settings()).normalize_profile_url(url)


@pytest.mark.asyncio
async def test_http_rejects_private_dns_before_sending_request(monkeypatch):
    adapter = BilibiliAdapter(get_settings())
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text="<html>unexpected</html>")

    monkeypatch.setattr(adapter, "_resolve_host_addresses", lambda _host, _port: {"127.0.0.1"})
    monkeypatch.setattr(
        adapter,
        "_make_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
    )

    with pytest.raises(AdapterError, match="non-public"):
        await adapter._http_html("https://space.bilibili.com/123")
    assert requested == []


@pytest.mark.asyncio
async def test_http_validates_redirect_dns_before_following(monkeypatch):
    adapter = BilibiliAdapter(get_settings())
    requested: list[str] = []

    def resolve(host: str, _port: int) -> set[str]:
        return {"10.0.0.7"} if host == "internal.bilibili.com" else {"8.8.8.8"}

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://internal.bilibili.com/private"})

    monkeypatch.setattr(adapter, "_resolve_host_addresses", resolve)
    monkeypatch.setattr(
        adapter,
        "_make_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
    )

    with pytest.raises(AdapterError, match="non-public"):
        await adapter._http_html("https://space.bilibili.com/123")
    assert requested == ["https://space.bilibili.com/123"]


@pytest.mark.asyncio
async def test_http_follows_a_validated_same_platform_redirect(monkeypatch):
    adapter = BilibiliAdapter(get_settings())
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "space.bilibili.com":
            return httpx.Response(302, headers={"Location": "https://www.bilibili.com/video/BV123"})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text="<html><body>public</body></html>",
        )

    monkeypatch.setattr(adapter, "_resolve_host_addresses", lambda _host, _port: {"8.8.8.8"})
    monkeypatch.setattr(
        adapter,
        "_make_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
    )

    html, final_url = await adapter._http_html("https://space.bilibili.com/123")

    assert "public" in html
    assert final_url == "https://www.bilibili.com/video/BV123"
    assert requested == [
        "https://space.bilibili.com/123",
        "https://www.bilibili.com/video/BV123",
    ]


@pytest.mark.asyncio
async def test_http_rejects_cross_platform_redirect_without_following(monkeypatch):
    adapter = WeiboAdapter(get_settings())
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})

    monkeypatch.setattr(adapter, "_resolve_host_addresses", lambda _host, _port: {"8.8.8.8"})
    monkeypatch.setattr(
        adapter,
        "_make_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
    )

    with pytest.raises(AdapterError):
        await adapter._http_html("https://weibo.com/u/123456")
    assert requested == ["https://weibo.com/u/123456"]


@pytest.mark.asyncio
async def test_http_streaming_enforces_decompressed_response_limit(monkeypatch):
    adapter = BilibiliAdapter(get_settings())
    adapter.max_html_response_bytes = 5

    class ChunkedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123"
            yield b"456"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/html"}, stream=ChunkedBody())

    monkeypatch.setattr(adapter, "_resolve_host_addresses", lambda _host, _port: {"8.8.8.8"})
    monkeypatch.setattr(
        adapter,
        "_make_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
    )

    with pytest.raises(AdapterError, match="size limit"):
        await adapter._http_html("https://space.bilibili.com/123")


@pytest.mark.asyncio
async def test_browser_navigation_guard_aborts_unsafe_redirect(monkeypatch):
    adapter = BilibiliAdapter(get_settings())
    main_frame = object()
    page = SimpleNamespace(main_frame=main_frame)
    request = SimpleNamespace(
        url="https://internal.bilibili.com/private",
        frame=main_frame,
        is_navigation_request=lambda: True,
    )

    class FakeRoute:
        def __init__(self):
            self.request = request
            self.aborted = False
            self.continued = False

        async def abort(self, _reason):
            self.aborted = True

        async def continue_(self):
            self.continued = True

    route = FakeRoute()
    blocked: list[AdapterError] = []
    monkeypatch.setattr(adapter, "_resolve_host_addresses", lambda _host, _port: {"192.168.1.2"})

    await adapter._guard_browser_navigation(route, page, blocked)

    assert route.aborted is True
    assert route.continued is False
    assert len(blocked) == 1


@pytest.mark.asyncio
async def test_browser_fallback_runs_when_http_page_has_no_references(monkeypatch):
    adapter = BilibiliAdapter(get_settings())
    fixture = (FIXTURES / "profiles.html").read_text(encoding="utf-8")

    async def empty_http(_url):
        return "<html><body><div id='app'></div></body></html>", "https://space.bilibili.com/123"

    async def rendered_browser(_url):
        return fixture, "https://space.bilibili.com/123"

    async def original_video(_url, **_kwargs):
        return {
            "code": 0,
            "data": {
                "bvid": "BV1TEST123",
                "copyright": 1,
                "owner": {"mid": 123},
            },
        }, "https://api.bilibili.com/x/web-interface/view?bvid=BV1TEST123"

    monkeypatch.setattr(adapter, "_http_html", empty_http)
    monkeypatch.setattr(adapter, "_browser_html", rendered_browser)
    monkeypatch.setattr(adapter, "_http_json", original_video)
    refs = await adapter.fetch_latest("https://space.bilibili.com/123")
    assert refs[0].remote_id == "BV1TEST123"


@pytest.mark.asyncio
async def test_bilibili_fallback_excludes_reposts_and_unverifiable_types(monkeypatch):
    adapter = BilibiliAdapter(get_settings())
    fixture = (FIXTURES / "profiles.html").read_text(encoding="utf-8")

    async def profile(_url):
        return fixture, "https://space.bilibili.com/123"

    async def repost(_url, **_kwargs):
        return {
            "code": 0,
            "data": {
                "bvid": "BV1TEST123",
                "copyright": 2,
                "owner": {"mid": 123},
            },
        }, "https://api.bilibili.com/x/web-interface/view?bvid=BV1TEST123"

    monkeypatch.setattr(adapter, "_http_html", profile)
    monkeypatch.setattr(adapter, "_http_json", repost)

    assert await adapter.fetch_latest("https://space.bilibili.com/123") == []


@pytest.mark.asyncio
async def test_bilibili_fallback_rejects_unknown_originality_metadata(monkeypatch):
    adapter = BilibiliAdapter(get_settings())

    async def unknown(_url, **_kwargs):
        return {"code": 0, "data": {"bvid": "BV1TEST123", "owner": {"mid": 123}}}, _url

    monkeypatch.setattr(adapter, "_http_json", unknown)

    with pytest.raises(StructureChangedError, match="originality marker"):
        await adapter._require_original_video(
            ContentRef("BV1TEST123", "https://www.bilibili.com/video/BV1TEST123"),
            creator_id="123",
        )


@pytest.mark.asyncio
async def test_bilibili_fallback_detail_rejects_repost_before_page_fetch(monkeypatch):
    adapter = BilibiliAdapter(get_settings())

    async def repost(_url, **_kwargs):
        return {
            "code": 0,
            "data": {
                "bvid": "BV1TEST123",
                "copyright": 2,
                "owner": {"mid": 123},
            },
        }, _url

    async def unexpected_page(_url):
        raise AssertionError("repost detail page must not be fetched")

    monkeypatch.setattr(adapter, "_http_json", repost)
    monkeypatch.setattr(adapter, "_http_html", unexpected_page)

    with pytest.raises(NonOriginalContentError, match="repost"):
        await adapter.fetch_detail(
            ContentRef("BV1TEST123", "https://www.bilibili.com/video/BV1TEST123")
        )


@pytest.mark.asyncio
async def test_douyin_browser_fallback_accepts_normal_page_with_script_captcha_marker(monkeypatch):
    adapter = DouyinAdapter(get_settings())
    fixture = (FIXTURES / "douyin_profile.html").read_text(encoding="utf-8")

    async def empty_http(_url):
        return "<html><body><div id='app'></div></body></html>", "https://www.douyin.com/user/MS4w"

    async def rendered_browser(_url):
        return fixture, "https://www.douyin.com/user/MS4w"

    monkeypatch.setattr(adapter, "_http_html", empty_http)
    monkeypatch.setattr(adapter, "_browser_html", rendered_browser)

    refs = await adapter.fetch_latest("https://www.douyin.com/user/MS4w")

    assert [ref.remote_id for ref in refs] == ["7234567890", "7234567891"]
