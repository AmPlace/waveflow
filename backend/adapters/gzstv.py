import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from . import AdapterRequest, AdapterResolveError


GZSTV_CHANNELS = {
    "ch01": "贵州卫视",
    "ch02": "公共频道",
    "ch03": "影视文艺频道",
    "ch04": "大众生活频道",
    "ch05": "生态·乡村频道",
    "ch06": "科教健康频道",
    "ch13": "贵州移动电视",
}

GZSTV_API_BASE = "https://api.gzstv.com/v1/tv"
GZSTV_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.gzstv.com",
    "Referer": "https://www.gzstv.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
GZSTV_SIGN_REFRESH_MARGIN_SECONDS = 60
GZSTV_FALLBACK_TTL_SECONDS = 5 * 60


def _normalize_channel_id(resource_id: str) -> str:
    value = resource_id.strip("/").lower()
    if value.isdigit():
        value = f"ch{int(value):02d}"
    return value


def _extract_tx_time(play_url: str) -> int | None:
    try:
        parsed = urlparse(play_url)
    except ValueError:
        return None

    tx_time = (parse_qs(parsed.query).get("txTime") or [""])[0].strip()
    if not tx_time:
        return None
    try:
        return int(tx_time, 16)
    except ValueError:
        return None


def _cache_ttl_from_expiry(expires_at: int | None) -> int:
    if not expires_at:
        return GZSTV_FALLBACK_TTL_SECONDS
    return max(0, int(expires_at - time.time() - GZSTV_SIGN_REFRESH_MARGIN_SECONDS))


async def resolve_gzstv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_id = _normalize_channel_id(request.resource_id)
    channel_name = GZSTV_CHANNELS.get(channel_id)
    if not channel_name:
        supported = ", ".join(GZSTV_CHANNELS)
        raise AdapterResolveError(
            "invalid_gzstv_channel_id",
            f"不支持的贵州频道 ID: {channel_id}，支持的频道有: {supported}",
        )

    api_url = f"{GZSTV_API_BASE}/{channel_id}/"
    try:
        response = await client.get(
            api_url,
            params={"fields": "description,title,stream_url,image,author"},
            headers={**GZSTV_HEADERS, "Referer": f"https://www.gzstv.com/tv/{channel_id}"},
            follow_redirects=True,
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "gzstv_request_failed",
            f"贵州广电接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc
    except ValueError as exc:
        raise AdapterResolveError(
            "gzstv_response_parse_failed",
            "贵州广电接口返回的不是有效 JSON",
            status_code=502,
            retryable=True,
        ) from exc

    if not isinstance(payload, dict):
        raise AdapterResolveError(
            "gzstv_response_parse_failed",
            "贵州广电接口数据格式不正确",
            status_code=502,
            retryable=True,
        )

    play_url = str(payload.get("stream_url") or "").strip()
    if not play_url:
        raise AdapterResolveError(
            "gzstv_no_play_url",
            "贵州广电没有返回有效直播地址",
            status_code=502,
            retryable=True,
        )

    expires_at = _extract_tx_time(play_url)
    ttl = _cache_ttl_from_expiry(expires_at)
    url_ttl = max(0, int(expires_at - time.time())) if expires_at else None

    return {
        "ok": True,
        "adapter": "gzstv",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {
            "Referer": "https://www.gzstv.com/",
            "User-Agent": GZSTV_HEADERS["User-Agent"],
        },
        "ttl": ttl,
        "expires_at": expires_at,
        "warnings": [],
        "channel_id": channel_id,
        "channel_name": str(payload.get("title") or channel_name),
        "url_expires_at": expires_at,
        "url_ttl": url_ttl,
        "volatile_url": True,
    }
