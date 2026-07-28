from __future__ import annotations

import hashlib

import httpx
import pytest

from app.adapters.base import ContentRef
from app.config import get_settings
from app.models import Platform
from app.provider import HttpProvider, ProviderExecutionError


@pytest.mark.asyncio
async def test_request_preserves_structured_provider_error_metadata(monkeypatch):
    class FakeResponse:
        status_code = 502
        content = b'{"detail": {}}'

        @staticmethod
        def json():
            return {
                "detail": {
                    "code": "provider_contract_invalid",
                    "message": "Provider contract is incomplete",
                    "phase": "discovery",
                    "retryable": False,
                }
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "app.provider.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )
    provider = HttpProvider(
        get_settings().model_copy(
            update={
                "provider_base_url": "http://provider.example",
                "provider_api_token": "provider-token-that-is-long-enough",
            }
        )
    )

    with pytest.raises(ProviderExecutionError) as caught:
        await provider._request("POST", "/v1/creators/discover")

    assert caught.value.code == "provider_contract_invalid"
    assert caught.value.phase == "discovery"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_json_requests_reuse_a_bounded_http_client(monkeypatch):
    settings = get_settings().model_copy(
        update={
            "provider_base_url": "http://provider.example",
            "provider_api_token": "provider-token-that-is-long-enough",
        }
    )
    provider = HttpProvider(settings)
    clients: list[httpx.AsyncClient] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    def make_client() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            base_url=settings.provider_base_url,
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(provider, "_client", make_client)

    assert await provider._request("GET", "/v1/health") == {"status": "ok"}
    assert await provider._request("GET", "/v1/health") == {"status": "ok"}
    assert len(clients) == 1

    await provider.aclose()
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_discovery_orders_complete_publication_times_newest_first(monkeypatch):
    provider = HttpProvider(get_settings())

    async def request(_method, _path, **_kwargs):
        return {
            "items": [
                {
                    "remote_id": "oldest",
                    "source_url": "https://example.com/oldest",
                    "published_at": "2026-07-01T08:00:00Z",
                    "original": True,
                    "aliases": ["legacy-oldest"],
                },
                {
                    "remote_id": "newest",
                    "source_url": "https://example.com/newest",
                    "published_at": "2026-07-15T08:00:00Z",
                    "original": True,
                },
                {
                    "remote_id": "middle",
                    "source_url": "https://example.com/middle",
                    "published_at": "2026-07-10T08:00:00Z",
                    "original": True,
                },
            ]
        }

    monkeypatch.setattr(provider, "_request", request)

    refs = await provider.discover(Platform.bilibili, "https://space.bilibili.com/123")

    assert [ref.remote_id for ref in refs] == ["newest", "middle", "oldest"]
    assert refs[-1].aliases == ("legacy-oldest",)


@pytest.mark.asyncio
async def test_discovery_preserves_provider_order_when_a_publication_time_is_missing(monkeypatch):
    provider = HttpProvider(get_settings())

    async def request(_method, _path, **_kwargs):
        return {
            "items": [
                {
                    "remote_id": "provider-first",
                    "source_url": "https://example.com/first",
                    "original": True,
                },
                {
                    "remote_id": "dated",
                    "source_url": "https://example.com/dated",
                    "published_at": "2026-07-15T08:00:00Z",
                    "original": True,
                },
            ]
        }

    monkeypatch.setattr(provider, "_request", request)

    refs = await provider.discover(Platform.bilibili, "https://space.bilibili.com/123")

    assert [ref.remote_id for ref in refs] == ["provider-first", "dated"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"items": []},
        {"items": [{"remote_id": "42", "source_url": "https://example.com/42"}]},
        {
            "items": [
                {
                    "remote_id": "42",
                    "source_url": "https://example.com/42",
                    "original": False,
                }
            ]
        },
    ],
)
async def test_discovery_rejects_empty_or_incomplete_provider_contract(monkeypatch, payload):
    provider = HttpProvider(get_settings())

    async def request(_method, _path, **_kwargs):
        return payload

    monkeypatch.setattr(provider, "_request", request)

    with pytest.raises(ProviderExecutionError):
        await provider.discover(Platform.weibo, "https://weibo.com/u/12345")


