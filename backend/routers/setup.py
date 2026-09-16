from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel

from core.settings_service import get_effective_settings
from security import database as security_db
from security.passwords import hash_password
from security.sessions import create_login_session


router = APIRouter(prefix="/api/setup", tags=["setup"])


class InitializeRequest(BaseModel):
    username: str
    password: str


async def require_setup_request(
    x_waveflow_request: str = Header(default=""),
) -> None:
    if x_waveflow_request != "1":
        raise HTTPException(status_code=403, detail="缺少安全请求头")


@router.get("/status")
async def setup_status() -> dict:
    settings = await get_effective_settings()
    initialized = await security_db.has_admin_user()
    return {
        "initialized": initialized,
        "mode": settings.mode,
        "anonymous_browse": settings.anonymous_browse,
        "anonymous_playback": settings.anonymous_playback,
    }


@router.post("/initialize", dependencies=[Depends(require_setup_request)])
async def initialize_admin(payload: InitializeRequest, response: Response) -> dict:
    username = (payload.username or "").strip()
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="用户名至少需要 3 个字符")
    try:
        password_hash = hash_password(payload.password)
        user = await security_db.create_admin_user(username, password_hash)
    except ValueError as exc:
        if "已初始化" in str(exc):
            raise HTTPException(status_code=409, detail="管理员已初始化") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await create_login_session(user["id"], response)
    return {"ok": True, "user": user}
