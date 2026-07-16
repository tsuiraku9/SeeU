from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import signal
import shutil
import socket
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .adapters.base import MediaCandidate, NormalizedContent, USER_AGENT
from .config import Settings


class InsufficientStorageError(RuntimeError):
    pass


class ArchiveError(RuntimeError):
    pass


def sanitize_component(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-._")
    return cleaned[:120] or fallback


def markdown_escape(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


ALLOWED_MEDIA_PREFIXES = ("image/", "video/", "audio/")


def _media_family(mime_type: str) -> str:
    return mime_type.partition("/")[0].lower() if mime_type.startswith(ALLOWED_MEDIA_PREFIXES) else ""


def _sniff_media_family(path: Path, declared_family: str = "") -> str | None:
    """Recognize common media containers and reject obvious HTML/error bodies."""

    with path.open("rb") as handle:
        head = handle.read(64)
    lowered = head.lstrip().lower()
    if lowered.startswith((b"<!doctype html", b"<html", b"<?xml")):
        return "document"
    if head.startswith(b"\xff\xd8\xff") or head.startswith((b"GIF87a", b"GIF89a")):
        return "image"
    if head.startswith(b"\x89PNG\r\n\x1a\n") or (head.startswith(b"RIFF") and head[8:12] == b"WEBP"):
        return "image"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12].lower()
        if brand in {b"avif", b"avis", b"heic", b"heix", b"mif1", b"msf1"}:
            return "image"
        if declared_family == "audio" or brand in {b"m4a ", b"m4b ", b"f4a ", b"f4b "}:
            return "audio"
        return "video"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video"
    if (
        head.startswith((b"ID3", b"fLaC", b"OggS"))
        or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
        or (head.startswith(b"RIFF") and head[8:12] == b"WAVE")
    ):
        return "audio"
    if head.startswith((b"FLV", b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "video"
    return None


class ArchiveManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.archive_root.resolve()
        self.semaphore = asyncio.Semaphore(settings.download_concurrency)

    def storage_status(self) -> dict[str, int | bool]:
        usage = shutil.disk_usage(self.root)
        archive_bytes = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                try:
                    archive_bytes += path.stat().st_size
                except OSError:
                    continue
        minimum = int(self.settings.min_free_disk_gb * 1024**3)
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "archive_bytes": archive_bytes,
            "minimum_free_bytes": minimum,
            "downloads_paused": usage.free < minimum,
        }

    def _assert_storage(self) -> None:
        free_bytes = shutil.disk_usage(self.root).free
        minimum = int(self.settings.min_free_disk_gb * 1024**3)
        if free_bytes < minimum:
            raise InsufficientStorageError(
                f"Free disk space is below {self.settings.min_free_disk_gb:g} GiB; downloads are paused"
            )

    @staticmethod
    def _normalized_content_type(content_type: str, media_records: list[dict]) -> str:
        value = content_type.strip().lower()
        if value in {"image", "video", "audio", "text"}:
            return value
        kinds = {str(record.get("kind") or "").lower() for record in media_records}
        for kind in ("video", "image", "audio"):
            if kind in kinds:
                return kind
        return "unknown"

    def _validate_existing_archive(
        self,
        target: Path,
        content: NormalizedContent,
        expected_media_count: int | None = None,
    ) -> tuple[Path, dict]:
        metadata_path = target / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArchiveError(f"Existing archive metadata is invalid: {target}") from exc
        if not (target / "content.md").is_file():
            raise ArchiveError(f"Existing archive is missing content.md: {target}")
        if (
            metadata.get("status") != "complete"
            or str(metadata.get("platform")) != content.platform.value
            or str(metadata.get("content_id")) != content.remote_id
        ):
            raise ArchiveError(f"Existing archive does not match requested content: {target}")
        media = metadata.get("media")
        if not isinstance(media, list):
            raise ArchiveError("Existing archive media manifest is invalid")
        expected = int(metadata.get("expected_media_count", len(media)))
        verified = int(metadata.get("verified_media_count", len(media)))
        if expected_media_count is not None and expected != expected_media_count:
            raise ArchiveError("Existing archive media count differs from provider manifest")
        if expected != len(media) or verified != len(media):
            raise ArchiveError("Existing archive is not media-complete")
        normalized_type = self._normalized_content_type(str(metadata.get("content_type", "unknown")), media)
        if expected == 0 and normalized_type != "text":
            raise ArchiveError("Existing non-text archive has no media")
        self._validate_manifest_files(target, media)
        # Return a normalized view for legacy schema-v1 archives without
        # rewriting their canonical metadata during a read/rebuild operation.
        metadata["content_type"] = normalized_type
        metadata.setdefault("expected_media_count", expected)
        metadata.setdefault("verified_media_count", verified)
        metadata.setdefault("integrity_status", "complete")
        return target, metadata

    def _validate_manifest_files(self, source_root: Path, media_records: list[dict]) -> None:
        source_root = source_root.resolve()
        total_bytes = 0
        seen_paths: set[str] = set()
        for record in media_records:
            relative = Path(str(record.get("local_path") or record.get("path") or ""))
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise ArchiveError("Media path must be a safe relative path")
            path_key = relative.as_posix().casefold()
            if path_key in seen_paths:
                raise ArchiveError("Media manifest contains duplicate paths")
            seen_paths.add(path_key)
            lexical_source = source_root / relative
            current = source_root
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise ArchiveError("Media manifest must not contain symlinked paths")
            source = lexical_source.resolve()
            if source_root != source.parent and source_root not in source.parents:
                raise ArchiveError("Media path escapes staging directory")
            if not source.is_file():
                raise ArchiveError(f"Staged media file is missing: {relative.as_posix()}")
            expected_size = int(record.get("size_bytes", -1))
            actual_size = source.stat().st_size
            if actual_size <= 0 or expected_size != actual_size:
                raise ArchiveError(f"Media size mismatch: {relative.as_posix()}")
            total_bytes += actual_size
            if total_bytes > self.settings.media_max_bytes:
                raise ArchiveError("Media exceeds configured byte limit")
            digest = hashlib.sha256()
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            expected_hash = str(record.get("sha256", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or digest.hexdigest() != expected_hash:
                raise ArchiveError(f"Media SHA-256 mismatch: {relative.as_posix()}")
            mime_type = str(record.get("mime_type") or mimetypes.guess_type(source.name)[0] or "")
            family = _media_family(mime_type)
            if not family:
                raise ArchiveError(f"Unsupported media type: {mime_type or 'unknown'}")
            sniffed = _sniff_media_family(source, family)
            if sniffed is None or sniffed == "document" or sniffed != family:
                raise ArchiveError(f"Media content does not match declared type: {relative.as_posix()}")

    def _target_directory(self, content: NormalizedContent, account_slug: str) -> Path:
        published = content.published_at.astimezone(timezone.utc)
        return (
            self.root
            / content.platform.value
            / sanitize_component(account_slug, "account")
            / f"{published.year:04d}"
            / f"{published.month:02d}"
            / sanitize_component(content.remote_id, "content")
        )

    async def archive(self, content: NormalizedContent, account_slug: str) -> tuple[Path, dict]:
        self._assert_storage()
        target = self._target_directory(content, account_slug)
        if target.exists():
            return self._validate_existing_archive(target, content, len(content.media))
        if not content.media and content.content_type.lower() != "text":
            raise ArchiveError("Non-text content has no downloadable media")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
        temporary.mkdir(parents=False)
        media_dir = temporary / "media"
        media_dir.mkdir()
        try:
            media_records = []
            for index, candidate in enumerate(content.media, start=1):
                record = await self._download_candidate(candidate, media_dir, index)
                media_records.extend(record)
            if len(media_records) < len(content.media):
                raise ArchiveError("One or more expected media files were not downloaded")
            self._validate_manifest_files(temporary, media_records)
            content_type = self._normalized_content_type(content.content_type, media_records)
            metadata = {
                "schema_version": 2,
                "platform": content.platform.value,
                "content_id": content.remote_id,
                "source_url": content.source_url,
                "title": content.title,
                "author": content.author,
                "text": content.text,
                "content_type": content_type,
                "published_at": content.published_at.astimezone(timezone.utc).isoformat(),
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "status": "complete",
                "integrity_status": "complete",
                "expected_media_count": len(media_records),
                "verified_media_count": len(media_records),
                "media": media_records,
            }
            markdown = (
                f"# {markdown_escape(content.title)}\n\n"
                f"- 作者：{markdown_escape(content.author or '未知')}\n"
                f"- 平台：{content.platform.value}\n"
                f"- 发布时间：{content.published_at.astimezone(timezone.utc).isoformat()}\n"
                f"- 来源：{content.source_url}\n\n"
                f"{markdown_escape(content.text)}\n"
            )
            (temporary / "content.md").write_text(markdown, encoding="utf-8")
            (temporary / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, target)
            return target, metadata
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def archive_from_files(
        self,
        content: NormalizedContent,
        account_slug: str,
        source_root: Path,
        media_records: list[dict],
        *,
        expected_media_count: int | None = None,
        provider_complete: bool = True,
    ) -> tuple[Path, dict]:
        """Validate provider/import files and atomically promote a complete archive."""
        self._assert_storage()
        if len(media_records) > self.settings.import_max_files:
            raise ArchiveError("Media file count exceeds configured limit")
        expected = len(media_records) if expected_media_count is None else expected_media_count
        if expected < 0 or expected != len(media_records) or not provider_complete:
            raise ArchiveError(
                f"Provider media is incomplete: expected {expected}, received {len(media_records)}"
            )
        content_type = self._normalized_content_type(content.content_type, media_records)
        if expected == 0 and content_type != "text":
            raise ArchiveError("Non-text content has no downloadable media")
        source_root = source_root.resolve()
        target = self._target_directory(content, account_slug)
        if target.exists():
            return self._validate_existing_archive(target, content, expected)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
        media_dir = temporary / "media"
        media_dir.mkdir(parents=True)
        validated: list[dict] = []
        total_bytes = 0
        try:
            seen_paths: set[str] = set()
            for index, record in enumerate(media_records, start=1):
                relative = Path(str(record.get("local_path") or record.get("path") or ""))
                if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                    raise ArchiveError("Media path must be a safe relative path")
                path_key = relative.as_posix().casefold()
                if path_key in seen_paths:
                    raise ArchiveError("Media manifest contains duplicate paths")
                seen_paths.add(path_key)
                lexical_source = source_root / relative
                current = source_root
                for part in relative.parts:
                    current /= part
                    if current.is_symlink():
                        raise ArchiveError("Media manifest must not contain symlinked paths")
                source = lexical_source.resolve()
                if source_root != source.parent and source_root not in source.parents:
                    raise ArchiveError("Media path escapes staging directory")
                if not source.is_file():
                    raise ArchiveError(f"Staged media file is missing: {relative.as_posix()}")
                expected_size = int(record.get("size_bytes", -1))
                actual_size = source.stat().st_size
                if expected_size != actual_size:
                    raise ArchiveError(f"Media size mismatch: {relative.as_posix()}")
                total_bytes += actual_size
                if total_bytes > self.settings.media_max_bytes:
                    raise ArchiveError("Import exceeds configured byte limit")
                digest = hashlib.sha256()
                with source.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                expected_hash = str(record.get("sha256", "")).lower()
                if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or digest.hexdigest() != expected_hash:
                    raise ArchiveError(f"Media SHA-256 mismatch: {relative.as_posix()}")
                mime_type = str(record.get("mime_type") or mimetypes.guess_type(source.name)[0] or "")
                family = _media_family(mime_type)
                if not family:
                    raise ArchiveError(f"Unsupported media type: {mime_type or 'unknown'}")
                sniffed = _sniff_media_family(source, family)
                if sniffed is None or sniffed == "document" or sniffed != family:
                    raise ArchiveError(f"Media content does not match declared type: {relative.as_posix()}")
                suffix = source.suffix.lower()
                suffix_type = mimetypes.guess_type(f"file{suffix}")[0] if suffix else None
                if not suffix_type or _media_family(suffix_type) != family:
                    suffix = mimetypes.guess_extension(mime_type) or ".bin"
                name = f"{index:02d}-{sanitize_component(str(record.get('kind') or 'media'))}{suffix}"
                shutil.copyfile(source, media_dir / name)
                validated.append({
                    "kind": str(record.get("kind") or mime_type.split("/", 1)[0]),
                    "local_path": f"media/{name}", "mime_type": mime_type,
                    "size_bytes": actual_size, "sha256": expected_hash,
                })
            metadata = {
                "schema_version": 2, "platform": content.platform.value,
                "content_id": content.remote_id, "source_url": content.source_url,
                "title": content.title, "author": content.author, "text": content.text,
                "content_type": content_type,
                "published_at": content.published_at.astimezone(timezone.utc).isoformat(),
                "collected_at": datetime.now(timezone.utc).isoformat(), "status": "complete",
                "integrity_status": "complete", "expected_media_count": expected,
                "verified_media_count": len(validated),
                "media": validated,
            }
            markdown = (
                f"# {markdown_escape(content.title)}\n\n"
                f"- Author: {markdown_escape(content.author or 'Unknown')}\n"
                f"- Platform: {content.platform.value}\n"
                f"- Published: {metadata['published_at']}\n"
                f"- Source: {content.source_url}\n\n{markdown_escape(content.text)}\n"
            )
            (temporary / "content.md").write_text(markdown, encoding="utf-8")
            (temporary / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, target)
            return target, metadata
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    async def _download_candidate(
        self, candidate: MediaCandidate, media_dir: Path, index: int
    ) -> list[dict]:
        async with self.semaphore:
            self._assert_storage()
            existing_bytes = 0
            for path in media_dir.iterdir():
                try:
                    if path.is_file():
                        existing_bytes += path.stat().st_size
                except OSError:
                    continue
            remaining_bytes = self.settings.media_max_bytes - existing_bytes
            if remaining_bytes <= 0:
                raise ArchiveError("Media exceeds configured cumulative byte limit")
            if candidate.via_ytdlp:
                return await self._download_ytdlp(
                    candidate, media_dir, index, remaining_bytes
                )
            return [
                await self._download_http(
                    candidate, media_dir, index, remaining_bytes
                )
            ]

    async def _assert_public_media_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ArchiveError("Media URL must be a public HTTP(S) URL without embedded credentials")
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ArchiveError("Media URL resolves to a non-public host")
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                0,
                socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ArchiveError("Media URL host could not be resolved") from exc
        ips = {ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in addresses}
        if not ips or any(not address.is_global for address in ips):
            raise ArchiveError("Media URL resolves to a non-public address")

    def _extension(self, candidate: MediaCandidate, content_type: str = "") -> str:
        hint = Path(urlparse(candidate.url).path).suffix.lower()
        normalized_type = content_type.partition(";")[0].strip().lower()
        hinted_type = mimetypes.guess_type(f"file{hint}")[0] if hint else None
        if (
            hint
            and len(hint) <= 6
            and hinted_type
            and _media_family(hinted_type) == _media_family(normalized_type)
        ):
            return hint
        guessed = mimetypes.guess_extension(normalized_type)
        if guessed:
            return guessed
        return ".mp4" if candidate.kind == "video" else ".jpg" if candidate.kind == "image" else ".bin"

    async def _download_http(
        self,
        candidate: MediaCandidate,
        media_dir: Path,
        index: int,
        remaining_bytes: int,
    ) -> dict:
        headers = {"User-Agent": USER_AGENT, "Referer": candidate.url}
        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds, read=180),
            follow_redirects=False,
        ) as client:
            current_url = candidate.url
            response: httpx.Response | None = None
            for _redirect in range(6):
                await self._assert_public_media_url(current_url)
                response = await client.send(client.build_request("GET", current_url), stream=True)
                if response.is_redirect:
                    location = response.headers.get("location")
                    await response.aclose()
                    if not location:
                        raise ArchiveError("Media redirect did not include a destination")
                    current_url = urljoin(current_url, location)
                    continue
                break
            else:
                raise ArchiveError("Media URL exceeded the redirect limit")
            assert response is not None
            try:
                response.raise_for_status()
                declared_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                if not _media_family(declared_type):
                    raise ArchiveError(f"Unsupported media response type: {declared_type or 'unknown'}")
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise ArchiveError("Media response has an invalid Content-Length") from exc
                    if declared_length < 0 or declared_length > remaining_bytes:
                        raise ArchiveError("Media exceeds configured cumulative byte limit")
                extension = self._extension(candidate, declared_type)
                name = f"{index:02d}-{sanitize_component(candidate.kind)}{extension}"
                destination = media_dir / name
                partial = destination.with_suffix(destination.suffix + ".part")
                digest = hashlib.sha256()
                size = 0
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > remaining_bytes:
                            raise ArchiveError("Media exceeds configured cumulative byte limit")
                        handle.write(chunk)
                        digest.update(chunk)
                if size <= 0:
                    raise ArchiveError("Media response was empty")
                os.replace(partial, destination)
                declared_family = _media_family(declared_type)
                sniffed = _sniff_media_family(destination, declared_family)
                if sniffed is None or sniffed == "document" or sniffed != declared_family:
                    raise ArchiveError("Media response content does not match its declared type")
            finally:
                await response.aclose()
        return {
            "kind": candidate.kind,
            "source_url": candidate.url,
            "local_path": f"media/{name}",
            "mime_type": declared_type,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
        }

    async def _download_ytdlp(
        self,
        candidate: MediaCandidate,
        media_dir: Path,
        index: int,
        remaining_bytes: int,
    ) -> list[dict]:
        await self._assert_public_media_url(candidate.url)
        template = str(media_dir / f"{index:02d}-video.%(ext)s")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--no-part",
            "--restrict-filenames",
            "--merge-output-format",
            "mp4",
            "--max-filesize",
            str(remaining_bytes),
            "-f",
            "bv*+ba/b",
            "-o",
            template,
            candidate.url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        limit_error: list[str] = []

        def kill_process_group() -> None:
            try:
                if os.name == "posix" and process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, PermissionError):
                pass

        async def monitor_limits() -> None:
            minimum = int(self.settings.min_free_disk_gb * 1024**3)
            while process.returncode is None:
                total = 0
                for path in media_dir.iterdir():
                    try:
                        if path.is_file():
                            total += path.stat().st_size
                    except OSError:
                        # yt-dlp/ffmpeg may atomically replace a temporary file
                        # while it is being inspected. Recheck it on the next tick.
                        continue
                if total > self.settings.media_max_bytes:
                    limit_error.append("yt-dlp exceeded the configured media byte limit")
                    kill_process_group()
                    return
                try:
                    free_bytes = shutil.disk_usage(self.root).free
                except OSError:
                    free_bytes = 0
                if free_bytes < minimum:
                    limit_error.append("Free disk space fell below the configured safety threshold")
                    kill_process_group()
                    return
                await asyncio.sleep(0.5)

        monitor = asyncio.create_task(monitor_limits())
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.download_process_timeout_seconds
            )
        except TimeoutError as exc:
            kill_process_group()
            await process.communicate()
            raise ArchiveError("yt-dlp timed out") from exc
        except asyncio.CancelledError:
            kill_process_group()
            await asyncio.shield(process.communicate())
            raise
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if limit_error:
            raise ArchiveError(limit_error[0])
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace")[-1500:]
            raise ArchiveError(f"yt-dlp failed: {message}")
        records: list[dict] = []
        total_bytes = 0
        for path in sorted(media_dir.glob(f"{index:02d}-video.*")):
            if path.suffix == ".part" or not path.is_file():
                continue
            digest = hashlib.sha256()
            size = path.stat().st_size
            total_bytes += size
            if size <= 0 or total_bytes > remaining_bytes:
                raise ArchiveError("yt-dlp output exceeds the configured media byte limit")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            records.append(
                {
                    "kind": "video",
                    "source_url": candidate.url,
                    "local_path": f"media/{path.name}",
                    "mime_type": mimetypes.guess_type(path.name)[0] or "video/mp4",
                    "size_bytes": size,
                    "sha256": digest.hexdigest(),
                }
            )
        if not records:
            detail = stdout.decode("utf-8", errors="replace")[-500:]
            raise ArchiveError(f"yt-dlp produced no media file: {detail}")
        return records