@pytest.mark.asyncio
async def test_stage_requires_explicit_matching_manifest_counts(monkeypatch):
    provider = HttpProvider(get_settings())

    async def request(_method, _path, **_kwargs):
        return {
            "job_id": "a" * 32,
            "content_id": "42",
            "source_url": "https://m.weibo.cn/status/42",
            "published_at": "2026-07-15T08:00:00Z",
            "content_type": "image",
            "media": [],
            "expected_media_count": 1,
            "downloaded_media_count": 1,
            "complete": True,
        }

    monkeypatch.setattr(provider, "_request", request)

    with pytest.raises(ProviderExecutionError, match="incomplete"):
        await provider.stage(
            Platform.weibo,
            ContentRef("42", "https://m.weibo.cn/status/42"),
        )


@pytest.mark.asyncio
async def test_discovery_rejects_alias_collisions(monkeypatch):
    provider = HttpProvider(get_settings())

    async def request(_method, _path, **_kwargs):
        return {
            "items": [
                {
                    "remote_id": "canonical-a",
                    "source_url": "https://example.com/a",
                    "original": True,
                    "aliases": ["shared-legacy-id"],
                },
                {
                    "remote_id": "shared-legacy-id",
                    "source_url": "https://example.com/b",
                    "original": True,
                    "aliases": [],
                },
            ]
        }

    monkeypatch.setattr(provider, "_request", request)

    with pytest.raises(ProviderExecutionError, match="colliding"):
        await provider.discover(Platform.bilibili, "https://space.bilibili.com/123")


@pytest.mark.asyncio
async def test_stage_downloads_media_over_http_into_local_staging(monkeypatch, tmp_path):
    media_bytes = b"\x89PNG\r\n\x1a\n" + b"provider-media"
    digest = hashlib.sha256(media_bytes).hexdigest()
    settings = get_settings().model_copy(
        update={
            "provider_base_url": "http://provider.example",
            "provider_api_token": "provider-token-that-is-long-enough",
            "provider_staging_root": tmp_path,
        }
    )
    provider = HttpProvider(settings)

    async def request(method, path, **_kwargs):
        if method == "DELETE":
            assert path == "/v1/staging/remote_job_123"
            return {}
        assert method == "POST"
        assert path == "/v1/content/stage"
        return {
            "job_id": "remote_job_123",
            "platform": "weibo",
            "content_id": "42",
            "source_url": "https://m.weibo.cn/status/42",
            "published_at": "2026-07-15T08:00:00Z",
            "content_type": "image",
            "media": [
                {
                    "file_id": "image_1",
                    "kind": "image",
                    "mime_type": "image/png",
                    "size_bytes": len(media_bytes),
                    "sha256": digest,
                }
            ],
            "expected_media_count": 1,
            "complete": True,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer provider-token-that-is-long-enough"
        assert request.url.path == "/v1/staging/remote_job_123/files/image_1"
        return httpx.Response(
            200,
            headers={
                "content-type": "image/png",
                "content-length": str(len(media_bytes)),
            },
            content=media_bytes,
        )

    monkeypatch.setattr(provider, "_request", request)
    monkeypatch.setattr(
        provider,
        "_client",
        lambda: httpx.AsyncClient(
            base_url="http://provider.example",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer provider-token-that-is-long-enough"},
        ),
    )
    ref = ContentRef("42", "https://m.weibo.cn/status/42")

    staged = await provider.stage(Platform.weibo, ref)

    assert staged.downloaded_media_count == 1
    assert (staged.local_root / staged.media[0]["local_path"]).read_bytes() == media_bytes
    await provider.cleanup(staged)
    assert not staged.local_root.exists()
