from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Platform(str, enum.Enum):
    bilibili = "bilibili"
    weibo = "weibo"
    douyin = "douyin"
    xiaohongshu = "xiaohongshu"


class AccountStatus(str, enum.Enum):
    pending = "pending"
    healthy = "healthy"
    polling = "polling"
    error = "error"
    blocked = "blocked"
    paused = "paused"


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    complete = "complete"
    failed = "failed"
    baseline = "baseline"


class CompletenessStatus(str, enum.Enum):
    unknown = "unknown"
    complete = "complete"
    pending_retry = "pending_retry"
    gap_detected = "gap_detected"


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[Platform] = mapped_column(Enum(Platform))
    display_name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str] = mapped_column(String(2048), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    baseline_established: Mapped[bool] = mapped_column(Boolean, default=False)
    completeness_status: Mapped[CompletenessStatus] = mapped_column(
        Enum(CompletenessStatus), default=CompletenessStatus.unknown
    )
    gap_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seen_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    ledger_revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[AccountStatus] = mapped_column(Enum(AccountStatus), default=AccountStatus.pending)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    contents: Mapped[list[ContentIndex]] = relationship(back_populates="account", cascade="all, delete-orphan")
    runs: Mapped[list[CrawlRun]] = relationship(back_populates="account", cascade="all, delete-orphan")
    observations: Mapped[list[ObservedContent]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class ObservedContent(Base):
    """Durable high-water ledger; unlike Account.seen_ids this is never truncated."""

    __tablename__ = "observed_content"
    __table_args__ = (UniqueConstraint("account_id", "remote_id", name="uq_observed_account_remote"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    remote_id: Mapped[str] = mapped_column(String(256))
    source_url: Mapped[str] = mapped_column(String(2048), default="")
    retry_pending: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped[Account] = relationship(back_populates="observations")


class ContentIndex(Base):
    __tablename__ = "content_index"
    __table_args__ = (UniqueConstraint("platform", "remote_id", name="uq_content_platform_remote"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    platform: Mapped[Platform] = mapped_column(Enum(Platform), index=True)
    remote_id: Mapped[str] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(500), default="")
    author: Mapped[str] = mapped_column(String(160), default="")
    content_type: Mapped[str] = mapped_column(String(32), index=True)
    source_url: Mapped[str] = mapped_column(String(2048))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    archive_path: Mapped[str] = mapped_column(String(2048))
    summary: Mapped[str] = mapped_column(Text, default="")
    media_count: Mapped[int] = mapped_column(Integer, default=0)
    expected_media_count: Mapped[int] = mapped_column(Integer, default=0)
    verified_media_count: Mapped[int] = mapped_column(Integer, default=0)
    integrity_status: Mapped[CompletenessStatus] = mapped_column(
        Enum(CompletenessStatus), default=CompletenessStatus.complete
    )
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.complete)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped[Account] = relationship(back_populates="contents")
    downloads: Mapped[list[DownloadJob]] = relationship(back_populates="content", cascade="all, delete-orphan")


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.running)
    discovered_count: Mapped[int] = mapped_column(Integer, default=0)
    archived_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    account: Mapped[Account] = relationship(back_populates="runs")


class DownloadJob(Base):
    __tablename__ = "download_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("content_index.id", ondelete="CASCADE"), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    relative_path: Mapped[str] = mapped_column(String(1024))
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.pending)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    bytes_downloaded: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    content: Mapped[ContentIndex] = relationship(back_populates="downloads")
