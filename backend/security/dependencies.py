from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

from core.settings_service import get_effective_settings
from security import database as security_db
from security.media_credentials import credential_is_valid
from security.sessions import SESSION_COOKIE, hash_token, session_is_valid


@dataclass(frozen=True)
class MediaAccessContext:
    """媒体访问的统一身份上下文。

    only Media Credential 来源时才把 ``access_token`` 透传到下游 m3u8 重写产物。
    Session/Desktop/匿名 三类身份**禁止**把任何凭证写进媒体 URL 的 query。
    """

    source: str             # "anonymous" | "session" | "credential"
    admin_user_id: int | None = None
    admin_username: str = ""
    media_credential_id: int | None = None
    propagated_access_token: str | None = None  # 仅 source == "credential" 时有值

    @property
    def is_admin(self) -> bool:
        return self.source == "session" and self.admin_user_id is not None


async def current_admin(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise HTTPException(status_code=401, detail="需要登录")
    session = await security_db.get_session_by_hash(hash_token(token))
    if not session or not session_is_valid(session) or session.get("role") != "admin":
        raise HTTPException(status_code=401, detail="登录已失效")
    return {
        "id": session["user_id"],
        "username": session["username"],
        "role": session["role"],
    }


async def require_admin(
    request: Request,
    x_waveflow_request: str = Header(default=""),
    admin: dict = Depends(current_admin),
) -> dict:
    if request.method not in {"GET", "HEAD", "OPTIONS"} and x_waveflow_request != "1":
        raise HTTPException(status_code=403, detail="缺少安全请求头")
    return admin


async def require_browse_access(request: Request) -> dict | None:
    settings = await get_effective_settings()
    if settings.anonymous_browse:
        return None
    return await current_admin(request)


async def resolve_media_access(request: Request, scope: str = "stream") -> MediaAccessContext:
    """通用媒体访问解析；返回结构化上下文，不裸吐 token。"""
    settings = await get_effective_settings()

    # 1. 优先 Media Credential（外部播放器场景）
    token = request.query_params.get("access_token", "")
    if token:
        record = await security_db.get_media_credential_by_hash(hash_token(token))
        if record and credential_is_valid(record, scope):
            return MediaAccessContext(
                source="credential",
                media_credential_id=record["id"],
                propagated_access_token=token,
            )

    # 2. Admin Session（网页内播放）
    cookie_token = request.cookies.get(SESSION_COOKIE, "")
    if cookie_token:
        session = await security_db.get_session_by_hash(hash_token(cookie_token))
        if session and session_is_valid(session) and session.get("role") == "admin":
            return MediaAccessContext(
                source="session",
                admin_user_id=session["user_id"],
                admin_username=session["username"],
            )

    # 3. 匿名（仅当 anonymous_playback 开启）
    if settings.anonymous_playback:
        return MediaAccessContext(source="anonymous")

    raise HTTPException(status_code=401, detail="需要登录或有效凭证")


async def require_media_access(request: Request, scope: str = "stream") -> dict | None:
    """兼容旧调用点的薄封装：仅决定 401/200，不返回 token。

    新的代码应该使用 ``Depends(resolve_media_access)`` 拿到 ``MediaAccessContext``。
    """
    await resolve_media_access(request, scope=scope)
    return None
