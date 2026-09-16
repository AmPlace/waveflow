import json
from typing import Any

import httpx
import xxtea

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError

NMTV_CHANNELS = {
    "nmws":   [262, "内蒙古卫视"],
    "nmmyws": [126, "内蒙古蒙古语卫视"],
    "nmxwzh": [127, "内蒙古新闻综合"],
    "nmjjsh": [128, "内蒙古经济生活"],
    "nmse":   [129, "内蒙古少儿频道"],
    "nmwtyl": [130, "内蒙古文体娱乐"],
    "nmnm":   [131, "内蒙古农牧频道"],
    "nmwh":   [132, "内蒙古蒙古语文化"],
    "hhht1":  [141, "呼和浩特新闻综合"],
    "xlgl1":  [156, "锡林郭勒新闻综合"],
    "als1":   [157, "阿拉善新闻综合"],
    "byle1":  [158, "巴彦淖尔新闻综合"],
    "erds1":  [159, "鄂尔多斯新闻综合"],
    "cf1":    [161, "赤峰新闻综合"],
    "tl1":    [163, "通辽新闻综合"],
    "wlcb1":  [164, "乌兰察布新闻综合"],
    "wh1":    [165, "乌海新闻综合"],
    "hlbe1":  [166, "呼伦贝尔新闻综合"],
    "xa1":    [167, "兴安新闻综合"],
    "bt1":    [168, "包头新闻综合"],
}

NMTV_API_URL = "https://api-bt.nmtv.cn/broadcast/list"
NMTV_XXTEA_KEY = b"5b28bae827e651b3"
NMTV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.nmtv.cn/",
}


async def resolve_nmtv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    channel = NMTV_CHANNELS.get(channel_key)
    if not channel:
        if channel_key.isdigit():
            target_id = int(channel_key)
        else:
            supported = ", ".join(NMTV_CHANNELS)
            raise AdapterResolveError(
                "invalid_nmtv_channel_id",
                f"不支持的内蒙古频道 ID: {channel_key}，支持的频道有: {supported}",
            )
    else:
        target_id = channel[0]

    try:
        resp = await client.get(
            NMTV_API_URL,
            params={"size": "100", "type": "1"},
            headers=NMTV_HEADERS,
            follow_redirects=True,
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "nmtv_request_failed",
            f"内蒙古广电接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    try:
        import base64

        raw = resp.text.strip().strip('"')
        encrypted = base64.b64decode(raw)
        decrypted = xxtea.decrypt(encrypted, NMTV_XXTEA_KEY, padding=False)
        text = decrypted.decode("utf-8", errors="ignore")
        data = json.JSONDecoder().raw_decode(text)[0]
    except Exception as exc:
        raise AdapterResolveError(
            "nmtv_decrypt_failed",
            f"内蒙古广电数据解密失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    target = None
    for v in data.get("data", []):
        d = v.get("data", {})
        if d.get("id") == target_id:
            target = d
            break

    if not target:
        raise AdapterResolveError(
            "nmtv_channel_not_found",
            f"内蒙古广电未找到频道 ID: {target_id}",
            status_code=502,
            retryable=True,
        )

    streams = target.get("streamUrls", [])
    if not streams:
        raise AdapterResolveError(
            "nmtv_no_stream",
            "内蒙古广电没有返回播放地址",
            status_code=502,
            retryable=True,
        )

    m3u8_url = streams[0]
    channel_name = (channel[1] if channel else target.get("name") or str(target_id))

    return {
        "ok": True,
        "adapter": "nmtv",
        "source_type": "hls",
        "url": m3u8_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": dict(NMTV_HEADERS),
        "ttl": ADAPTER_SUCCESS_TTL_SECONDS,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": channel_name,
        "volatile_url": True,
    }
