from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, HttpUrl

from .models import AccountStatus, CompletenessStatus, JobStatus, Platform


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive values; API timestamps are always UTC instants."""

    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


UtcDateTime = Annotated[datetime, BeforeValidator(_as_utc)]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=512)


class AuthResponse(BaseModel):
    username: str
    csrf_token: str


class AccountCreate(BaseModel):
    platform: Platform
    display_name: str | None = Field(default=None, max_length=160)
    source_url: HttpUrl
    interval_minutes: int = Field(default=60, ge=5, le=1440)


class AccountUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=5, le=1440)


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: Platform
    display_name: str
    slug: str
    source_url: str
    enabled: bool
    interval_minutes: int
    baseline_established: bool
    completeness_status: CompletenessStatus
    gap_detected_at: UtcDateTime | None
    status: AccountStatus
    consecutive_failures: int
    last_error: str | None
    last_polled_at: UtcDateTime | None
    next_poll_at: UtcDateTime | None
    created_at: UtcDateTime


class AccountTestOut(BaseModel):
    ok: bool
    found: int = Field(ge=0)
    latest_ids: list[str]


class ContentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    platform: Platform
    remote_id: str
    title: str
    author: str
    content_type: str
    source_url: str
    published_at: UtcDateTime
    collected_at: UtcDateTime
    summary: str
    media_count: int
    expected_media_count: int
    verified_media_count: int
    integrity_status: CompletenessStatus
    status: JobStatus
    error: str | None


class ContentDetail(ContentOut):
    markdown: str
    metadata: dict


class CrawlRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    started_at: UtcDateTime
    finished_at: UtcDateTime | None
    status: JobStatus
    discovered_count: int
    archived_count: int
    error: str | None
    details: dict


class ContentPage(BaseModel):
    items: list[ContentOut]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    has_more: bool


class CrawlRunPage(BaseModel):
    items: list[CrawlRunOut]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    has_more: bool


class StorageOut(BaseModel):
    total_bytes: int
    used_bytes: int
    free_bytes: int
    archive_bytes: int
    minimum_free_bytes: int
    downloads_paused: bool


class MessageOut(BaseModel):
    message: str
