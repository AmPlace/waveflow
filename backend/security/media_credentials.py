from __future__ import annotations

from datetime import datetime, timedelta, timezone

from security import database as security_db
from security.sessions import hash_token
from core.settings_service import get_effective_settings


TOKEN_PREFIX = "wbm_"
ALLOWED_SCOPES = {"stream", "playlist", "epg"}


async def create_credential(name: str, expires_in_days: int | None, scopes: list[str]) -> dict:
    clean_name = (name or "").strip()[:80] or "外部播放器"
    clean_scopes = sorted({scope for scope in scopes if scope in ALLOWED_SCOPES}) or ["stream", "playlist", "epg"]
    expires_at = None
    ttl_days = expires_in_days
    if ttl_days is None:
        ttl_days = (await get_effective_settings()).media_credential_default_ttl_days
    if ttl_days:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=max(1, int(ttl_days)))).isoformat()
    token = security_db.new_token(TOKEN_PREFIX)
    record = await security_db.create_media_credential(
        token_hash=hash_token(token),
        name=clean_name,
        scopes=clean_scopes,
        expires_at=expires_at,
    )
    return {**record, "token": token}


def credential_is_valid(record: dict, required_scope: str | None = None) -> bool:
    expires_at = record.get("expires_at") or ""
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
                return False
        except ValueError:
            return False
    if required_scope and required_scope not in set(record.get("scopes") or []):
        return False
    return True
