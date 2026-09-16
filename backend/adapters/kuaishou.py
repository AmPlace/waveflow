import json
import re
from typing import Any

import httpx
from curl_cffi.requests import AsyncSession

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError


ADAPTER_CAPABILITIES = {"cover": True}


async def resolve_kuaishou(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    room_id = request.resource_id.strip("/")
    if not room_id:
        raise AdapterResolveError("invalid_kuaishou_room_id", "快手房间号不能为空")

    kuaishou_url = f"https://live.kuaishou.com/u/{room_id}"

    try:
        async with AsyncSession(impersonate="chrome") as session:
            resp = await session.get(kuaishou_url)

        if resp.status_code != 200:
            raise AdapterResolveError(
                "kuaishou_http_error",
                f"快手 HTTP 请求失败: {resp.status_code}",
                status_code=502,
                retryable=True,
            )

        html = resp.text
        match = re.search(
            r'<script>window\.__INITIAL_STATE__=(.*?);\(function\(\)\{var s;', html
        )
        if not match:
            raise AdapterResolveError(
                "kuaishou_parse_failed",
                "快手页面解析失败，未找到直播数据",
                status_code=502,
                retryable=True,
            )

        raw = match.group(1)
        play_list_str = re.findall(r'(\{"liveStream".*?),"gameInfo', raw)[0] + "}"
        play_list = json.loads(play_list_str)
        live_stream = play_list.get("liveStream", {})
        author = play_list.get("author", {})
        anchor_name = author.get("name", "")

    except AdapterResolveError:
        raise
    except Exception as exc:
        raise AdapterResolveError(
            "kuaishou_resolve_failed",
            f"快手解析失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not live_stream:
        raise AdapterResolveError(
            "kuaishou_not_live",
            "该快手主播未开播",
            status_code=502,
            retryable=False,
        )

    # 提取 FLV 流地址
    play_urls = live_stream.get("playUrls")
    if not play_urls:
        raise AdapterResolveError(
            "kuaishou_no_play_url",
            "快手没有返回可播放地址",
            status_code=502,
            retryable=True,
        )

    # 优先取 h264 编码的流
    if "h264" in play_urls:
        adaptation = play_urls["h264"].get("adaptationSet", {})
    else:
        adaptation = play_urls[0].get("adaptationSet", {}) if isinstance(play_urls, list) else {}

    representation = adaptation.get("representation", [])
    if not representation:
        raise AdapterResolveError(
            "kuaishou_no_play_url",
            "快手没有返回可播放地址",
            status_code=502,
            retryable=True,
        )

    # 取最高码率的流
    flv_url = representation[0].get("url", "")
    if not flv_url:
        raise AdapterResolveError(
            "kuaishou_no_play_url",
            "快手没有返回可播放地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "kuaishou",
        "source_type": "http_flv",
        "url": flv_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": ADAPTER_SUCCESS_TTL_SECONDS,
        "expires_at": None,
        "warnings": [],
        "anchor_name": anchor_name,
    }
