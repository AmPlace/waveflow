from typing import Any

import httpx

from . import AdapterRequest, AdapterResolveError




_ND0593TV_API_URL = "https://app.0593tv.cn/jhxtapi/jhxt/Live/detail"
_ND0593TV_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "QZWireless/20241122 CFNetwork/3860.500.112 Darwin/25.4.0",
}

ND0593TV_CHANNELS: dict[str, dict[str, str]] = {
    "nd-culture": {"lid": "20", "name": "宁德文化旅游"},
    "nd-news":    {"lid": "21", "name": "宁德新闻综合"},
}

_ND0593TV_ALIASES: dict[str, str] = {
    "20": "nd-culture",
    "21": "nd-news",
    "culture": "nd-culture",
    "news": "nd-news",
}


async def resolve_nd0593tv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    channel_key = _ND0593TV_ALIASES.get(channel_key, channel_key)
    entry = ND0593TV_CHANNELS.get(channel_key)
    if entry:
        lid = entry["lid"]
        channel_name = entry["name"]
    elif channel_key.isdigit():
        lid = channel_key
        channel_name = channel_key
    else:
        supported = ", ".join(sorted(ND0593TV_CHANNELS) + sorted(_ND0593TV_ALIASES))
        raise AdapterResolveError(
            "invalid_nd0593tv_channel_id",
            f"不支持的宁德频道: {channel_key}，支持的频道有: {supported}",
        )

    payload = {
        "uid": "0",
        "device": "",
        "nid": "",
        "lid": lid,
        "siteid": "1",
    }

    try:
        resp = await client.post(
            _ND0593TV_API_URL,
            headers=_ND0593TV_HEADERS,
            data=payload,
            timeout=10.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "nd0593tv_request_failed",
            f"宁德广电接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise AdapterResolveError(
            "nd0593tv_parse_failed",
            "宁德广电接口返回非 JSON",
            status_code=502,
            retryable=True,
        ) from exc

    if not isinstance(data, dict) or data.get("code") != 200:
        raise AdapterResolveError(
            "nd0593tv_business_error",
            f"宁德广电业务报错: {data!r}",
            status_code=502,
            retryable=True,
        )

    play_url = ((data.get("data") or {}).get("link") or "").strip()
    if not play_url.startswith(("http://", "https://")):
        raise AdapterResolveError(
            "nd0593tv_no_stream",
            f"宁德广电频道 {channel_name} 没有返回播放地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "nd0593tv",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": 30 * 60,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": channel_name,
        "volatile_url": True,
    }
