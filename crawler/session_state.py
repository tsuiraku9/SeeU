from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path


SESSION_STATUSES = frozenset(
    {
        "logged_out",
        "starting",
        "qr_ready",
        "manual_verification_required",
        "authenticated",
        "expired",
        "error",
    }
)


def session_state_path(root: Path, platform: str) -> Path:
    return root / f"{platform}.json"


def session_qr_path(root: Path, platform: str) -> Path:
    return root / f"{platform}-qr.png"


def default_session_state(platform: str, manual_verification_url: str) -> dict:
    return {
        "platform": platform,
        "status": "logged_out",
        "updated_at": None,
        "message": None,
        "manual_verification_url": manual_verification_url,
    }


def read_session_state(
    path: Path,
    *,
    platform: str,
    manual_verification_url: str,
) -> dict:
    default = default_session_state(platform, manual_verification_url)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    if (
        not isinstance(raw, dict)
        or raw.get("platform") != platform
        or raw.get("status") not in SESSION_STATUSES
        or raw.get("message") is not None
        and not isinstance(raw.get("message"), str)
        or raw.get("updated_at") is not None
        and not isinstance(raw.get("updated_at"), str)
    ):
        return default
    return {
        **default,
        "status": raw["status"],
        "updated_at": raw.get("updated_at"),
        "message": raw.get("message"),
    }


def write_session_state(
    path: Path,
    *,
    platform: str,
    status: str,
    manual_verification_url: str,
    message: str | None = None,
) -> dict:
    if status not in SESSION_STATUSES:
        raise ValueError(f"Unknown platform session status: {status}")
    if message is not None and not isinstance(message, str):
        raise TypeError("Platform session message must be a string or null")
    value = {
        "platform": platform,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "manual_verification_url": manual_verification_url,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return value
