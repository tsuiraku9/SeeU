from __future__ import annotations

import hashlib
import mimetypes
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .adapters.base import AdapterError, ContentRef
from .config import Settings
from .models import Platform


class ProviderError(AdapterError):
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        phase: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or type(self).code
        self.phase = phase
        self.retryable = retryable


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"


class LoginRequiredError(ProviderError):
    code = "login_required"


class ProviderExecutionError(ProviderError):
    code = "provider_execution_failed"


def _published_timestamp(value: Any) -> float | None:
    """Return a sortable UTC timestamp without inventing missing dates."""

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


_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ALLOWED_MEDIA_PREFIXES = ("image/", "video/", "audio/")
_MAX_PROVIDER_JSON_BYTES = 4 * 1024 * 1024
_SESSION_STATUSES = {
    "logged_out",
    "starting",
    "qr_ready",
    "authenticated",
    "expired",
    "manual_verification_required",
    "error",
}


@dataclass(slots=True)
class StagedContent:
    remote_job_id: str
    local_root: Path
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


class HttpProvider:
    """Client for an optional external provider implementing SeeU contract v1.

    The provider is a separate service and never receives a SeeU data-directory
    mount. Media crosses the boundary through authenticated HTTP responses and is
    verified locally before the archive manager can publish it.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _client(self) -> httpx.AsyncClient:
        if not self.settings.provider_configured:
            raise ProviderUnavailableError("External provider is not configured")
        return httpx.AsyncClient(
            base_url=self.settings.provider_base_url,
            timeout=self.settings.provider_request_timeout_seconds,
            headers={
                "Authorization": f"Bearer {self.settings.provider_api_token}",
                "User-Agent": "SeeU-Provider-Client/1",
            },
            follow_redirects=False,
        )

    @staticmethod
    def _redact_detail(value: object) -> str:
        detail = re.sub(
            r"(https?://[^\s?]+)\?[^\s]+",
            r"\1?[query-redacted]",
            str(value),
        )
        return detail[:1000]

    @staticmethod
    def _valid_http_url(value: str) -> bool:
        if not value or len(value) > 4096:
            return False
        parsed = urlsplit(value)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )

    @classmethod
    def _raise_response_error(cls, response: httpx.Response) -> None:
        try:
            raw_detail = response.json().get("detail", "External provider request failed")
        except Exception:
            raw_detail = "External provider request failed"
        if isinstance(raw_detail, dict):
            detail = cls._redact_detail(
                raw_detail.get("message") or "External provider request failed"
            )
            error_code = str(raw_detail.get("code") or "").strip() or None
            phase = str(raw_detail.get("phase") or "").strip() or None
            retryable_value = raw_detail.get("retryable")
            retryable = retryable_value if isinstance(retryable_value, bool) else None
        else:
            detail = cls._redact_detail(raw_detail)
            error_code = None
            phase = None
            retryable = None
        metadata = {
            "code": error_code,
            "phase": phase,
            "retryable": retryable,
        }
        if error_code == LoginRequiredError.code:
            raise LoginRequiredError(detail, **metadata)
        if response.status_code in {401, 403}:
            raise ProviderUnavailableError(
                "External provider rejected its API credentials",
                code=error_code or "provider_auth_failed",
                phase=phase,
                retryable=False,
            )
        if response.status_code >= 500:
            raise ProviderExecutionError(detail, **metadata)
        raise ProviderError(detail, **metadata)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._client() as client:
                response = await client.request(method, path, **kwargs)
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError("External provider is unavailable") from exc
        if response.status_code >= 300:
            self._raise_response_error(response)
        if not response.content:
            return None
        if len(response.content) > _MAX_PROVIDER_JSON_BYTES:
            raise ProviderExecutionError(
                "External provider JSON response exceeds the configured contract limit"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderExecutionError(
                "External provider returned invalid JSON"
            ) from exc

    @staticmethod
    def _validate_session_payload(
        payload: Any,
        *,
        expected_platform: Platform | None = None,
        allow_image: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ProviderExecutionError(
                "External provider returned an invalid session response"
            )
        try:
            platform = Platform(str(payload.get("platform") or ""))
        except ValueError as exc:
            raise ProviderExecutionError(
                "External provider returned an unknown session platform"
            ) from exc
        status = str(payload.get("status") or "")
        if expected_platform is not None and platform != expected_platform:
            raise ProviderExecutionError(
                "External provider returned a mismatched session platform"
            )
        if status not in _SESSION_STATUSES:
            raise ProviderExecutionError(
                "External provider returned an unknown session status"
            )
        message_value = payload.get("message")
        message = None if message_value is None else str(message_value)[:1000]
        updated_value = payload.get("updated_at")
        updated_at = None if updated_value is None else str(updated_value)[:64]
        manual_url = str(payload.get("manual_verification_url") or "")
        if manual_url:
            parsed = urlsplit(manual_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or len(manual_url) > 2048
            ):
                raise ProviderExecutionError(
                    "External provider returned an unsafe manual verification URL"
                )
        result: dict[str, Any] = {
            "platform": platform.value,
            "status": status,
            "updated_at": updated_at,
            "message": message,
            "manual_verification_url": manual_url,
        }
        image_data_url = payload.get("image_data_url")
        if image_data_url is not None:
            if (
                not allow_image
                or not isinstance(image_data_url, str)
                or len(image_data_url) > 2_800_000
                or not re.fullmatch(
                    r"data:image/(?:png|jpeg);base64,[A-Za-z0-9+/=\r\n]+",
                    image_data_url,
                )
            ):
                raise ProviderExecutionError(
                    "External provider returned an invalid QR image"
                )
            result["image_data_url"] = image_data_url
        return result

    async def sessions(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/v1/sessions")
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ProviderExecutionError(
                "External provider returned an invalid sessions response"
            )
        sessions = [self._validate_session_payload(item) for item in payload]
        platforms = [session["platform"] for session in sessions]
        if len(platforms) != len(set(platforms)):
            raise ProviderExecutionError(
                "External provider returned duplicate session platforms"
            )
        return sessions

    async def login(self, platform: Platform) -> dict[str, Any]:
        payload = await self._request(
            "POST", f"/v1/sessions/{platform.value}/login"
        )
        return self._validate_session_payload(
            payload,
            expected_platform=platform,
            allow_image=True,
        )

    async def qr(self, platform: Platform) -> dict[str, Any]:
        payload = await self._request("GET", f"/v1/sessions/{platform.value}/qr")
        return self._validate_session_payload(
            payload,
            expected_platform=platform,
            allow_image=True,
        )

    async def logout(self, platform: Platform) -> dict[str, Any]:
        payload = await self._request("DELETE", f"/v1/sessions/{platform.value}")
        return self._validate_session_payload(payload, expected_platform=platform)

    async def discover(
        self,
        platform: Platform,
        profile_url: str,
        limit: int | None = None,
    ) -> list[ContentRef]:
        requested_limit = min(
            limit or self.settings.provider_discovery_limit,
            self.settings.provider_discovery_limit,
        )
        payload = await self._request(
            "POST",
            "/v1/creators/discover",
            json={
                "platform": platform.value,
                "profile_url": profile_url,
                "limit": requested_limit,
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ProviderExecutionError(
                "External provider returned an invalid discovery response"
            )
        candidates: list[tuple[dict[str, Any], str, str, tuple[str, ...]]] = []
        seen: set[str] = set()
        identity_owners: dict[str, str] = {}
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                raise ProviderExecutionError(
                    "External provider returned an invalid content reference"
                )
            remote_id = str(item.get("remote_id", "")).strip()
            source_url = str(item.get("source_url", "")).strip()
            if (
                not remote_id
                or len(remote_id) > 256
                or any(not character.isprintable() for character in remote_id)
                or not self._valid_http_url(source_url)
                or item.get("original") is not True
            ):
                raise ProviderExecutionError(
                    "External provider returned an incomplete content reference"
                )
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
                raise ProviderExecutionError(
                    "External provider returned invalid content aliases"
                )
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
                        "External provider returned colliding content aliases"
                    )
                identity_owners[identity] = remote_id
            seen.add(remote_id)
            candidates.append((item, remote_id, source_url, aliases))

        if not candidates:
            raise ProviderExecutionError(
                "External provider returned no recognizable creator content"
            )

        timestamps = [
            _published_timestamp(item.get("published_at"))
            for item, _, _, _ in candidates
        ]
        if timestamps and all(timestamp is not None for timestamp in timestamps):
            candidates = [
                candidate
                for _, candidate in sorted(
                    zip(timestamps, candidates),
                    key=lambda pair: pair[0] or 0,
                    reverse=True,
                )
            ]
        truncated = bool(payload.get("truncated", len(candidates) >= requested_limit))
        return [
            ContentRef(
                remote_id,
                source_url,
                pinned=item.get("pinned") is True,
                window_truncated=truncated,
                aliases=aliases,
            )
            for item, remote_id, source_url, aliases in candidates[:requested_limit]
        ]

    @staticmethod
    def _media_suffix(mime_type: str) -> str:
        suffix = mimetypes.guess_extension(mime_type) or ".bin"
        return ".jpg" if suffix == ".jpe" else suffix

    async def _download_media_file(
        self,
        remote_job_id: str,
        record: dict[str, Any],
        target: Path,
    ) -> dict[str, Any]:
        file_id = str(record.get("file_id") or "")
        mime_type = str(record.get("mime_type") or "").lower()
        expected_hash = str(record.get("sha256") or "").lower()
        expected_size = int(record.get("size_bytes", -1))
        digest = hashlib.sha256()
        written = 0
        try:
            async with self._client() as client:
                async with client.stream(
                    "GET",
                    f"/v1/staging/{remote_job_id}/files/{file_id}",
                    headers={"Accept": mime_type},
                ) as response:
                    if response.status_code >= 300:
                        await response.aread()
                        self._raise_response_error(response)
                    declared_length = response.headers.get("content-length")
                    if declared_length is not None:
                        try:
                            length = int(declared_length)
                        except ValueError as exc:
                            raise ProviderExecutionError(
                                "External provider media returned invalid Content-Length"
                            ) from exc
                        if length != expected_size:
                            raise ProviderExecutionError(
                                "External provider media Content-Length does not match its manifest"
                            )
                    content_encoding = response.headers.get("content-encoding", "").lower()
                    if content_encoding not in {"", "identity"}:
                        raise ProviderExecutionError(
                            "External provider media must not use content encoding"
                        )
                    response_type = response.headers.get("content-type", "").partition(";")[0].lower()
                    if response_type and response_type != mime_type:
                        raise ProviderExecutionError(
                            "External provider media Content-Type does not match its manifest"
                        )
                    with target.open("xb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            written += len(chunk)
                            if written > expected_size:
                                raise ProviderExecutionError(
                                    "External provider media exceeds its declared size"
                                )
                            handle.write(chunk)
                            digest.update(chunk)
        except ProviderError:
            target.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            target.unlink(missing_ok=True)
            raise ProviderUnavailableError(
                "External provider media download failed"
            ) from exc
        if written != expected_size or digest.hexdigest() != expected_hash:
            target.unlink(missing_ok=True)
            raise ProviderExecutionError(
                "External provider media failed size or SHA-256 validation"
            )
        return {
            "kind": str(record.get("kind") or mime_type.partition("/")[0]),
            "local_path": target.name,
            "mime_type": mime_type,
            "size_bytes": expected_size,
            "sha256": expected_hash,
        }

    def _validate_stage_manifest(
        self,
        payload: Any,
        platform: Platform,
        ref: ContentRef,
    ) -> tuple[str, list[dict[str, Any]], int]:
        if not isinstance(payload, dict):
            raise ProviderExecutionError(
                "External provider returned an invalid staging response"
            )
        remote_job_id = str(payload.get("job_id") or "")
        remote_id = str(payload.get("content_id") or "")
        source_url = str(payload.get("source_url") or "")
        raw_media = payload.get("media")
        if (
            not _OPAQUE_ID.fullmatch(remote_job_id)
            or remote_id != ref.remote_id
            or source_url != ref.source_url
            or not self._valid_http_url(source_url)
            or payload.get("platform") not in {None, platform.value}
            or not isinstance(raw_media, list)
            or any(not isinstance(record, dict) for record in raw_media)
        ):
            raise ProviderExecutionError(
                "External provider returned an incomplete staging response"
            )
        if len(raw_media) > self.settings.import_max_files:
            raise ProviderExecutionError(
                "External provider media count exceeds the configured limit"
            )
        try:
            raw_expected = payload["expected_media_count"]
            if isinstance(raw_expected, bool):
                raise TypeError
            expected = int(raw_expected)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderExecutionError(
                "External provider returned an invalid expected media count"
            ) from exc
        if (
            expected < 0
            or expected != len(raw_media)
            or payload.get("complete") is not True
        ):
            raise ProviderExecutionError(
                "External provider declared incomplete media"
            )
        seen_file_ids: set[str] = set()
        total_size = 0
        media = list(raw_media)
        for record in media:
            file_id = str(record.get("file_id") or "")
            kind = str(record.get("kind") or "").lower()
            mime_type = str(record.get("mime_type") or "").lower()
            expected_hash = str(record.get("sha256") or "").lower()
            try:
                raw_size = record.get("size_bytes", -1)
                if isinstance(raw_size, bool):
                    raise TypeError
                expected_size = int(raw_size)
            except (TypeError, ValueError) as exc:
                raise ProviderExecutionError(
                    "External provider returned an invalid media size"
                ) from exc
            if (
                not _OPAQUE_ID.fullmatch(file_id)
                or file_id in seen_file_ids
                or kind not in {"image", "video", "audio"}
                or not mime_type.startswith(_ALLOWED_MEDIA_PREFIXES)
                or mime_type.partition("/")[0] != kind
                or expected_size <= 0
                or not _SHA256.fullmatch(expected_hash)
            ):
                raise ProviderExecutionError(
                    "External provider returned an invalid media manifest"
                )
            seen_file_ids.add(file_id)
            total_size += expected_size
            if total_size > self.settings.media_max_bytes:
                raise ProviderExecutionError(
                    "External provider media exceeds the configured byte limit"
                )
        return remote_job_id, media, expected

    async def stage(self, platform: Platform, ref: ContentRef) -> StagedContent:
        payload = await self._request(
            "POST",
            "/v1/content/stage",
            json={
                "platform": platform.value,
                "content_id": ref.remote_id,
                "source_url": ref.source_url,
            },
        )
        remote_job_hint = (
            str(payload.get("job_id") or "") if isinstance(payload, dict) else ""
        )
        try:
            remote_job_id, remote_media, expected = self._validate_stage_manifest(
                payload, platform, ref
            )
        except BaseException:
            if _OPAQUE_ID.fullmatch(remote_job_hint):
                await self._cleanup_remote(remote_job_hint)
            raise
        assert isinstance(payload, dict)
        title = str(payload.get("title") or ref.remote_id)
        author = str(payload.get("author") or "")
        text = str(payload.get("text") or "")
        if len(title) > 500 or len(author) > 160 or len(text) > 2_000_000:
            await self._cleanup_remote(remote_job_id)
            raise ProviderExecutionError(
                "External provider content metadata exceeds the contract limit"
            )
        published_timestamp = _published_timestamp(payload.get("published_at"))
        if published_timestamp is None:
            await self._cleanup_remote(remote_job_id)
            raise ProviderExecutionError(
                "External provider content is missing a valid publication time"
            )
        published_at = datetime.fromtimestamp(published_timestamp, tz=timezone.utc)
        local_root = (
            self.settings.provider_staging_root / f"http-{secrets.token_hex(16)}"
        ).resolve()
        staging_root = self.settings.provider_staging_root.resolve()
        if staging_root != local_root.parent:
            raise ProviderExecutionError("Invalid local provider staging path")
        local_root.mkdir(parents=False, exist_ok=False)
        downloaded: list[dict[str, Any]] = []
        try:
            for index, record in enumerate(remote_media, start=1):
                mime_type = str(record["mime_type"]).lower()
                target = local_root / f"{index:03d}{self._media_suffix(mime_type)}"
                downloaded.append(
                    await self._download_media_file(
                        remote_job_id,
                        record,
                        target,
                    )
                )
        except BaseException:
            shutil.rmtree(local_root, ignore_errors=True)
            await self._cleanup_remote(remote_job_id)
            raise
        return StagedContent(
            remote_job_id=remote_job_id,
            local_root=local_root,
            platform=platform,
            remote_id=str(payload["content_id"]),
            source_url=str(payload["source_url"]),
            title=title,
            author=author,
            text=text,
            published_at=published_at,
            content_type=str(payload.get("content_type") or "unknown"),
            media=downloaded,
            expected_media_count=expected,
            downloaded_media_count=len(downloaded),
            complete=True,
        )

    async def _cleanup_remote(self, remote_job_id: str) -> None:
        if not _OPAQUE_ID.fullmatch(remote_job_id):
            return
        try:
            await self._request("DELETE", f"/v1/staging/{remote_job_id}")
        except ProviderError:
            pass

    async def cleanup(self, staged: StagedContent) -> None:
        staging_root = self.settings.provider_staging_root.resolve()
        local_root = staged.local_root.resolve()
        if staging_root == local_root.parent:
            shutil.rmtree(local_root, ignore_errors=True)
        await self._cleanup_remote(staged.remote_job_id)
