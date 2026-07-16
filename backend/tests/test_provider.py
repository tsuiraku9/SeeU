from __future__ import annotations

import pytest

from app.config import get_settings
from app.models import Platform
from app.provider import CrawlerProvider, ProviderExecutionError


@pytest.mark.asyncio
async def test_discovery_orders_complete_publication_times_newest_first(monkeypatch):
    provider = CrawlerProvider(get_settings())

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
    provider = CrawlerProvider(get_settings())

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
    provider = CrawlerProvider(get_settings())

    async def request(_method, _path, **_kwargs):
        return payload

    monkeypatch.setattr(provider, "_request", request)

    with pytest.raises(ProviderExecutionError):
        await provider.discover(Platform.weibo, "https://weibo.com/u/12345")


@pytest.mark.asyncio
async def test_stage_requires_explicit_matching_manifest_counts(monkeypatch):
    provider = CrawlerProvider(get_settings())

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

    from app.adapters.base import ContentRef

    with pytest.raises(ProviderExecutionError, match="counts"):
        await provider.stage(
            Platform.weibo,
            ContentRef("42", "https://m.weibo.cn/status/42"),
        )


@pytest.mark.asyncio
async def test_discovery_rejects_alias_collisions(monkeypatch):
    provider = CrawlerProvider(get_settings())

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
