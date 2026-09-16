"""媒体代理新路由组。

公共入口
========

* ``GET /api/media/channel/{channel_id}/playlist.m3u8`` —— 频道入口；按稳定 ID 解析
  source、签发 signed handle、返回主播放列表。同时承载电台 station_id（共用）。
* ``GET /api/media/channel/{channel_id}/cover`` —— adapter 封面元数据
  （仅 IPTV 频道；电台无意义）。
* ``GET /api/media/proxy/playlist/{handle}`` —— 子 playlist / variant playlist。
* ``GET /api/media/proxy/chunk/{handle}`` —— TS / fMP4 / init / KEY URI。
* ``GET /api/media/proxy/stream/{handle}`` —— MPEG-TS / FLV 直连流。
* ``GET /api/media/proxy/rtsp/{handle}`` —— RTSP→HLS playlist（基于 handle.url 与 compat）。
* ``GET /api/media/proxy/image/{handle}`` —— adapter 封面图（Referer 防盗链白名单兜底）。

管理员入口
==========

* ``POST /api/admin/probes/url`` —— 仅供管理员手动测试任意 URL 的可达性，
  不流式代理回客户端。

参数与凭证
==========

非匿名访问时 ``access_token=wbm_...`` 仅在「来源是 Media Credential」时被透传到
重写产物的子 URL；管理员 Session 走 HttpOnly Cookie，不会出现在 URL 中。

每个 handle 路由都会再次跑 SSRF 校验（策略可能在签发后变化）。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

import database as db
from core.m3u8_rewriter import CHUNK_URL_SUFFIXES, RewriteContext, rewrite_m3u8
from security.dependencies import (
    MediaAccessContext,
    require_admin,
    resolve_media_access,
)
from plugin_runtime import PluginError
from infrastructure.http_client import (
    RedirectTargetRejected,
    request_with_safe_redirects,
    stream_with_safe_redirects,
)
from security.proxy_context import ProxyContext, get_registry as get_proxy_context_registry
from security.proxy_handles import (
    DEFAULT_TTL_BY_KIND,
    HandleError,
    HandleExpired,
    HandleSignatureError,
    decode_for_kind,
    issue_cached_handle,
)
from security.redact import redact_url
from rtsp_playback import RtspPlaybackOptions, resolve_rtsp_playback_options
from security.source_ids import source_id_for, source_revision_for
from ssrf_guard import UnsafeTargetError, assert_safe_target_url
from core.visual_metadata import empty_visual, get_visual_metadata_cache


logger = logging.getLogger("media.proxy")

router = APIRouter(tags=["media"])
_PACKAGE_ASSET_INTEGRITY_CACHE: dict[tuple[str, str, str, str], tuple[int, int, str]] = {}


@router.get("/api/media/package-assets/{package_id}/{asset_id}")
async def package_asset(
    package_id: str,
    asset_id: str,
    _access: MediaAccessContext = Depends(resolve_media_access),
):
    """Serve only the currently installed, verified package asset."""
    asset = await db.get_active_package_asset(package_id, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Package asset 不存在")
    path = Path(str(asset.get("stored_path") or ""))
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="Package asset 不可用")
    try:
        stat = path.stat()
        cache_key = (
            package_id,
            asset_id,
            str(asset.get("package_version") or ""),
            str(path),
        )
        cached = _PACKAGE_ASSET_INTEGRITY_CACHE.get(cache_key)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            digest = cached[2]
        else:
            digest = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
            _PACKAGE_ASSET_INTEGRITY_CACHE[cache_key] = (stat.st_mtime_ns, stat.st_size, digest)
        if stat.st_size != int(asset.get("size_bytes") or 0) or digest != str(asset.get("sha256") or ""):
            raise HTTPException(status_code=404, detail="Package asset integrity failure")
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Package asset 不可用") from exc
    return FileResponse(path, media_type=str(asset.get("media_type") or "application/octet-stream"))


# ── helpers ────────────────────────────────────────────────────────────────


def _build_rewrite_context(
    *,
    base_url: str,
    src_id: str,
    src_label: str,
    ctx_id: str = "",
    access_ctx: MediaAccessContext,
    rtsp_compat: int = 0,
    proxy_segments: bool = True,
) -> RewriteContext:
    return RewriteContext(
        base_url=base_url,
        src_id=src_id,
        src_label=src_label,
        ctx_id=ctx_id,
        propagated_access_token=access_ctx.propagated_access_token or "",
        proxy_segments=proxy_segments,
        rtsp_compat=rtsp_compat,
    )


def _ctx_to_request_headers(ctx: ProxyContext | None, *, fallback_referer: str = "") -> dict[str, str]:
    """ProxyContext → 上游 HTTP Headers。无 ctx 走默认。"""
    headers: dict[str, str] = {}
    if not ctx:
        return headers
    if ctx.no_ua:
        if ctx.custom_ua:
            headers["User-Agent"] = ctx.custom_ua
    else:
        if ctx.custom_ua:
            headers["User-Agent"] = ctx.custom_ua
    if ctx.referer:
        headers["Referer"] = ctx.referer
    elif fallback_referer:
        headers["Referer"] = fallback_referer
    if ctx.cookie:
        headers["Cookie"] = ctx.cookie
    return headers


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _playback_source_supported(_m, source: dict) -> bool:
    if not source.get("url"):
        return False
    if not _is_truthy(source.get("enabled", True)):
        return False
    return _m._source_type(source) not in {"unsupported"}


def _media_access_suffix(access_ctx: MediaAccessContext, *, separator: str = "?") -> str:
    token = access_ctx.propagated_access_token or ""
    if not token:
        return ""
    return f"{separator}access_token={quote(token, safe='')}"


# Hop-by-hop headers from RFC 7230 §6.1，外加 Content-Encoding（我们已用
# Accept-Encoding: identity 强制上游不压缩，下游也不应该自己声明 gzip/br）。
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    # 与 streaming 行为相关，单独剔除
    "content-encoding",
})

_CHUNK_PASSTHROUGH_HEADERS = frozenset({
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
})


def _safe_upstream_response_headers(headers: httpx.Headers, *, live_chunk: bool = False) -> dict[str, str]:
    """从上游响应中挑出可以安全透传给客户端的 header。

    * 跳过 hop-by-hop 与 Content-Encoding（我们用 ``Accept-Encoding: identity`` 拉
      上游，所以下游永远是身份编码，原始 ``Content-Length`` 直接透传是安全的）。
    * 仅透传白名单中的关键媒体 header（长度/range/缓存校验）。
    """
    has_encoding = bool(headers.get("content-encoding"))
    result: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in _HOP_BY_HOP_HEADERS:
            continue
        if lower not in _CHUNK_PASSTHROUGH_HEADERS or not value:
            continue
        # 万一上游忽略了我们的 identity 请求又压缩了，就不要把错的 Content-Length
        # 透传下去，否则浏览器/hls.js 会按声明长度截断或判定 corrupt。
        if lower == "content-length" and has_encoding:
            continue
        result[key] = value
    if live_chunk:
        result["Cache-Control"] = "no-store"
    return result


def _fallback_chunk_content_type(url: str, content_type: str) -> str:
    if content_type and content_type != "application/octet-stream":
        return content_type
    path = urlparse(url).path
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "ts": "video/MP2T",
        "m4s": "video/mp4",
        "mp4": "video/mp4",
        "fmp4": "video/mp4",
        "m4v": "video/mp4",
        "aac": "audio/aac",
        "mp3": "audio/mpeg",
    }.get(ext, content_type or "application/octet-stream")


async def _stream_httpx_response(upstream: httpx.Response):
    """流式透传上游响应。

    使用 ``aiter_raw`` 而不是 ``aiter_bytes``，避免 httpx 自动解码
    ``Content-Encoding``：我们已经用 ``Accept-Encoding: identity`` 显式禁止压缩，
    上游若仍返回压缩内容也应原样转发，配合 ``_safe_upstream_response_headers`` 不
    透传 ``Content-Length``，浏览器自己会按 ``Transfer-Encoding: chunked`` 处理。
    """
    try:
        async for chunk in upstream.aiter_raw(64 * 1024):
            if chunk:
                yield chunk
    finally:
        await upstream.aclose()


async def _validate_handle_url_or_403(handle_url: str, *, allowed_schemes: set[str]) -> None:
    try:
        await assert_safe_target_url(handle_url, allowed_schemes=allowed_schemes)
    except UnsafeTargetError as exc:
        logger.info("handle SSRF reject: %s", exc)
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _safe_decode(handle: str, *, expected_kind: str) -> "decoded":
    if expected_kind == "chunk" and handle.count('.') == 2:
        token, suffix = handle.rsplit('.', 1)
        if '.' + suffix in CHUNK_URL_SUFFIXES:
            handle = token
    try:
        return decode_for_kind(handle, expected_kind)
    except HandleSignatureError as exc:
        logger.info("handle bad signature: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HandleExpired as exc:
        logger.info("handle expired: %s", exc)
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except HandleError as exc:
        logger.info("handle invalid (kind=%s): %s", expected_kind, exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


# ── 频道入口（稳定 ID）──────────────────────────────────────────────────


async def _find_iptv_channel_source(
    canonical_key: str,
    source_id: str,
) -> tuple[dict, dict]:
    import main as _m

    channels, _groups = await _m._get_aggregated_iptv_channels()
    channel = next((ch for ch in channels if ch.get("canonical_key") == canonical_key), None)
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")

    all_sources = list(channel.get("urls", []) or [])
    for source in all_sources:
        if not source.get("source_id"):
            source["source_id"] = source_id_for(source)

    requested_source_id = str(source_id or "").strip()
    if not requested_source_id:
        raise HTTPException(status_code=400, detail="source_id 不能为空")

    source = next((s for s in all_sources if str(s.get("source_id") or "") == requested_source_id), None)
    if not source:
        raise HTTPException(status_code=404, detail="播放源不存在")
    if not _is_truthy(source.get("enabled", True)) or source.get("disabled") is True:
        raise HTTPException(status_code=403, detail="播放源已禁用")
    if not _playback_source_supported(_m, source):
        raise HTTPException(status_code=404, detail="播放源类型不支持")
    return channel, source


def _source_revision_stale() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "SOURCE_REVISION_STALE",
            "message": "播放源已更新，请重新选择",
        },
    )


def _validate_expected_source_revision(source: dict, expected_source_revision: str = "") -> str:
    current_revision = source_revision_for(source)
    expected = str(expected_source_revision or "").strip()
    if expected and expected != current_revision:
        raise _source_revision_stale()
    return current_revision


async def _assert_current_iptv_source_revision(
    canonical_key: str,
    source_id: str,
    expected_source_revision: str,
) -> None:
    """Re-read the source after an async boundary before publishing a result."""
    import main as _m

    if not source_id or not callable(getattr(_m, "_get_aggregated_iptv_channels", None)):
        return
    try:
        _channel, current_source = await _find_iptv_channel_source(canonical_key, source_id)
    except HTTPException as exc:
        if exc.status_code in {403, 404}:
            raise _source_revision_stale() from exc
        raise
    if source_revision_for(current_source) != expected_source_revision:
        raise _source_revision_stale()


async def _validate_iptv_proxy_context(ctx: ProxyContext | None) -> None:
    """Reject new use of a revision-bound IPTV context after source mutation."""
    if not ctx or not ctx.source_id or not ctx.source_revision or ctx.source_id.startswith("radio:"):
        return
    import main as _m

    getter = getattr(_m, "_get_aggregated_iptv_channels", None)
    if not callable(getter):
        return
    channels, _groups = await getter()
    for channel in channels:
        for source in list(channel.get("urls", []) or []):
            current_source_id = str(source.get("source_id") or "").strip()
            if not current_source_id:
                current_source_id = source_id_for(source)
            if current_source_id != ctx.source_id:
                continue
            if not _is_truthy(source.get("enabled", True)) or source.get("disabled") is True:
                raise _source_revision_stale()
            if source_revision_for(source) != ctx.source_revision:
                raise _source_revision_stale()
            return
    raise _source_revision_stale()


def _validate_resolved_source_identity(
    resolved: dict, *, source_id: str, source_revision: str,
) -> None:
    returned_source_id = str(resolved.get("source_id") or "").strip()
    returned_revision = str(resolved.get("source_revision") or "").strip()
    if returned_source_id and returned_source_id != source_id:
        raise _source_revision_stale()
    if returned_revision and returned_revision != source_revision:
        raise _source_revision_stale()


async def _resolve_provider_source(
    resolver,
    target_url: str,
    client,
    *,
    source_id: str,
    source_revision: str,
) -> dict:
    """Pass identity metadata to the current resolver without breaking test/legacy doubles."""
    resolve = resolver.resolve
    kwargs = {}
    try:
        parameters = inspect.signature(resolve).parameters
        accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
        if accepts_kwargs or "source_id" in parameters:
            kwargs["source_id"] = source_id
        if accepts_kwargs or "source_revision" in parameters:
            kwargs["source_revision"] = source_revision
    except (TypeError, ValueError):
        pass
    return await resolve(target_url, client, **kwargs)


@router.get("/api/media/channel/{channel_key}/resolve")
async def media_channel_source_resolve(
    channel_key: str,
    request: Request,
    source_id: str = Query(..., description="Adapter source selector; must match a source in this channel"),
    expected_source_revision: str = "",
    access: MediaAccessContext = Depends(resolve_media_access),
):
    """安全版 adapter resolve。

    只接受当前频道内的 ``source_id``，不接受任意 target_url。用于前端恢复旧的
    adapter direct 候选语义：adapter 返回 direct_playable 且未 requires_proxy 时，
    前端可直连 resolved URL；proxy fallback 仍使用 source_id 频道入口。
    """
    import main as _m

    _channel, source = await _find_iptv_channel_source(channel_key, source_id)
    source_ref = str(source.get("source_id") or source_id_for(source))
    source_revision = _validate_expected_source_revision(source, expected_source_revision)
    source_type = _m._source_type(source)
    if source_type not in {"adapter", "youtube", "unsupported_youtube_url"}:
        raise HTTPException(status_code=404, detail="播放源不是 adapter")

    raw_url = str(source.get("url") or "").strip()
    adapter_url = raw_url
    if source_type in {"youtube", "unsupported_youtube_url"}:
        adapter_url = raw_url if raw_url.lower().startswith("youtube://") else f"youtube://resolve?url={quote(raw_url, safe='')}"

    try:
        provider_resolver = _provider_resolver(_m, request)
        resolved = await _resolve_provider_source(
            provider_resolver,
            adapter_url,
            _m.http_client,
            source_id=source_ref,
            source_revision=source_revision,
        )
        _validate_resolved_source_identity(
            resolved,
            source_id=source_ref,
            source_revision=source_revision,
        )
        await _assert_current_iptv_source_revision(channel_key, source_ref, source_revision)
    except _m.AdapterResolveError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_payload()) from exc
    except PluginError as exc:
        raise HTTPException(status_code=502, detail=exc.as_contract()) from exc

    resolved_url = str(resolved.get("url") or "").strip()
    if resolved_url:
        try:
            await assert_safe_target_url(resolved_url, allowed_schemes={"http", "https", "rtsp"})
        except UnsafeTargetError as exc:
            logger.info("adapter resolve SSRF reject: %s", exc)
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    proxy_path = f"/api/media/channel/{quote(channel_key, safe='')}/playlist.m3u8?source_id={quote(source_ref, safe='')}"
    proxy_path += f"&expected_source_revision={quote(source_revision, safe='')}"
    if access.propagated_access_token:
        proxy_path += f"&access_token={quote(access.propagated_access_token, safe='')}"

    return {
        "ok": True,
        "source_id": source_ref,
        "source_revision": source_revision,
        "adapter": resolved.get("adapter") or source.get("adapter") or "",
        "url": resolved_url,
        "source_type": str(resolved.get("source_type") or "hls").strip().lower(),
        "direct_playable": resolved.get("direct_playable", True),
        "requires_proxy": bool(resolved.get("requires_proxy", False)),
        "volatile_url": bool(resolved.get("volatile_url", False)),
        "proxy_url": proxy_path,
        "ttl": resolved.get("ttl"),
        "expires_at": resolved.get("expires_at"),
        "warnings": resolved.get("warnings") or [],
    }


@router.get("/api/media/channel/{channel_key}/playlist.m3u8")
async def media_channel_playlist(
    channel_key: str,
    request: Request,
    source_id: str = Query("", description="IPTV source selector; must match a source in this channel"),
    expected_source_revision: str = "",
    access: MediaAccessContext = Depends(resolve_media_access),
):
    """按稳定 ID 解析频道 → 选 best source → 签 handle → 返回主播放列表。

    ``channel_key`` 可以是：

    * 电台 station_id（如 ``cnr_1``）
    * IPTV 聚合频道的 ``canonical_key``

    优先级：先尝试 IPTV 聚合频道（用户订阅里能命中 canonical_key 的话，
    用户期望就是 IPTV）。命中失败再回落到电台 station_id。
    这样可以避免用户自定义订阅的 canonical_key 撞上电台 station_id（如
    ``cnr_1``）时整条频道被错判为电台的情况。
    """
    import main as _m  # 延迟导入，避开循环

    if "source_url" in request.query_params:
        raise HTTPException(status_code=400, detail="source_url 参数已移除，请使用 source_id")

    # 1. IPTV 聚合频道优先：仅当用户订阅里真的存在该 canonical_key 才走 IPTV。
    channels, _groups = await _m._get_aggregated_iptv_channels()
    iptv_match = any(ch.get("canonical_key") == channel_key for ch in channels)
    if iptv_match:
        return await _serve_iptv_channel_playlist(
            channel_key,
            request,
            access,
            source_id=source_id,
            expected_source_revision=expected_source_revision,
        )

    raise HTTPException(status_code=404, detail="频道不存在")


@router.get("/api/media/channel/{channel_key}/stream")
async def media_channel_stream(
    channel_key: str,
    request: Request,
    access: MediaAccessContext = Depends(resolve_media_access),
):
    """Legacy static Radio stream endpoint has been removed.

    Plugin Radio sources use ``/api/media/radio/{station_id}/stream`` with an
    explicit persisted ``source_id``.  This route remains reserved for the
    IPTV channel endpoint and therefore does not interpret a bare key as a
    Radio station.
    """
    raise HTTPException(status_code=404, detail="频道不存在")


@router.get("/api/media/radio/{station_id}/playlist.m3u8")
async def media_radio_playlist(
    station_id: str,
    request: Request,
    source_id: str = Query("", description="Explicit persisted Radio source selector"),
    access: MediaAccessContext = Depends(resolve_media_access),
):
    """Resolve a persisted Radio source through the Plugin media bridge."""
    if not source_id.strip():
        raise HTTPException(status_code=400, detail="source_id 参数必填")
    resolver = getattr(request.app.state, "radio_resolver", None)
    if resolver is None:
        raise HTTPException(status_code=503, detail="Radio Plugin runtime 不可用")
    try:
        resolved = await resolver.resolve_source(source_id.strip(), station_id=station_id)
    except PluginError as exc:
        status = 404 if exc.code in {"RESOURCE_NOT_FOUND", "SCHEME_CONFLICT"} else 503
        raise HTTPException(status_code=status, detail=exc.as_contract()) from exc
    resolved_url = str(resolved.get("url") or "").strip()
    if not resolved_url:
        raise HTTPException(status_code=502, detail="Radio Plugin 未返回播放地址")
    resolved_type = str(resolved.get("source_type") or "audio_http").strip().lower()
    if resolved_type in {"dash", "probe_only"}:
        raise HTTPException(status_code=501, detail="当前 Radio media path 不支持该 transport")
    headers = resolved.get("headers") if isinstance(resolved.get("headers"), dict) else {}
    return await _serve_resolved_source_playlist(
        resolved_url=resolved_url,
        resolved_st=resolved_type,
        custom_ua=str(resolved.get("user_agent") or headers.get("User-Agent") or headers.get("user-agent") or ""),
        referer=str(resolved.get("referer") or headers.get("Referer") or headers.get("referer") or ""),
        cookie="",
        no_ua=False,
        canonical_key=station_id,
        source_id=str(resolved.get("source_id") or source_id.strip()),
        source_revision=str(resolved.get("source_revision") or ""),
        access=access,
        domain="radio",
        handle_ttl=resolved.get("ttl"),
    )


@router.get("/api/media/radio/{station_id}/stream")
async def media_radio_stream(
    station_id: str,
    request: Request,
    source_id: str = Query("", description="Explicit persisted Radio source selector"),
    access: MediaAccessContext = Depends(resolve_media_access),
):
    if not source_id.strip():
        raise HTTPException(status_code=400, detail="source_id 参数必填")
    resolver = getattr(request.app.state, "radio_resolver", None)
    if resolver is None:
        raise HTTPException(status_code=503, detail="Radio Plugin runtime 不可用")
    try:
        resolved = await resolver.resolve_source(source_id.strip(), station_id=station_id)
    except PluginError as exc:
        status = 404 if exc.code in {"RESOURCE_NOT_FOUND", "SCHEME_CONFLICT"} else 503
        raise HTTPException(status_code=status, detail=exc.as_contract()) from exc
    resolved_url = str(resolved.get("url") or "").strip()
    if not resolved_url:
        raise HTTPException(status_code=502, detail="Radio Plugin 未返回播放地址")
    resolved_type = str(resolved.get("source_type") or "audio_http").strip().lower()
    if resolved_type in {"dash", "probe_only"}:
        raise HTTPException(status_code=501, detail="当前 Radio media path 不支持该 transport")
    headers = resolved.get("headers") if isinstance(resolved.get("headers"), dict) else {}
    custom_ua = str(resolved.get("user_agent") or headers.get("User-Agent") or headers.get("user-agent") or "")
    referer = str(resolved.get("referer") or headers.get("Referer") or headers.get("referer") or "")
    source_ref = f"radio:{str(resolved.get('source_id') or source_id.strip())}"
    ctx_id = ""
    if custom_ua or referer or resolved_type in {"audio_http", "mpegts", "http_flv"}:
        ctx_id = get_proxy_context_registry().put(ProxyContext(
            custom_ua=custom_ua,
            referer=referer,
            upstream_url=resolved_url,
            source_type=resolved_type,
            source_id=source_ref,
            source_revision=str(resolved.get("source_revision") or ""),
        ))
    await _validate_handle_url_or_403(resolved_url, allowed_schemes={"http", "https"})
    handle = issue_cached_handle(
        kind="stream",
        url=resolved_url,
        src=f"radio:station:{station_id}:source:{source_id.strip()}",
        src_id=source_ref,
        ctx=ctx_id,
        ttl_seconds=resolved.get("ttl"),
    )
    return RedirectResponse(
        f"/api/media/proxy/stream/{handle}{_media_access_suffix(access)}",
        status_code=307,
    )


async def _serve_iptv_channel_playlist(
    canonical_key: str,
    request: Request,
    access: MediaAccessContext,
    source_id: str = "",
    expected_source_revision: str = "",
) -> Response:
    import main as _m

    channels, _groups = await _m._get_aggregated_iptv_channels()
    channel = next((ch for ch in channels if ch.get("canonical_key") == canonical_key), None)
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")

    requested_source_id = str(source_id or "").strip()
    if requested_source_id:
        _channel, source = await _find_iptv_channel_source(canonical_key, requested_source_id)
        _validate_expected_source_revision(source, expected_source_revision)
        return await _serve_iptv_source_playlist(source, canonical_key, access)

    all_sources = list(channel.get("urls", []) or [])
    for source in all_sources:
        if not source.get("source_id"):
            source["source_id"] = source_id_for(source)

    sources = _m._sorted_sources([
        s for s in all_sources if _playback_source_supported(_m, s)
    ])
    if not sources:
        raise HTTPException(status_code=503, detail="没有可用播放源")

    # 选第一个能播的；逐个尝试以兼容 smart playlist 历史行为
    last_exc: HTTPException | None = None
    for source in sources:
        try:
            return await _serve_iptv_source_playlist(source, canonical_key, access)
        except HTTPException as exc:
            last_exc = exc
            continue
    raise last_exc or HTTPException(status_code=503, detail="所有源均不可用")


async def _serve_iptv_source_playlist(
    source: dict,
    canonical_key: str,
    access: MediaAccessContext,
) -> Response:
    """对单一 source 解析 → 拉上游 → 重写。供频道入口和 smart playlist 共用。"""
    import main as _m

    source_type = _m._source_type(source)
    custom_ua = str(source.get("custom_ua") or "")
    referer = str(source.get("referer") or "")
    raw_url = str(source.get("url") or "").strip()
    source_id = str(source.get("source_id") or source_id_for(source))
    source_revision = source_revision_for(source)
    has_source_snapshot_identity = bool(str(source.get("source_id") or "").strip())
    rtsp_playback_options = resolve_rtsp_playback_options(source)
    if not raw_url:
        raise HTTPException(status_code=502, detail="source url 为空")

    if has_source_snapshot_identity:
        await _assert_current_iptv_source_revision(canonical_key, source_id, source_revision)

    # adapter / YouTube：先 resolve 拿到真实 HTTP/RTSP URL + headers
    if source_type in {"adapter", "youtube", "unsupported_youtube_url"}:
        adapter_url = raw_url
        if source_type in {"youtube", "unsupported_youtube_url"}:
            adapter_url = raw_url if raw_url.lower().startswith("youtube://") else f"youtube://resolve?url={quote(raw_url, safe='')}"
        try:
            provider_resolver = _provider_resolver(_m)
            resolved = await _resolve_provider_source(
                provider_resolver,
                adapter_url,
                _m.http_client,
                source_id=source_id,
                source_revision=source_revision,
            )
            _validate_resolved_source_identity(
                resolved,
                source_id=source_id,
                source_revision=source_revision,
            )
            if has_source_snapshot_identity:
                await _assert_current_iptv_source_revision(canonical_key, source_id, source_revision)
        except _m.AdapterResolveError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.to_payload()) from exc
        except PluginError as exc:
            raise HTTPException(status_code=502, detail=exc.as_contract()) from exc

        resolved_url = str(resolved.get("url") or "").strip()
        if not resolved_url:
            raise HTTPException(status_code=502, detail="adapter 未返回播放地址")
        resolved_st = str(resolved.get("source_type") or "hls").strip().lower()
        headers = resolved.get("headers") if isinstance(resolved.get("headers"), dict) else {}
        ad_ua = str(headers.get("User-Agent") or headers.get("user-agent") or custom_ua)
        ad_ref = str(headers.get("Referer") or headers.get("referer") or referer)
        ad_cookie = str(headers.get("Cookie") or headers.get("cookie") or "")
        no_ua = bool(headers.get("no_ua") or headers.get("No-UA"))
        response = await _serve_resolved_source_playlist(
            resolved_url=resolved_url,
            resolved_st=resolved_st,
            custom_ua=ad_ua,
            referer=ad_ref,
            cookie=ad_cookie,
            no_ua=no_ua,
            canonical_key=canonical_key,
            source_id=source_id,
            source_revision=source_revision,
            access=access,
            rtsp_playback_options=rtsp_playback_options,
        )
        if has_source_snapshot_identity:
            await _assert_current_iptv_source_revision(canonical_key, source_id, source_revision)
        return response

    # 直接 source（非 adapter）
    response = await _serve_resolved_source_playlist(
        resolved_url=raw_url,
        resolved_st=source_type,
        custom_ua=custom_ua,
        referer=referer,
        cookie="",
        no_ua=False,
        canonical_key=canonical_key,
        source_id=source_id,
        source_revision=source_revision,
        access=access,
        rtsp_playback_options=rtsp_playback_options,
    )
    if has_source_snapshot_identity:
        await _assert_current_iptv_source_revision(canonical_key, source_id, source_revision)
    return response


async def _serve_resolved_source_playlist(
    *,
    resolved_url: str,
    resolved_st: str,
    custom_ua: str,
    referer: str,
    cookie: str,
    no_ua: bool,
    canonical_key: str,
    source_id: str,
    source_revision: str,
    access: MediaAccessContext,
    rtsp_playback_options: RtspPlaybackOptions | None = None,
    domain: str = "iptv",
    handle_ttl: int | None = None,
) -> Response:
    """已经解析到 HTTP/RTSP URL 的 source 的统一入口。"""
    import main as _m

    source_ref = source_id or canonical_key
    if not isinstance(handle_ttl, int) or handle_ttl <= 0:
        handle_ttl = None
    if domain == "radio":
        source_ref = f"radio:{source_ref}"
        src_label = f"radio:station:{canonical_key}:source:{source_id or canonical_key}"
    else:
        src_label = f"channel:{canonical_key}:source:{source_ref}"

    # 是否需要建 ProxyContext（动态 header）
    ctx_id = ""
    has_dynamic_headers = bool(
        custom_ua or referer or cookie or no_ua or resolved_st == "audio_http" or source_revision
    )
    if has_dynamic_headers:
        ctx = ProxyContext(
            custom_ua=custom_ua,
            referer=referer,
            cookie=cookie,
            no_ua=no_ua,
            upstream_url=resolved_url,
            source_type=resolved_st,
            source_id=source_ref,
            source_revision=source_revision,
        )
        ctx_id = get_proxy_context_registry().put(ctx)

    if resolved_st == "rtsp":
        # 直接签 rtsp handle 并 302；让客户端命中 /api/media/proxy/rtsp/{handle}
        if not _m.config_rtsp_proxy_enabled():
            raise HTTPException(status_code=503, detail="RTSP 代理已禁用")
        options = rtsp_playback_options or resolve_rtsp_playback_options()
        handle = issue_cached_handle(
            kind="rtsp",
            url=resolved_url,
            src=src_label,
            src_id=source_ref,
            ctx=ctx_id,
            compat=1 if options.compat else 0,
            timestamp_mode=options.timestamp_mode.value,
            ttl_seconds=handle_ttl,
        )
        token_qs = (
            f"?access_token={access.propagated_access_token}"
            if access.propagated_access_token
            else ""
        )
        return RedirectResponse(f"/api/media/proxy/rtsp/{handle}{token_qs}", status_code=307)

    if resolved_st in {"mpegts", "http_flv"}:
        handle = issue_cached_handle(
            kind="stream",
            url=resolved_url,
            src=src_label,
            src_id=source_ref,
            ctx=ctx_id,
            ttl_seconds=handle_ttl,
        )
        suffix = "?stream_type=http_flv" if resolved_st == "http_flv" else ""
        token_qs = (
            f"{'&' if suffix else '?'}access_token={access.propagated_access_token}"
            if access.propagated_access_token
            else ""
        )
        return RedirectResponse(f"/api/media/proxy/stream/{handle}{suffix}{token_qs}", status_code=307)

    if resolved_st == "audio_http":
        handle = issue_cached_handle(
            kind="stream",
            url=resolved_url,
            src=src_label,
            src_id=source_ref,
            ctx=ctx_id,
            ttl_seconds=handle_ttl,
        )
        token_qs = (
            f"?access_token={access.propagated_access_token}"
            if access.propagated_access_token else ""
        )
        return RedirectResponse(f"/api/media/proxy/stream/{handle}{token_qs}", status_code=307)

    # HLS：统一走 Thin playlist 入口；子 playlist/chunk 仍通过 signed handle 代理。
    return await _m.serve_iptv_playlist_by_source(
        upstream_url=resolved_url,
        ctx_id=ctx_id,
        src_label=src_label,
        canonical_key=canonical_key,
        source_id=source_ref,
        source_revision=source_revision,
        access=access,
    )


# ── Cover (channel-based) ─────────────────────────────────────────────────


def _normalize_visual_payload(payload: dict, *, source_id: str, source_revision: str, fallback: dict) -> dict:
    """Project Plugin/compat metadata without letting it replace stable logo."""
    import main as _m

    result = dict(fallback)
    cover_role = str(payload.get("cover_role") or "live")
    legacy_cover = str(payload.get("cover_url") or "").strip()
    stable_cover = str(payload.get("stable_cover_url") or "").strip()
    dynamic_cover = str(payload.get("dynamic_cover_url") or "").strip()
    if cover_role == "stable" and not stable_cover:
        stable_cover = legacy_cover
    elif not dynamic_cover:
        # Existing artifacts use cover_url for both content thumbnails and
        # live screenshots.  Keep that field as the compatibility source;
        # the frontend applies the role/live gate before using it.
        dynamic_cover = legacy_cover
    result.update({
        "source_id": source_id,
        "source_revision": source_revision,
        "avatar_url": str(payload.get("avatar_url") or payload.get("identity_visual") or "").strip(),
        "stable_cover_url": stable_cover,
        "dynamic_cover_url": dynamic_cover,
        "cover_url": legacy_cover or stable_cover or dynamic_cover,
        "is_live": bool(payload.get("is_live", False)),
        "title": str(payload.get("title") or "").strip(),
        "owner_name": str(payload.get("owner_name") or payload.get("anchor_name") or "").strip(),
        "ttl_seconds": int(payload.get("ttl_seconds") or 300),
        "cover_role": cover_role,
    })
    # Keep the existing image anti-hotlink behavior in the generic bridge.
    for field in ("avatar_url", "stable_cover_url", "dynamic_cover_url", "cover_url"):
        raw = result[field]
        if raw:
            result[field] = _m._cover_img_proxy_url(raw)
    return result


async def _select_visual_source(_m, channel: dict, resolver, requested_source_id: str = "") -> dict | None:
    sources = list(channel.get("urls", []) or [])
    for source in sources:
        if not source.get("source_id"):
            source["source_id"] = source_id_for(source)
    if requested_source_id:
        return next((source for source in sources if source.get("source_id") == requested_source_id), None)
    for source in sources:
        if not _is_truthy(source.get("enabled", True)) or source.get("disabled") is True:
            continue
        if resolver is not None and resolver.supports_visual_metadata(str(source.get("url") or "")):
            return source
    # Narrow compatibility only for legacy installations which have not yet
    # received a visual-capable Plugin artifact.  It is never used when a
    # Plugin has declared the generic feature.
    for source in sources:
        adapter = str(source.get("adapter") or "").strip().lower()
        if not adapter:
            continue
        if getattr(_m, "adapter_supports", lambda *_args: False)(adapter, "cover"):
            return source
    return None


@router.get("/api/media/channel/{channel_key}/visual")
async def media_channel_visual(
    channel_key: str,
    request: Request,
    source_id: str = Query("", description="Optional source-scoped visual metadata selector"),
):
    """Return optional source-scoped visual metadata for an IPTV channel.

    The endpoint selects a visual-capable source generically.  It does not
    inspect provider names in the frontend and it never accepts an upstream
    URL from the caller.
    """
    import main as _m

    channels, _groups = await _m._get_aggregated_iptv_channels()
    channel = next((ch for ch in channels if ch.get("canonical_key") == channel_key), None)
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")
    try:
        resolver = _provider_resolver(_m, request)
    except PluginError:
        # Visual metadata is non-critical presentation.  A plugin subsystem
        # that is recovering must not make the channel API fail or affect
        # playback; the stable channel logo remains the fallback.
        resolver = None
    source = await _select_visual_source(_m, channel, resolver, source_id.strip())
    fallback = empty_visual(
        source_id=str(source.get("source_id") or "") if source else "",
        source_revision=source_revision_for(source) if source else "",
        logo_url=str(channel.get("logo_url") or ""),
        title=str(channel.get("name") or ""),
    )
    if source is None:
        return fallback
    source_ref = str(source.get("source_id") or source_id_for(source))
    source_revision = source_revision_for(source)
    source_url = str(source.get("url") or "")

    async def fetch() -> dict:
        try:
            if resolver is not None and resolver.supports_visual_metadata(source_url):
                payload = await resolver.visual_metadata(
                    source_url, source_id=source_ref, source_revision=source_revision,
                )
            else:
                # Compatibility bridge for legacy ownership only.  New Plugin
                # visual implementations never enter this branch.
                payload = await _m.fetch_adapter_cover_payload(source_url)
            return _normalize_visual_payload(
                payload, source_id=source_ref, source_revision=source_revision, fallback=fallback,
            )
        except Exception:
            return dict(fallback)

    return await get_visual_metadata_cache().get_or_fetch(
        source_id=source_ref,
        source_revision=source_revision,
        fallback=fallback,
        fetch=fetch,
    )


@router.get("/api/media/channel/{canonical_key}/cover")
async def media_channel_cover(
    canonical_key: str,
    request: Request,
):
    """返回该频道 adapter 类源的封面元数据（cover_url/avatar_url 已经是 image handle）。

    使用轻量 CoverCache：
    - 频道索引缓存 60s，不每次全量聚合；
    - Adapter fetch 受 Semaphore(4) 限制；
    - 同一 key 的并发请求合并为一次执行；
    - 非 adapter 频道直接返回 logo_url 兜底并缓存。
    """
    from core.cover_cache import get_cover_cache

    cache = get_cover_cache()
    channel = await cache.get_channel(canonical_key)
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")

    sources = channel.get("urls", []) or []
    import main as _m
    adapter_source = next((s for s in sources if _m._source_type(s) == "adapter"), None)
    adapter_url = adapter_source["url"] if adapter_source else None

    return await cache.get_or_fetch(
        canonical_key=canonical_key,
        adapter_source_url=adapter_url,
        logo_url=channel.get("logo_url") or "",
        channel_name=channel.get("name") or "",
    )


# ── Handle 路由（playlist / chunk / stream / rtsp / image）────────────────


@router.get("/api/media/proxy/playlist/{handle}")
async def media_proxy_playlist(
    handle: str,
    access: MediaAccessContext = Depends(resolve_media_access),
):
    import main as _m

    payload = _safe_decode(handle, expected_kind="playlist")

    ctx = get_proxy_context_registry().get(payload.ctx) if payload.ctx else None
    await _validate_iptv_proxy_context(ctx)
    headers = _ctx_to_request_headers(ctx)
    headers.setdefault("Accept-Encoding", "identity")

    # 所有 signed playlist handle 都走同一条 Thin 拉取链：重试、single-flight、
    # 短暂成功缓存和安全 redirect guard 均由共享 helper 负责。
    source_revision = ctx.source_revision if ctx else ""
    cache_scope = f"{payload.src_id}:{source_revision}" if payload.src_id else source_revision
    raw_text, base_url = await _m._thin_playlist_fetch(
        payload.url,
        headers,
        cache_scope=cache_scope,
        omit_user_agent=bool(ctx and ctx.no_ua and not ctx.custom_ua),
    )
    await _validate_iptv_proxy_context(ctx)

    rewrite_ctx = _build_rewrite_context(
        base_url=base_url,
        src_id=payload.src_id,
        src_label=payload.src,
        ctx_id=payload.ctx,
        access_ctx=access,
        proxy_segments=True,
    )
    body = rewrite_m3u8(raw_text, rewrite_ctx)
    return Response(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# HLS 客户端、CDN 预热和某些 link prefetch 会发 HEAD；只声明 GET 会让上游
# 立即收到 405。这里把 HEAD 接到同一处理：FastAPI/Starlette 在 method=HEAD
# 时会自动跳过响应体，handler 内部不需要分支。
@router.api_route(
    "/api/media/proxy/chunk/{handle}",
    methods=["GET", "HEAD"],
)
async def media_proxy_chunk(
    handle: str,
    request: Request,
    _: MediaAccessContext = Depends(resolve_media_access),
):
    import main as _m

    payload = _safe_decode(handle, expected_kind="chunk")

    # HEAD 请求只回必要的元信息：handle 解析合法 + URL 通过 SSRF 校验就够了。
    # 不去打上游 HEAD（很多直播源会拒 HEAD），也不开 GET 流，避免被预检放大成
    # 真实下载。
    if request.method == "HEAD":
        await _validate_handle_url_or_403(payload.url, allowed_schemes={"http", "https"})
        return Response(
            content=b"",
            status_code=200,
            media_type=_fallback_chunk_content_type(payload.url, ""),
            headers={
                "Cache-Control": "no-store",
                # Range 是否可用要看上游；HEAD 阶段我们不下结论，告知 client
                # 即可见 ``Accept-Ranges: bytes`` 是 GET 时按上游透传的事实。
                "Accept-Ranges": "none",
            },
        )

    ctx = get_proxy_context_registry().get(payload.ctx) if payload.ctx else None
    headers = _ctx_to_request_headers(ctx, fallback_referer="")
    if not ctx and "User-Agent" not in headers:
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
        )
    # 直播分片走身份编码：避免 httpx 默认 ``Accept-Encoding: gzip,...`` →
    # 上游返回压缩内容 + 原始 Content-Length 不一致导致 hls.js 判定截断。
    headers["Accept-Encoding"] = "identity"
    for header_name in ("range", "if-range", "if-none-match", "if-modified-since"):
        value = request.headers.get(header_name)
        if value:
            headers["-".join(part.capitalize() for part in header_name.split("-"))] = value

    try:
        upstream = await stream_with_safe_redirects(
            _m.http_client,
            "GET",
            payload.url,
            headers=headers,
            omit_headers={"User-Agent"} if ctx and ctx.no_ua and not ctx.custom_ua else None,
        )
    except RedirectTargetRejected as exc:
        raise HTTPException(status_code=403, detail=str(exc.cause)) from exc
    except httpx.TimeoutException:
        return Response(content=b"", status_code=504, media_type="text/plain")
    except httpx.HTTPError as exc:
        logger.warning(f"chunk proxy failed for {payload.url[:80]}: {exc}")
        return Response(content=b"", status_code=502, media_type="text/plain")

    content_type = _fallback_chunk_content_type(
        payload.url,
        upstream.headers.get("content-type", ""),
    )
    response_headers = _safe_upstream_response_headers(upstream.headers, live_chunk=True)
    # 上游 4xx/5xx 也透传给客户端；不要把 404/410 升级成 502。
    # hls.js 对 404 的 fragLoadError 自带 retry，比一刀切的 502 友好得多。
    return StreamingResponse(
        _stream_httpx_response(upstream),
        status_code=upstream.status_code,
        media_type=content_type,
        headers=response_headers,
    )


@router.get("/api/media/proxy/stream/{handle}")
async def media_proxy_stream(
    handle: str,
    request: Request,
    stream_type: str = "",
    _: MediaAccessContext = Depends(resolve_media_access),
):
    import main as _m

    payload = _safe_decode(handle, expected_kind="stream")
    await _validate_handle_url_or_403(payload.url, allowed_schemes={"http", "https"})

    ctx = get_proxy_context_registry().get(payload.ctx) if payload.ctx else None
    await _validate_iptv_proxy_context(ctx)
    parsed = urlparse(payload.url)
    upstream_headers = {
        "User-Agent": (ctx.custom_ua if ctx else "") or _m.CDN_REQUEST_HEADERS["User-Agent"],
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Referer": (ctx.referer if ctx and ctx.referer else f"{parsed.scheme}://{parsed.netloc}/"),
    }
    if ctx and ctx.cookie:
        upstream_headers["Cookie"] = ctx.cookie

    resolved_stream_type = (ctx.source_type if ctx and ctx.source_type else stream_type)
    return await _m.serve_iptv_proxy_stream_response(
        request=request,
        upstream_url=payload.url,
        upstream_headers=upstream_headers,
        stream_type=resolved_stream_type,
    )


@router.get("/api/media/proxy/rtsp/{handle}")
async def media_proxy_rtsp(
    handle: str,
    _: MediaAccessContext = Depends(resolve_media_access),
):
    import main as _m

    payload = _safe_decode(handle, expected_kind="rtsp")
    ctx = get_proxy_context_registry().get(payload.ctx) if payload.ctx else None
    await _validate_iptv_proxy_context(ctx)
    custom_ua = ctx.custom_ua if ctx else ""

    playback_options = resolve_rtsp_playback_options(
        compat=bool(payload.compat),
        timestamp_mode=payload.timestamp_mode,
    )
    return await _m.serve_rtsp_playlist_response(
        upstream_url=payload.url,
        custom_ua=custom_ua,
        playback_options=playback_options,
    )


@router.api_route(
    "/api/media/proxy/image/{handle}",
    methods=["GET", "HEAD"],
)
async def media_proxy_image(
    handle: str,
    request: Request,
):
    """封面图片 handle。仅供匿名/受限来源访问；不接受 chunk/playlist URL。"""
    import main as _m

    # 封面图允许匿名（与原 require_browse_access 一致），但仍要保护：
    # 这里要求至少能浏览。
    from security.dependencies import require_browse_access as _rb
    await _rb(request)

    payload = _safe_decode(handle, expected_kind="image")
    await _validate_handle_url_or_403(payload.url, allowed_schemes={"http", "https"})

    referer = _m._cover_img_referer_for(payload.url)
    if not referer:
        raise HTTPException(status_code=403, detail="该域名不在封面代理白名单中")

    # HEAD：handle 与 referer 校验都通过即可，直接给 200 + image/* 占位。
    # 不去抓上游，避免封面 HEAD 被放大成全量下载。
    if request.method == "HEAD":
        return Response(
            content=b"",
            status_code=200,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )

    try:
        upstream = await request_with_safe_redirects(
            _m.http_client,
            "GET",
            payload.url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": referer},
        )
        upstream.raise_for_status()
    except RedirectTargetRejected:
        raise HTTPException(status_code=403, detail="封面图片重定向目标不安全")
    except Exception:
        raise HTTPException(status_code=502, detail="封面图片获取失败")

    # 上游 content-type 透传必须是 image/*，否则降级到 image/jpeg 占位（不暴露
    # 上游误返回的 text/html 等内容到 <img> 之外的环境，理论上 <img> 不执行
    # 脚本，但搭配 ACAO:* + 长缓存仍是冗余风险）。
    upstream_ct = (upstream.headers.get("content-type") or "").strip()
    media_type = upstream_ct if upstream_ct.lower().startswith("image/") else "image/jpeg"

    return Response(
        content=upstream.content,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── 管理员 URL probe ──────────────────────────────────────────────────────


@router.post("/api/admin/probes/url")
async def admin_probe_url(
    payload: dict,
    _admin: dict = Depends(require_admin),
):
    """管理员手动测试任意 URL 的可达性。

    限制：

    * 仅 ``http``/``https``；
    * SSRF 校验；
    * 仅返回 status/headers 摘要，不流式回客户端；
    * 上游响应大小限制 2 MiB（封面/简短 m3u8 足够）。
    """
    import main as _m

    url = str((payload or {}).get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url 不能为空")
    upstream = None
    try:
        upstream = await request_with_safe_redirects(
            _m.http_client,
            "GET",
            url,
            timeout=8.0,
            headers={"User-Agent": _m.CDN_REQUEST_HEADERS["User-Agent"]},
        )
    except RedirectTargetRejected as exc:
        raise HTTPException(status_code=403, detail="探测目标被 SSRF 安全策略拒绝") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"探测失败: {exc}") from exc

    try:
        body_preview = upstream.content[: 2 * 1024]
        try:
            text_preview = body_preview.decode("utf-8", errors="replace")
        except Exception:
            text_preview = ""
        return {
            "ok": True,
            "status": upstream.status_code,
            "final_url": redact_url(str(upstream.url)),
            "content_type": upstream.headers.get("content-type", ""),
            "content_length": int(upstream.headers.get("content-length") or 0),
            "preview": text_preview,
        }
    finally:
        await upstream.aclose()
def _provider_resolver(main_module, request: Request | None = None):
    request_app = getattr(request, "app", None) if request is not None else None
    app = request_app or getattr(main_module, "app", None)
    state = getattr(app, "state", None)
    resolver = getattr(state, "provider_resolver", None)
    if resolver is None:
        raise PluginError("PLUGIN_UNAVAILABLE", "Provider resolver is unavailable", category="lifecycle")
    return resolver
