from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.getenv("SEEU_ENV_FILE", ".env") or None,
        extra="ignore",
        hide_input_in_errors=True,
    )

    _webui_login_token_configured: bool = PrivateAttr(default=False)

    webui_login_token: str = Field(default="", repr=False)
    webui_port: int = Field(default=8080, ge=1, le=65535)
    session_secret: str = ""
    cookie_secure: bool = False
    app_bind_address: str = "127.0.0.1"
    database_path: Path = Path("data/state/app.db")
    archive_root: Path = Path("data/archive")
    provider_staging_root: Path = Path("data/provider-staging")
    provider_base_url: str = ""
    provider_api_token: str = Field(default="", repr=False)
    provider_request_timeout_seconds: int = Field(default=900, ge=5, le=7200)
    provider_discovery_limit: int = Field(default=10, ge=10, le=500)
    provider_poll_concurrency: int = Field(default=1, ge=1, le=4)
    scheduler_batch_size: int = Field(default=4, ge=1, le=100)
    allow_fake_ip_dns: bool = False
    import_max_bytes: int = Field(default=2 * 1024**3, ge=1024)
    import_max_files: int = Field(default=100, ge=1, le=1000)
    poll_interval_minutes: int = Field(default=60, ge=5, le=1440)
    poll_jitter_minutes: int = Field(default=5, ge=0, le=30)
    min_free_disk_gb: float = Field(default=5.0, ge=0.1)
    request_timeout_seconds: int = Field(default=30, ge=5, le=120)
    download_process_timeout_seconds: int = Field(default=900, ge=30, le=14400)
    media_max_bytes: int = Field(default=2 * 1024**3, ge=1024)
    download_concurrency: int = Field(default=2, ge=1, le=8)
    archive_size_cache_seconds: int = Field(default=300, ge=5, le=3600)
    scheduler_enabled: bool = True
    app_name: str = "我会一直看着你"

    def model_post_init(self, _context: object) -> None:
        self._webui_login_token_configured = bool(self.webui_login_token)

    @field_validator("webui_login_token")
    @classmethod
    def clean_webui_login_token(cls, value: str) -> str:
        if any(not character.isprintable() for character in value):
            raise ValueError("WEBUI_LOGIN_TOKEN must contain printable characters only")
        value = value.strip()
        if value and len(value) < 24:
            raise ValueError("WEBUI_LOGIN_TOKEN must contain at least 24 characters when configured")
        if len(value) > 512:
            raise ValueError("WEBUI_LOGIN_TOKEN must be at most 512 printable characters")
        return value

    @field_validator("app_bind_address")
    @classmethod
    def valid_bind_address(cls, value: str) -> str:
        value = value.strip().lower()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError as error:
            raise ValueError("APP_BIND_ADDRESS must be a valid IPv4 or IPv6 address") from error

    @field_validator("provider_base_url")
    @classmethod
    def clean_provider_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "PROVIDER_BASE_URL must be an http(s) origin without credentials, path, query, or fragment"
            )
        return value

    @field_validator("provider_api_token")
    @classmethod
    def clean_provider_api_token(cls, value: str) -> str:
        if any(not character.isprintable() for character in value):
            raise ValueError("PROVIDER_API_TOKEN must contain printable characters only")
        value = value.strip()
        if value and len(value) < 24:
            raise ValueError("PROVIDER_API_TOKEN must contain at least 24 characters")
        if len(value) > 512:
            raise ValueError("PROVIDER_API_TOKEN must be at most 512 printable characters")
        return value

    @model_validator(mode="after")
    def require_provider_token(self) -> "Settings":
        if self.provider_base_url and not self.provider_api_token:
            raise ValueError(
                "PROVIDER_API_TOKEN is required when PROVIDER_BASE_URL is configured"
            )
        if self.provider_api_token and not self.provider_base_url:
            raise ValueError(
                "PROVIDER_BASE_URL is required when PROVIDER_API_TOKEN is configured"
            )
        return self

    @property
    def provider_configured(self) -> bool:
        return bool(self.provider_base_url and self.provider_api_token)

    def ensure_webui_login_token(self) -> str | None:
        """Generate a fresh token for this startup when configuration left it empty."""

        if self._webui_login_token_configured:
            return None
        generated = secrets.token_urlsafe(32)
        self.webui_login_token = generated
        return generated

    @property
    def generated_webui_token_path(self) -> Path:
        return self.database_path.parent / "webui-login-token.txt"

    def publish_generated_webui_login_token(self, token: str) -> Path:
        """Atomically publish an auto-generated token without exposing it in logs."""

        path = self.generated_webui_token_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                descriptor = None
                handle.write(f"{token}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        return path

    def clear_generated_webui_login_token(self) -> None:
        self.generated_webui_token_path.unlink(missing_ok=True)

    @property
    def webui_auth_fingerprint(self) -> str:
        if not self.webui_login_token or not self.session_secret:
            raise RuntimeError("WebUI authentication is not initialized")
        return hmac.new(
            self.session_secret.encode("utf-8"),
            b"webui-login-token\0" + self.webui_login_token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def validate_secrets(self) -> None:
        if len(self.session_secret) < 32:
            raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
        if not self.webui_login_token:
            raise RuntimeError("WEBUI_LOGIN_TOKEN was not initialized")

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.provider_staging_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
