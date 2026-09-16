import json
from typing import Any
from urllib.parse import urlparse

import httpx
from streamget import DouyuLiveStream

from . import AdapterRequest, AdapterResolveError


ADAPTER_CAPABILITIES = {"cover": True}

# CDN selection priority:
# 1. Prefer douyucdn2.cn (stable)
# 2. Avoid edgesrv.com:8443 (TLS issues)
# 3. Fallback to other non-edgesrv
# 4. Last resort: edgesrv
_PREFERRED_CDN = "douyucdn2.cn"
_BLOCKED_CDN_HOSTS = {"edgesrv.com"}


def _cdn_score(url: str) -> int:
    """Lower score = higher priority. -1 = blocked."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return 100
    if any(blocked in host for blocked in _BLOCKED_CDN_HOSTS):
        if ":8443" in url:
            return -1  # blocked: TLS handshake failure
        return 50  # edgesrv without 8443: low priority
    if _PREFERRED_CDN in host:
        return 0  # best
    return 10  # other CDN: acceptable


def _select_best_cdn(all_urls: list[str]) -> str | None:
    """Select the best CDN URL from a list, preferring stable CDNs."""
    if not all_urls:
        return None
    scored = [(u, _cdn_score(u)) for u in all_urls]
    # Filter out blocked (-1), sort by score ascending
    candidates = [(u, s) for u, s in scored if s >= 0]
    if not candidates:
        # All blocked — last resort: use the first one anyway
        return all_urls[0]
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


async def resolve_douyu(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    room_id = request.resource_id.strip("/")
    if not room_id:
        raise AdapterResolveError("invalid_douyu_room_id", "斗鱼房间号不能为空")

    douyu_url = f"https://www.douyu.com/{room_id}"

    try:
        live = DouyuLiveStream()
        data = await live.fetch_web_stream_data(douyu_url)
        stream_obj = await live.fetch_stream_url(data, "OD")
        json_str = stream_obj.to_json()
        result = json.loads(json_str)
    except Exception as exc:
        raise AdapterResolveError(
            "douyu_resolve_failed",
            f"斗鱼解析失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not result.get("is_live"):
        raise AdapterResolveError(
            "douyu_not_live",
            "该斗鱼主播未开播",
            status_code=502,
            retryable=False,
        )

    # Collect all available CDN URLs
    all_urls = []
    primary_url = result.get("flv_url") or result.get("m3u8_url") or ""
    if primary_url:
        all_urls.append(primary_url)
    backup_urls = (result.get("extra") or {}).get("backup_url_list") or []
    all_urls.extend(backup_urls)

    if not all_urls:
        raise AdapterResolveError(
            "douyu_no_play_url",
            "斗鱼没有返回可播放地址",
            status_code=502,
            retryable=True,
        )

    # Select best CDN (avoid edgesrv:8443 TLS issues)
    play_url = _select_best_cdn(all_urls)
    source_type = "http_flv" if result.get("flv_url") else "hls"

    return {
        "ok": True,
        "adapter": "douyu",
        "source_type": source_type,
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": 0,
        "cacheable": False,
        "volatile_url": True,
        "expires_at": None,
        "warnings": [],
        "anchor_name": result.get("anchor_name", ""),
    }
