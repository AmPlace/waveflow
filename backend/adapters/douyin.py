import asyncio
import json
from streamget import DouyinLiveStream
from typing import Any

import httpx

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError


async def resolve_douyin(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    room_id = request.resource_id.strip("/")
    if not room_id:
        raise AdapterResolveError("invalid_douyin_room_id", "抖音房间号不能为空")

    douyin_url = f"https://live.douyin.com/{room_id}"

    try:
        live = DouyinLiveStream()
        data = await live.fetch_web_stream_data(douyin_url)
        stream_obj = await live.fetch_stream_url(data, "OD")
        json_str = stream_obj.to_json()
        result = json.loads(json_str)
    except Exception as exc:
        raise AdapterResolveError(
            "douyin_resolve_failed",
            f"抖音解析失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not result.get("is_live"):
        raise AdapterResolveError(
            "douyin_not_live",
            "该抖音主播未开播",
            status_code=502,
            retryable=False,
        )

    play_url = result.get("m3u8_url") or result.get("flv_url")
    if not play_url:
        raise AdapterResolveError(
            "douyin_no_play_url",
            "抖音没有返回可播放地址",
            status_code=502,
            retryable=True,
        )

    source_type = "hls" if result.get("m3u8_url") else "http_flv"

    return {
        "ok": True,
        "adapter": "douyin",
        "source_type": source_type,
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": ADAPTER_SUCCESS_TTL_SECONDS,
        "expires_at": None,
        "warnings": [],
        "anchor_name": result.get("anchor_name", ""),
    }
