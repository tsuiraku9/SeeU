from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Admin


def verify_webui_login_token(token: str) -> bool:
    configured = get_settings().webui_login_token
    return bool(configured) and secrets.compare_digest(
        configured.encode("utf-8"),
        token.encode("utf-8"),
    )


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def require_session(request: Request, db: Session | None = None) -> int:
    admin_id = request.session.get("admin_id")
    if not admin_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    admin_id = int(admin_id)
    if db is not None:
        admin = db.get(Admin, admin_id)
        session_version = request.session.get("session_version")
        auth_fingerprint = request.session.get("auth_fingerprint")
        if (
            admin is None
            or session_version != admin.session_version
            or not isinstance(auth_fingerprint, str)
            or not secrets.compare_digest(
                auth_fingerprint,
                get_settings().webui_auth_fingerprint,
            )
        ):
            request.session.clear()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return admin_id


def require_csrf(request: Request) -> None:
    expected = request.session.get("csrf_token")
    actual = request.headers.get("X-CSRF-Token", "")
    if not expected or not secrets.compare_digest(str(expected), actual):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


class LoginRateLimiter:
    def __init__(self, limit: int = 5, window_seconds: int = 900) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.attempts: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        bucket = self.attempts[key]
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.limit:
            raise HTTPException(status_code=429, detail="Too many login attempts; try again later")
        bucket.append(now)

    def reset(self, key: str) -> None:
        self.attempts.pop(key, None)


login_rate_limiter = LoginRateLimiter()
