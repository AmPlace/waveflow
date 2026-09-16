from typing import Any

import httpx

from . import AdapterRequest, AdapterResolveError

NOWTV_API_URL = "https://webtvapi.now.com/10/7/getLiveURL"
NOWTV_CHANNELS = {
    "NEWS":    {"id": "331", "name": "NOW 新闻台"},
    "FINANCE": {"id": "332", "name": "NOW 财经台"},
    "LIVE":    {"id": "333", "name": "NOW 直播新闻台"},
}


async def resolve_nowtv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").upper()
    ch = NOWTV_CHANNELS.get(channel_key)
    if not ch:
        if channel_key.isdigit():
            ch = {"id": channel_key, "name": f"NOW CH{channel_key}"}
        else:
            supported = ", ".join(NOWTV_CHANNELS)
            raise AdapterResolveError(
                "invalid_nowtv_channel",
                f"不支持的 NOW TV 频道: {channel_key}，支持: {supported}",
            )

    body = {
        "deviceType": "IOS_PHONE",
        "contentId": ch["id"],
        "audioCode": "A",
        "deviceId": "8269809F-7702-45CE-9378-D7157A2E6819",
        "mode": "prod",
        "callerReferenceNo": "20140702122500",
        "contentType": "Channel",
    }

    try:
        resp = await client.post(
            NOWTV_API_URL,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "NNC/6.3.0 (com.now.news; build:2309121224; iOS 17.1.0) Alamofire/5.2.2",
            },
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "nowtv_api_failed",
            f"NOW TV API 请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    try:
        data = resp.json()
    except ValueError:
        raise AdapterResolveError("nowtv_parse_failed", "NOW TV API 返回非 JSON", status_code=502, retryable=True)

    if data.get("responseCode") != "SUCCESS":
        raise AdapterResolveError(
            "nowtv_failed",
            f"NOW TV {ch['name']} 失败: {data.get('responseCode')}",
            status_code=502,
            retryable=True,
        )

    assets = data.get("asset", [])
    if not assets:
        raise AdapterResolveError("nowtv_no_url", f"NOW TV {ch['name']} 无播放地址", status_code=502, retryable=True)

    return {
        "ok": True,
        "adapter": "nowtv",
        "source_type": "hls",
        "url": assets[0],
        "direct_playable": False,
        "requires_proxy": True,
        "headers": {},
        "ttl": 5 * 60,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": ch["name"],
        "volatile_url": True,
    }
