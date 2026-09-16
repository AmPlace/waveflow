from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import database as app_db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    return app_db._connect()


async def has_admin_user() -> bool:
    def _get() -> bool:
        conn = _connect()
        row = conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
        conn.close()
        return row is not None
    return await asyncio.to_thread(_get)


async def create_admin_user(username: str, password_hash: str) -> dict[str, Any]:
    def _create() -> dict[str, Any]:
        conn = _connect()
        try:
            # Serialize the first-admin check with the insert at the database
            # boundary.  An asyncio lock would not protect another worker or
            # process using the same SQLite database.
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
                conn.rollback()
                raise ValueError("管理员已初始化")
            now = _now_iso()
            cur = conn.execute(
                """
                INSERT INTO users(username, password_hash, role, created_at, updated_at)
                VALUES(?, ?, 'admin', ?, ?)
                """,
                (username, password_hash, now, now),
            )
            conn.commit()
            return {"id": cur.lastrowid, "username": username, "role": "admin"}
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()
    return await asyncio.to_thread(_create)


async def ensure_desktop_user() -> dict[str, Any]:
    """Create or return the internal desktop admin identity.

    This identity is only used after the separate desktop session secret has
    already been validated. It should not be accepted by password login.
    """
    def _ensure() -> dict[str, Any]:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, username, role FROM users WHERE username='desktop'"
            ).fetchone()
            if row:
                return dict(row)
            now = _now_iso()
            cur = conn.execute(
                """
                INSERT INTO users(username, password_hash, role, created_at, updated_at)
                VALUES('desktop', 'desktop-session', 'admin', ?, ?)
                """,
                (now, now),
            )
            conn.commit()
            return {"id": cur.lastrowid, "username": "desktop", "role": "admin"}
        finally:
            conn.close()
    return await asyncio.to_thread(_ensure)


async def get_user_by_username(username: str) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM users WHERE username=? AND role='admin'",
            (username,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    return await asyncio.to_thread(_get)


async def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        conn = _connect()
        row = conn.execute("SELECT id, username, role, created_at, updated_at FROM users WHERE id=?", (user_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    return await asyncio.to_thread(_get)


async def create_session(user_id: int, token_hash: str, *, ttl_hours: int = 24 * 14) -> dict[str, Any]:
    def _create() -> dict[str, Any]:
        conn = _connect()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=ttl_hours)
        cur = conn.execute(
            """
            INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (token_hash, user_id, now.isoformat(), expires.isoformat(), now.isoformat()),
        )
        conn.commit()
        session_id = cur.lastrowid
        conn.close()
        return {"id": session_id, "user_id": user_id, "expires_at": expires.isoformat()}
    return await asyncio.to_thread(_create)


async def get_session_by_hash(token_hash: str) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        conn = _connect()
        row = conn.execute(
            """
            SELECT s.*, u.username, u.role
            FROM sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND (s.revoked_at IS NULL OR s.revoked_at='')
            """,
            (token_hash,),
        ).fetchone()
        if row:
            conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (_now_iso(), row["id"]))
            conn.commit()
        conn.close()
        return dict(row) if row else None
    return await asyncio.to_thread(_get)


async def revoke_session(token_hash: str) -> None:
    def _revoke() -> None:
        conn = _connect()
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE token_hash=? AND (revoked_at IS NULL OR revoked_at='')",
            (_now_iso(), token_hash),
        )
        conn.commit()
        conn.close()
    await asyncio.to_thread(_revoke)


async def create_media_credential(
    *,
    token_hash: str,
    name: str,
    scopes: list[str],
    expires_at: str | None,
) -> dict[str, Any]:
    def _create() -> dict[str, Any]:
        conn = _connect()
        cur = conn.execute(
            """
            INSERT INTO media_credentials(token_hash, name, scopes_json, created_at, expires_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (token_hash, name, json.dumps(scopes), _now_iso(), expires_at or ""),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return {"id": row_id, "name": name, "scopes": scopes, "expires_at": expires_at or ""}
    return await asyncio.to_thread(_create)


async def list_media_credentials() -> list[dict[str, Any]]:
    def _list() -> list[dict[str, Any]]:
        conn = _connect()
        rows = conn.execute(
            """
            SELECT id, name, scopes_json, created_at, expires_at, last_used_at, revoked_at
            FROM media_credentials
            ORDER BY id DESC
            """
        ).fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item["scopes"] = json.loads(item.pop("scopes_json") or "[]")
            result.append(item)
        return result
    return await asyncio.to_thread(_list)


async def get_media_credential_by_hash(token_hash: str) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        conn = _connect()
        row = conn.execute(
            """
            SELECT * FROM media_credentials
            WHERE token_hash=? AND (revoked_at IS NULL OR revoked_at='')
            """,
            (token_hash,),
        ).fetchone()
        if row:
            conn.execute("UPDATE media_credentials SET last_used_at=? WHERE id=?", (_now_iso(), row["id"]))
            conn.commit()
        conn.close()
        if not row:
            return None
        item = dict(row)
        item["scopes"] = json.loads(item.pop("scopes_json") or "[]")
        return item
    return await asyncio.to_thread(_get)


async def revoke_media_credential(credential_id: int) -> bool:
    def _revoke() -> bool:
        conn = _connect()
        cur = conn.execute(
            "UPDATE media_credentials SET revoked_at=? WHERE id=? AND (revoked_at IS NULL OR revoked_at='')",
            (_now_iso(), credential_id),
        )
        conn.commit()
        changed = cur.rowcount > 0
        conn.close()
        return changed
    return await asyncio.to_thread(_revoke)


def new_token(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"
