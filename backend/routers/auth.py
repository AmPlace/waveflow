from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from core.settings_service import get_effective_settings
from security import database as security_db
from security.dependencies import current_admin
from security.passwords import verify_password
from security.sessions import SESSION_COOKIE, clear_session_cookie, create_login_session, hash_token


router = APIRouter(prefix="/api/auth", tags=["auth"])
_DESKTOP_BOOTSTRAP_LOCK = asyncio.Lock()
_desktop_bootstrap_consumed = False


class LoginRequest(BaseModel):
    username: str
    password: str


class DesktopAuthRequest(BaseModel):
    desktop_session: str


@router.post("/login")
async def login(payload: LoginRequest, response: Response) -> dict:
    user = await security_db.get_user_by_username((payload.username or "").strip())
    if not user or not verify_password(payload.password, user.get("password_hash") or ""):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    await create_login_session(user["id"], response)
    return {"ok": True, "user": {"id": user["id"], "username": user["username"], "role": user["role"]}}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        await security_db.revoke_session(hash_token(token))
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
async def me(admin: dict = Depends(current_admin)) -> dict:
    return {"authenticated": True, "user": admin}


@router.post("/desktop")
async def desktop_auth(payload: DesktopAuthRequest, request: Request, response: Response) -> dict:
    global _desktop_bootstrap_consumed

    async with _DESKTOP_BOOTSTRAP_LOCK:
        settings = await get_effective_settings()
        if settings.mode != "desktop":
            raise HTTPException(status_code=404, detail="desktop auth unavailable")
        client_host = request.client.host if request.client else ""
        if client_host not in {"127.0.0.1", "::1", "localhost"}:
            raise HTTPException(status_code=403, detail="desktop auth only accepts loopback")
        if _desktop_bootstrap_consumed:
            raise HTTPException(status_code=401, detail="desktop auth already used")
        if not settings.desktop_session_secret or payload.desktop_session != settings.desktop_session_secret:
            raise HTTPException(status_code=401, detail="desktop auth failed")

        user = await security_db.ensure_desktop_user()
        await create_login_session(user["id"], response)
        _desktop_bootstrap_consumed = True
        return {"ok": True, "desktop": True, "user": {"id": user["id"], "username": user["username"], "role": user["role"]}}
