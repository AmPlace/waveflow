import json
from typing import Any

import httpx
from streamget import BluedLiveStream

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError


async def resolve_blued(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    room_id = request.resource_id.strip("/")
    if not room_id:
        raise AdapterResolveError("invalid_blued_room_id", "Blued 房间号不能为空")

    blued_url = f"https://blued.com/live/{room_id}"

    try:
        live = BluedLiveStream(cookies="")
        data = await live.fetch_web_stream_data(blued_url)
        stream_obj = await live.fetch_stream_url(data, "OD")
        json_str = stream_obj.to_json()
        result = json.loads(json_str)
    except Exception as exc:
        raise AdapterResolveError(
            "blued_resolve_failed",
            f"Blued 解析失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not result.get("is_live"):
        raise AdapterResolveError(
            "blued_not_live",
            "该 Blued 主播未开播",
            status_code=502,
            retryable=False,
        )

    play_url = result.get("m3u8_url") or result.get("record_url")
    if not play_url:
        raise AdapterResolveError(
            "blued_no_play_url",
            "Blued 没有返回可播放地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "blued",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": ADAPTER_SUCCESS_TTL_SECONDS,
        "expires_at": None,
        "warnings": [],
        "anchor_name": result.get("anchor_name", ""),
    }
