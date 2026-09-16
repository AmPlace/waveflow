import json
from typing import Any

import httpx
from streamget import WeiboLiveStream

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError


async def resolve_weibo(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    room_id = request.resource_id.strip("/")
    if not room_id:
        raise AdapterResolveError("invalid_weibo_room_id", "微博 房间号不能为空")

    # 支持两种格式：
    # weibo://UID → 查找该用户的直播
    # weibo://1022:ROOM_ID → 直接用 show URL
    if room_id.startswith("1022:"):
        url = f"https://weibo.com/show/{room_id}"
    elif room_id.isdigit():
        url = f"https://weibo.com/u/{room_id}"
    else:
        url = f"https://weibo.com/show/{room_id}"

    try:
        live = WeiboLiveStream(cookies="")
        data = await live.fetch_web_stream_data(url)
        stream_obj = await live.fetch_stream_url(data, "OD")
        json_str = stream_obj.to_json()
        result = json.loads(json_str)
    except Exception as exc:
        raise AdapterResolveError(
            "weibo_resolve_failed",
            f"微博 解析失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not result.get("is_live"):
        raise AdapterResolveError(
            "weibo_not_live",
            "该 微博 主播未开播",
            status_code=502,
            retryable=False,
        )

    play_url = result.get("flv_url") or result.get("m3u8_url") or result.get("record_url")
    if not play_url:
        raise AdapterResolveError(
            "weibo_no_play_url",
            "微博 没有返回可播放地址",
            status_code=502,
            retryable=True,
        )

    source_type = "http_flv" if result.get("flv_url") else "hls"

    return {
        "ok": True,
        "adapter": "weibo",
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
