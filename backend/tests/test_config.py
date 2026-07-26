from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    "value",
    ["", "example.com", "localhost", "999.1.1.1", "127.0.0.1:8080"],
)
def test_published_bind_address_must_be_an_ip_address(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(app_bind_address=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("::1", "::1"),
        ("0.0.0.0", "0.0.0.0"),
        ("::", "::"),
        ("192.168.1.10", "192.168.1.10"),
        ("2001:0DB8::1", "2001:db8::1"),
    ],
)
def test_valid_bind_addresses_are_accepted(value: str, expected: str) -> None:
    assert Settings(app_bind_address=value).app_bind_address == expected


def test_external_provider_requires_url_and_strong_token_together() -> None:
    with pytest.raises(ValidationError, match="PROVIDER_API_TOKEN"):
        Settings(provider_base_url="http://provider.example:8090")
    with pytest.raises(ValidationError, match="PROVIDER_BASE_URL"):
        Settings(provider_api_token="provider-token-that-is-long-enough")


@pytest.mark.parametrize(
    "value",
    [
        "ftp://provider.example",
        "http://user:pass@provider.example",
        "http://provider.example/api",
        "http://provider.example?token=secret",
        "http://provider.example/#fragment",
    ],
)
def test_external_provider_url_rejects_unsafe_forms(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url=value,
            provider_api_token="provider-token-that-is-long-enough",
        )


def test_external_provider_configuration_is_normalized() -> None:
    settings = Settings(
        provider_base_url="https://provider.example/",
        provider_api_token="provider-token-that-is-long-enough",
    )

    assert settings.provider_configured is True
    assert settings.provider_base_url == "https://provider.example"


def test_provider_discovery_defaults_to_ten_historical_items() -> None:
    assert Settings().provider_discovery_limit == 10


def test_provider_discovery_limit_rejects_values_below_ten() -> None:
    with pytest.raises(ValidationError):
        Settings(provider_discovery_limit=9)


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
