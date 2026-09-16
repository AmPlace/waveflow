import hashlib
import time
from typing import Any

import httpx

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError

HNTV_CHANNELS = {
    # 省台
    "hnws": {"id": 145, "name": "河南卫视"},
    "hnds": {"id": 141, "name": "河南都市"},
    "hnms": {"id": 146, "name": "河南民生"},
    "hmfz": {"id": 147, "name": "河南法治"},
    "hndsj": {"id": 148, "name": "河南电视剧"},
    "hnxw": {"id": 149, "name": "河南新闻"},
    "htgw": {"id": 150, "name": "欢腾购物"},
    "hngg": {"id": 151, "name": "河南公共"},
    "hnxc": {"id": 152, "name": "河南乡村"},
    "hngj": {"id": 153, "name": "河南国际"},
    "hnly": {"id": 154, "name": "河南梨园"},
    "wwbk": {"id": 155, "name": "文物宝库"},
    "wspd": {"id": 156, "name": "武术世界"},
    "jczy": {"id": 157, "name": "睛彩中原"},
    "ydxj": {"id": 163, "name": "移动戏曲"},
    "xsj": {"id": 183, "name": "象视界"},
    "gxpd": {"id": 194, "name": "国学频道"},
    # 地方台
    "zz1": {"id": 197, "name": "郑州新闻综合"},
    "kf1": {"id": 198, "name": "开封新闻综合"},
    "ly1": {"id": 204, "name": "洛阳新闻综合"},
    "pds1": {"id": 205, "name": "平顶山新闻综合"},
    "ay1": {"id": 206, "name": "安阳新闻综合"},
    "hb1": {"id": 207, "name": "鹤壁新闻综合"},
    "xx1": {"id": 208, "name": "新乡新闻综合"},
    "jz1": {"id": 209, "name": "焦作新闻综合"},
    "py1": {"id": 219, "name": "濮阳新闻综合"},
    "xc1": {"id": 220, "name": "许昌新闻综合"},
    "lh1": {"id": 221, "name": "漯河新闻综合"},
    "smx1": {"id": 222, "name": "三门峡新闻综合"},
    "ny1": {"id": 223, "name": "南阳新闻综合"},
    "sq1": {"id": 224, "name": "商丘新闻综合"},
    "xy1": {"id": 225, "name": "信阳新闻综合"},
    "zk1": {"id": 226, "name": "周口新闻综合"},
    "zmd1": {"id": 227, "name": "驻马店新闻综合"},
    "jy1": {"id": 228, "name": "济源新闻综合"},
}

_SIGN_SALT = "6ca114a836ac7d73"
HNTV_API = "https://pubmod.hntv.tv/program/getAuth/channel/channelIds/1"


async def resolve_hntv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    channel = HNTV_CHANNELS.get(channel_key)
    if not channel:
        if channel_key.isdigit():
            cid = int(channel_key)
        else:
            supported = ", ".join(HNTV_CHANNELS)
            raise AdapterResolveError(
                "invalid_hntv_channel_id",
                f"不支持的河南频道 ID: {channel_key}，支持的频道有: {supported}",
            )
    else:
        cid = channel["id"]

    t = str(int(time.time()))
    sign = hashlib.sha256((_SIGN_SALT + t).encode()).hexdigest()

    try:
        resp = await client.get(
            f"{HNTV_API}/{cid}",
            headers={"timestamp": t, "sign": sign},
            follow_redirects=True,
            timeout=15.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "hntv_request_failed",
            f"河南广电接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    try:
        data = resp.json()
    except ValueError:
        raise AdapterResolveError(
            "hntv_parse_failed",
            "河南广电接口返回非 JSON",
            status_code=502,
            retryable=True,
        )

    if not isinstance(data, list) or not data:
        raise AdapterResolveError(
            "hntv_channel_not_found",
            f"河南广电未找到频道: {channel_key}",
            status_code=502,
            retryable=True,
        )

    item = data[0]
    channel_name = item.get("name") or (channel["name"] if channel else str(cid))

    # 优先 video_streams，回退 streams
    play_url = None
    for field in ("video_streams", "streams"):
        urls = item.get(field)
        if isinstance(urls, list) and urls:
            play_url = urls[0]
            break

    if not play_url:
        raise AdapterResolveError(
            "hntv_no_stream",
            f"河南广电频道 {channel_name} 没有返回播放地址",
            status_code=502,
            retryable=True,
        )

    warnings: list[str] = []
    source_type = "hls"
    if play_url.startswith("rtmp"):
        source_type = "rtmp"
        warnings.append("该频道使用 rtmp 协议，部分播放器可能不支持")

    return {
        "ok": True,
        "adapter": "hntv",
        "source_type": source_type,
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": 30 * 60,
        "expires_at": None,
        "warnings": warnings,
        "channel_id": channel_key,
        "channel_name": channel_name,
        "volatile_url": True,
    }
