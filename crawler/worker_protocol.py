from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any


WORKER_RESULT_FILENAME = "bridge-result.json"
WORKER_RESULT_SCHEMA_VERSION = 1
WORKER_REQUEST_FILENAME = "bridge-request.json"
WORKER_REQUEST_SCHEMA_VERSION = 1
MANUAL_VERIFICATION_CODE = "manual_verification_required"
LOGIN_REQUIRED_CODE = "login_required"
PROVIDER_EXECUTION_CODE = "provider_execution_failed"

_MANUAL_MARKERS = (
    "slider",
    "captcha",
    "verify",
    "verification",
    "短信",
    "验证码",
    "安全验证",
    "滑块",
)
_LOGIN_MARKERS = (
    "login",
    "logged in",
    "cookie",
    "session expired",
    "登录",
    "会话过期",
)


def safe_diagnostic(value: object, fallback: str) -> str:
    text = str(value).strip() or fallback
    text = re.sub(
        r"(https?://[^\s?]+)\?[^\s]+",
        r"\1?[query-redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\b(cookie|authorization|x-s|x-t)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    return text[:1000]


def safe_exception_diagnostic(exc: Exception, fallback: str) -> str:
    """Prefer the underlying retry/cause exception while preserving redaction."""
    current: BaseException = exc
    seen: set[int] = set()
    for _depth in range(8):
        if id(current) in seen:
            break
        seen.add(id(current))
        candidate: BaseException | None = None
        last_attempt = getattr(current, "last_attempt", None)
        exception_reader = getattr(last_attempt, "exception", None)
        if callable(exception_reader):
            try:
                value = exception_reader()
            except Exception:
                value = None
            if isinstance(value, BaseException):
                candidate = value
        if candidate is None:
            candidate = current.__cause__ or current.__context__
        if candidate is None:
            break
        current = candidate
    return safe_diagnostic(current, fallback)


def classify_worker_exception(exc: Exception, phase: str) -> dict[str, Any]:
    diagnostic = safe_exception_diagnostic(exc, "Provider worker failed")
    lowered = diagnostic.lower()
    if any(marker in lowered for marker in _MANUAL_MARKERS):
        return {
            "schema_version": WORKER_RESULT_SCHEMA_VERSION,
            "code": MANUAL_VERIFICATION_CODE,
            "message": (
                "Platform requires manual SMS, slider, CAPTCHA, or security "
                "verification in the local noVNC browser"
            ),
            "phase": phase,
            "retryable": True,
        }
    if any(marker in lowered for marker in _LOGIN_MARKERS):
        return {
            "schema_version": WORKER_RESULT_SCHEMA_VERSION,
            "code": LOGIN_REQUIRED_CODE,
            "message": "Platform session is missing or expired",
            "phase": phase,
            "retryable": True,
        }
    return {
        "schema_version": WORKER_RESULT_SCHEMA_VERSION,
        "code": PROVIDER_EXECUTION_CODE,
        "message": diagnostic,
        "phase": phase,
        "retryable": True,
    }


def write_worker_result(output: Path, result: dict[str, Any]) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    destination = output / WORKER_RESULT_FILENAME
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def write_worker_request(
    output: Path,
    *,
    platform: str,
    mode: str,
    value: str,
    limit: int,
) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    destination = output / WORKER_REQUEST_FILENAME
    payload = {
        "schema_version": WORKER_REQUEST_SCHEMA_VERSION,
        "platform": platform,
        "mode": mode,
        "value": value,
        "limit": limit,
    }
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_worker_request(
    output: Path,
    *,
    expected_platform: str,
    expected_mode: str,
) -> dict[str, Any]:
    source = output / WORKER_REQUEST_FILENAME
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Provider worker request is unreadable") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != WORKER_REQUEST_SCHEMA_VERSION
        or raw.get("platform") != expected_platform
        or raw.get("mode") != expected_mode
        or not isinstance(raw.get("value"), str)
        or not isinstance(raw.get("limit"), int)
        or isinstance(raw.get("limit"), bool)
        or not 1 <= raw["limit"] <= 500
    ):
        raise ValueError("Provider worker request violates its contract")
    return {
        "value": raw["value"],
        "limit": raw["limit"],
    }


def read_worker_result(output: Path, *, expected_phase: str) -> dict[str, Any] | None:
    source = output / WORKER_RESULT_FILENAME
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Provider worker result is unreadable") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != WORKER_RESULT_SCHEMA_VERSION:
        raise ValueError("Provider worker result has an unknown schema")
    code = raw.get("code")
    message = raw.get("message")
    phase = raw.get("phase")
    retryable = raw.get("retryable")
    if (
        not isinstance(code, str)
        or not code
        or not isinstance(message, str)
        or not message
        or phase != expected_phase
        or not isinstance(retryable, bool)
    ):
        raise ValueError("Provider worker result violates its contract")
    return {
        "code": code,
        "message": safe_diagnostic(message, "Provider worker failed"),
        "phase": phase,
        "retryable": retryable,
    }
