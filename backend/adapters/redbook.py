import json
import re
from urllib.parse import unquote
from typing import Any

import httpx
from streamget import RedNoteLiveStream
from streamget.requests.async_http import async_req

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError


async def _patched_fetch_app_stream_data(self, url: str, process_data: bool = True) -> dict:
    """修复 streamget 的 RedNoteLiveStream.fetch_app_stream_data，解码 URL 编码的 flvUrl"""
    if "xhslink.com" in url:
        url = await async_req(url, proxy_addr=self.proxy_addr, headers=self.mobile_headers, redirect_url=True)

    host_id = self.get_params(url, "host_id")
    user_id = re.search(r"/user/profile/(.*?)(?=/|\?|$)", url)
    user_id = user_id.group(1) if user_id else host_id
    result = {"anchor_name": '', "is_live": False, "live_url": url}
    html_str = await async_req(url, proxy_addr=self.proxy_addr, headers=self.mobile_headers)
    match_data = re.search(r"<script>window\.__INITIAL_STATE__=(.*?)</script>", html_str)

    if match_data:
        json_str = match_data.group(1).replace("undefined", "null")
        json_data = json.loads(json_str)
        if not process_data:
            return json_data

        if json_data.get("liveStream"):
            stream_data = json_data["liveStream"]
            if stream_data.get("liveStatus") == "success":
                room_info = stream_data["roomData"]["roomInfo"]
                title = room_info.get("roomTitle")
                if title and "回放" not in title:
                    live_link = room_info["deeplink"]
                    anchor_name = self.get_params(live_link, "host_nickname")
                    flv_url = self.get_params(live_link, "flvUrl")

                    # 关键修复：URL 解码
                    flv_url = unquote(flv_url) if flv_url else flv_url

                    if flv_url and 'live/' in flv_url:
                        room_id = flv_url.split('live/')[1].split('.')[0]
                        flv_url = f"http://live-source-play.xhscdn.com/live/{room_id}.flv"
                        m3u8_url = flv_url.replace('.flv', '.m3u8')
                        result |= {
                            "anchor_name": anchor_name,
                            "is_live": True,
                            "title": title,
                            "flv_url": flv_url,
                            "m3u8_url": m3u8_url,
                            'record_url': flv_url
                        }
                        return result

    profile_url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
    html_str = await async_req(profile_url, proxy_addr=self.proxy_addr, headers=self.mobile_headers)
    anchor_name = re.search(r"<title>@(.*?) 的个人主页</title>", html_str)
    if anchor_name:
        result["anchor_name"] = anchor_name.group(1)

    return result


# Monkey-patch: 替换 streamget 的 fetch_app_stream_data
RedNoteLiveStream.fetch_app_stream_data = _patched_fetch_app_stream_data


async def resolve_redbook(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    room_id = request.resource_id.strip("/")
    if not room_id:
        raise AdapterResolveError("invalid_redbook_room_id", "小红书房间号不能为空")

    redbook_url = f"https://www.xiaohongshu.com/livestream/{room_id}"

    try:
        live = RedNoteLiveStream()
        data = await live.fetch_app_stream_data(redbook_url)
        stream_obj = await live.fetch_stream_url(data, "OD")
        json_str = stream_obj.to_json()
        result = json.loads(json_str)
    except Exception as exc:
        raise AdapterResolveError(
            "redbook_resolve_failed",
            f"小红书解析失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not result.get("is_live"):
        raise AdapterResolveError(
            "redbook_not_live",
            "该小红书主播未开播",
            status_code=502,
            retryable=False,
        )

    play_url = result.get("m3u8_url") or result.get("flv_url")
    if not play_url:
        raise AdapterResolveError(
            "redbook_no_play_url",
            "小红书没有返回可播放地址",
            status_code=502,
            retryable=True,
        )

    source_type = "hls" if result.get("m3u8_url") else "http_flv"

    return {
        "ok": True,
        "adapter": "redbook",
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
