from typing import Any

import httpx

from . import AdapterRequest, AdapterResolveError




_QUKAN_API_URL = "https://www.qukanvideo.com/h5/channel/view/item/AntiTheft/playUrl"
_QUKAN_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://www.qukanvideo.com",
    "Referer": "https://www.qukanvideo.com/",
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
    ),
}

QUKAN_CHANNELS: dict[str, dict[str, str]] = {
    "jimei": {
        "name": "厦门集美电视台综合频道",
        "live_id": "1778569138673121",
        "sign": "18d34b7bfe26df9912192850e3eb36c3",
    },
}


async def resolve_qukan(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    entry = QUKAN_CHANNELS.get(channel_key)
    if not entry:
        supported = ", ".join(sorted(QUKAN_CHANNELS))
        raise AdapterResolveError(
            "invalid_qukan_channel_id",
            f"不支持的趣看频道: {channel_key}，支持的频道有: {supported}",
        )

    payload = {
        "source": "web",
        "liveId": entry["live_id"],
        "sign": entry["sign"],
    }

    referer = f"https://www.qukanvideo.com/cloud/h5/{entry['live_id']}"
    headers = {**_QUKAN_HEADERS, "Referer": referer}

    try:
        resp = await client.post(
            _QUKAN_API_URL,
            headers=headers,
            data=payload,
            timeout=10.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "qukan_request_failed",
            f"趣看接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise AdapterResolveError(
            "qukan_parse_failed",
            "趣看接口返回非 JSON",
            status_code=502,
            retryable=True,
        ) from exc

    if not isinstance(data, dict) or data.get("code") != 0:
        raise AdapterResolveError(
            "qukan_business_error",
            f"趣看接口业务报错: {data!r}",
            status_code=502,
            retryable=True,
        )

    play_url = ((data.get("value") or {}).get("url") or "").strip()
    if not play_url.startswith(("http://", "https://")):
        raise AdapterResolveError(
            "qukan_no_stream",
            f"趣看频道 {entry['name']} 没有返回播放地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "qukan",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": 30 * 60,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": entry["name"],
        "volatile_url": True,
    }
