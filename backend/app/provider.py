from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .adapters.base import AdapterError, ContentRef, parse_datetime
from .config import Settings
from .models import Platform


class ProviderError(AdapterError):
    code = "provider_error"


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"


class LoginRequiredError(ProviderError):
    code = "login_required"


class ProviderExecutionError(ProviderError):
    code = "provider_execution_failed"


def _published_timestamp(value: Any) -> float | None:
    """Return a sortable UTC timestamp without inventing dates for missing values."""
    if isinstance(value, bool) or value is None or value == "" or value == 0 or value == "0":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return stamp if stamp > 0 else None
    elif isinstance(value, str):
        stripped = value.strip()
        try:
            stamp = float(stripped)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            if stamp > 10_000_000_000:
                stamp /= 1000
            return stamp if stamp > 0 else None
    else:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).timestamp()


@dataclass(slots=True)
class StagedContent:
    job_id: str
    platform: Platform
    remote_id: str
    source_url: str
    title: str
    author: str
    text: str
    published_at: datetime
    content_type: str
    media: list[dict[str, Any]]
    expected_media_count: int
    downloaded_media_count: int
    complete: bool


class CrawlerProvider:
    """Typed client for the Docker-internal crawler bridge.

    The bridge never returns browser storage or cookies. Error bodies are kept
    deliberately small so signed media query strings do not reach application logs.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        if not self.settings.crawler_provider_enabled:
            raise ProviderUnavailableError("Crawler provider is disabled")
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.crawler_bridge_url,
                timeout=self.settings.crawler_request_timeout_seconds,
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("Crawler provider is unavailable") from exc
        if response.status_code == 401:
            raise LoginRequiredError("Platform session is not authenticated; scan the QR code or open manual verification")
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", "Crawler provider request failed"))
            except Exception:
                detail = "Crawler provider request failed"
            detail = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[query-redacted]", detail)[:1000]
            if response.status_code >= 500:
                raise ProviderExecutionError(detail)
            raise ProviderError(detail)
        return response.json() if response.content else None

    async def sessions(self) -> list[dict]:
        return await self._request("GET", "/v1/sessions")

    async def login(self, platform: Platform) -> dict:
        return await self._request("POST", f"/v1/sessions/{platform.value}/login")

    async def qr(self, platform: Platform) -> dict:
        return await self._request("GET", f"/v1/sessions/{platform.value}/qr")

    async def logout(self, platform: Platform) -> dict:
        return await self._request("DELETE", f"/v1/sessions/{platform.value}")

    async def discover(self, platform: Platform, profile_url: str, limit: int | None = None) -> list[ContentRef]:
        requested_limit = min(limit or self.settings.crawler_discovery_limit, self.settings.crawler_discovery_limit)
        payload = await self._request(
            "POST", "/v1/creators/discover",
            json={"platform": platform.value, "profile_url": profile_url, "limit": requested_limit},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ProviderExecutionError("Crawler provider returned an invalid discovery response")
        candidates: list[tuple[dict[str, Any], str, str, tuple[str, ...]]] = []
        seen: set[str] = set()
        identity_owners: dict[str, str] = {}
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                raise ProviderExecutionError("Crawler provider returned an invalid content reference")
            remote_id = str(item.get("remote_id", "")).strip()
            source_url = str(item.get("source_url", "")).strip()
            if not remote_id or not source_url or item.get("original") is not True:
                raise ProviderExecutionError("Crawler provider returned an incomplete content reference")
            raw_aliases = item.get("aliases", [])
            if (
                not isinstance(raw_aliases, list)
                or len(raw_aliases) > 10
                or any(
                    not isinstance(alias, str)
                    or not alias.strip()
                    or len(alias.strip()) > 256
                    for alias in raw_aliases
                )
            ):
                raise ProviderExecutionError("Crawler provider returned invalid content aliases")
            aliases = tuple(
                dict.fromkeys(
                    alias.strip()
                    for alias in raw_aliases
                    if alias.strip() != remote_id
                )
            )
            if remote_id in seen:
                continue
            for identity in (remote_id, *aliases):
                owner = identity_owners.get(identity)
                if owner is not None and owner != remote_id:
                    raise ProviderExecutionError(
                        "Crawler provider returned colliding content aliases"
                    )
                identity_owners[identity] = remote_id
            seen.add(remote_id)
            candidates.append((item, remote_id, source_url, aliases))

        if not candidates:
            raise ProviderExecutionError("Crawler provider returned no recognizable creator content")

        # The crawler normally returns newest-first. When every item includes a
        # usable publication time, enforce that contract explicitly. If any time
        # is missing, preserve provider order instead of guessing.
        timestamps = [
            _published_timestamp(item.get("published_at"))
            for item, _, _, _ in candidates
        ]
        if timestamps and all(timestamp is not None for timestamp in timestamps):
            candidates = [
                candidate
                for _, candidate in sorted(
                    zip(timestamps, candidates), key=lambda pair: pair[0] or 0, reverse=True
                )
            ]
        truncated = bool(payload.get("truncated", len(candidates) >= requested_limit))
        refs = [
            ContentRef(
                remote_id,
                source_url,
                pinned=item.get("pinned") is True,
                window_truncated=truncated,
                aliases=aliases,
            )
            for item, remote_id, source_url, aliases in candidates
        ]
        return refs[:requested_limit]

    async def stage(self, platform: Platform, ref: ContentRef) -> StagedContent:
        payload = await self._request(
            "POST", "/v1/content/stage",
            json={"platform": platform.value, "content_id": ref.remote_id, "source_url": ref.source_url},
        )
        if not isinstance(payload, dict):
            raise ProviderExecutionError("Crawler provider returned an invalid staging response")
        job_id = str(payload.get("job_id") or "")
        remote_id = str(payload.get("content_id") or "")
        source_url = str(payload.get("source_url") or "")
        raw_media = payload.get("media")
        if (
            not re.fullmatch(r"[0-9a-f]{32}", job_id)
            or remote_id != ref.remote_id
            or not source_url
            or not isinstance(raw_media, list)
            or any(not isinstance(record, dict) for record in raw_media)
        ):
            raise ProviderExecutionError("Crawler provider returned an incomplete staging response")
        media = list(raw_media)
        try:
            expected = int(payload["expected_media_count"])
            downloaded = int(payload["downloaded_media_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderExecutionError("Crawler provider returned invalid media counts") from exc
        if expected < 0 or downloaded < 0 or downloaded != len(media):
            raise ProviderExecutionError("Crawler provider media counts do not match its manifest")
        complete = payload.get("complete")
        if not isinstance(complete, bool):
            raise ProviderExecutionError("Crawler provider did not declare staging completeness")
        return StagedContent(
            job_id=job_id, platform=platform,
            remote_id=remote_id,
            source_url=source_url,
            title=str(payload.get("title") or ref.remote_id), author=str(payload.get("author") or ""),
            text=str(payload.get("text") or ""), published_at=parse_datetime(payload.get("published_at")),
            content_type=str(payload.get("content_type") or "unknown"), media=media,
            expected_media_count=expected,
            downloaded_media_count=downloaded,
            complete=complete,
        )

    async def cleanup(self, job_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            return
        try:
            await self._request("DELETE", f"/v1/staging/{job_id}")
        except ProviderError:
            pass
