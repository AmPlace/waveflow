from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import Response

from core.settings_service import get_effective_settings_sync
from security import database as security_db


SESSION_COOKIE = "waveflow_session"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


async def create_login_session(user_id: int, response: Response) -> str:
    settings = get_effective_settings_sync()
    token = new_session_token()
    await security_db.create_session(
        user_id,
        hash_token(token),
        ttl_hours=settings.session_max_age_days * 24,
    )
    set_session_cookie(response, token, settings=settings)
    return token


def set_session_cookie(response: Response, token: str, *, settings=None) -> None:
    settings = settings or get_effective_settings_sync()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
        max_age=settings.session_max_age_days * 24 * 3600,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def session_is_valid(session: dict[str, Any]) -> bool:
    expires_at = session.get("expires_at") or ""
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    return expires > datetime.now(timezone.utc)
