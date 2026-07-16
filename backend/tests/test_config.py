from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    "value",
    ["0.0.0.0", "::", "127.0.0.2", "192.168.1.10", "example.com", "localhost"],
)
@pytest.mark.parametrize("field", ["app_bind_address", "novnc_bind_address"])
def test_published_bind_addresses_must_be_loopback(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_ipv6_loopback_is_rendered_as_a_valid_url_host() -> None:
    assert Settings(novnc_bind_address="::1").novnc_url_host == "[::1]"


@pytest.mark.parametrize("value", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("field", ["app_bind_address", "novnc_bind_address"])
def test_supported_loopback_bind_addresses_are_accepted(field: str, value: str) -> None:
    assert getattr(Settings(**{field: value}), field) == value


@pytest.mark.parametrize("value", [0, 65536])
def test_webui_port_must_be_a_valid_tcp_port(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(webui_port=value)


def test_configured_webui_token_is_preserved_and_not_regenerated() -> None:
    settings = Settings(
        webui_login_token="configured-webui-token-long-enough",
        session_secret="session-secret-that-is-longer-than-32-characters",
    )

    assert settings.ensure_webui_login_token() is None
    assert settings.webui_login_token == "configured-webui-token-long-enough"
    assert len(settings.webui_auth_fingerprint) == 64


def test_printable_unicode_webui_token_is_supported() -> None:
    token = "本地登录令牌" * 5
    settings = Settings(
        webui_login_token=token,
        session_secret="session-secret-that-is-longer-than-32-characters",
    )

    assert settings.webui_login_token == token
    assert len(settings.webui_auth_fingerprint) == 64


def test_empty_webui_token_is_regenerated_for_each_application_start() -> None:
    settings = Settings(
        webui_login_token="",
        session_secret="session-secret-that-is-longer-than-32-characters",
    )

    generated = settings.ensure_webui_login_token()

    assert generated is not None
    assert generated == settings.webui_login_token
    assert len(generated) >= 40
    next_start_token = settings.ensure_webui_login_token()
    assert next_start_token is not None
    assert next_start_token != generated
    assert next_start_token == settings.webui_login_token


def test_generated_webui_token_is_atomically_published_and_cleared(tmp_path) -> None:
    settings = Settings(
        webui_login_token="",
        session_secret="session-secret-that-is-longer-than-32-characters",
        database_path=tmp_path / "state" / "app.db",
    )
    generated = settings.ensure_webui_login_token()

    assert generated is not None
    path = settings.publish_generated_webui_login_token(generated)
    assert path == tmp_path / "state" / "webui-login-token.txt"
    assert path.read_text(encoding="utf-8").strip() == generated
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600

    settings.clear_generated_webui_login_token()
    assert not path.exists()


@pytest.mark.parametrize("value", ["short", "x" * 513, "valid-token\nvalue-that-is-long-enough"])
def test_configured_webui_token_rejects_weak_or_invalid_values(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(webui_login_token=value)


def test_invalid_webui_token_is_hidden_from_validation_error_text() -> None:
    sensitive_invalid_token = "sensitive-token-value\nthat-must-not-appear"

    with pytest.raises(ValidationError) as caught:
        Settings(webui_login_token=sensitive_invalid_token)

    error_text = str(caught.value)
    assert sensitive_invalid_token not in error_text
    assert "input_value" not in error_text
