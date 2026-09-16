import json
from typing import Any

import httpx
from streamget import BilibiliLiveStream

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError


# 该 adapter 自描述能力清单。中央实现（fetch 函数、缓存、路由）仍在 main.py，
# 这里只声明"本 adapter 支持取直播间封面/头像"。
ADAPTER_CAPABILITIES = {"cover": True}


async def resolve_bilibili(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    room_id = request.resource_id.strip("/")
    if not room_id:
        raise AdapterResolveError("invalid_bilibili_room_id", "Bilibili 房间号不能为空")

    bilibili_url = f"https://live.bilibili.com/{room_id}"

    try:
        live = BilibiliLiveStream(cookies="")
        info = await live.fetch_web_stream_data(bilibili_url)
        stream_obj = await live.fetch_stream_url(info, "OD")
        json_str = stream_obj.to_json()
        result = json.loads(json_str)
    except Exception as exc:
        raise AdapterResolveError(
            "bilibili_resolve_failed",
            f"Bilibili 解析失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not result.get("is_live"):
        raise AdapterResolveError(
            "bilibili_not_live",
            "该 Bilibili 主播未开播",
            status_code=502,
            retryable=False,
        )

    play_url = result.get("flv_url") or result.get("record_url")
    if not play_url:
        raise AdapterResolveError(
            "bilibili_no_play_url",
            "Bilibili 没有返回可播放地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "bilibili",
        "source_type": "http_flv",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": ADAPTER_SUCCESS_TTL_SECONDS,
        "expires_at": None,
        "warnings": [],
        "anchor_name": result.get("anchor_name", ""),
    }
