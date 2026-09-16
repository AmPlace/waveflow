import base64
import hashlib
import time
from typing import Any

import httpx

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError

_JSTV_SIGN_KEY = base64.b64decode("dEphbkFIa3lHdGFpZmFRRzRkV2U=").decode()
_JSTV_ENCRYPTED_BASE = "https://litchi-play-encrypted-site.jstv.com"

_JSTV_ENCRYPTED_PATHS: dict[str, str] = {
    "jsws":        "/applive/jswspro.m3u8",
    "jsws4k":      "/4klive/jsws4kpro.m3u8",
    "jscs":        "/applive/jscspro.m3u8",
    "jszy":        "/applive/jszypro.m3u8",
    "jsys":        "/applive/jsyspro.m3u8",
    "jsxw":        "/applive/jsxwpro.m3u8",
    "jsjy":        "/applive/jsjypro.m3u8",
    "jsxx":        "/applive/jsxxpro.m3u8",
    "ymkt":        "/applive/ymktpro.m3u8",
    "jsgj":        "/applive/jsgjpro.m3u8",
    "cftx":        "/live/cftxtxjm.m3u8",
    "nanjing":     "/live/nanjing.m3u8",
    "luhe":        "/live/luhe.m3u8",
    "wuxi":        "/live/wuxi.m3u8",
    "xuzhou":      "/live/xuzhou.m3u8",
    "pizhou":      "/live/pizhou.m3u8",
    "xinyi":       "/live/xinyi.m3u8",
    "jiawang":     "/live/jiawang.m3u8",
    "tongshan":    "/live/tongshan.m3u8",
    "changzhou":   "/applive/czpro.m3u8",
    "wujin":       "/live/wujin.m3u8",
    "nantong":     "/live/nantong.m3u8",
    "lianyungang": "/live/lianyungang.m3u8",
    "donghai":     "/live/donghai.m3u8",
    "huaian":      "/live/huaian.m3u8",
    "xuyi":        "/live/xuyi.m3u8",
    "hongze":      "/live/hongze.m3u8",
    "yancheng":    "/live/yancheng.m3u8",
    "xiangshui":   "/live/xiangshui.m3u8",
    "zhenjiang":   "/live/zhenjiang.m3u8",
    "jurong":      "/live/jurong.m3u8",
    "taizhou":     "/live/taizhou.m3u8",
    "taixing":     "/live/taixing.m3u8",
    "xinghua":     "/live/xinghua.m3u8",
    "jingjiang":   "/live/jingjiang.m3u8",
    "suqian":      "/live/suqian.m3u8",
    "siyang":      "/live/siyang.m3u8",
}


def _sign_url(path: str) -> str:
    stream_name = path.split("/")[-1].split(".")[0]
    ts = int(time.time()) + 180
    ts_hex = format(ts, "x")
    sign_str = f"{_JSTV_SIGN_KEY}{stream_name}{ts_hex}"
    tx_secret = hashlib.md5(sign_str.encode()).hexdigest()
    return f"{_JSTV_ENCRYPTED_BASE}{path}?txSecret={tx_secret}&txTime={ts_hex}"


async def resolve_jstv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()

    path = _JSTV_ENCRYPTED_PATHS.get(channel_key)
    if path:
        play_url = _sign_url(path)
    elif channel_key.startswith("http"):
        play_url = channel_key
    else:
        raise AdapterResolveError(
            "jstv_channel_not_found",
            f"未知的 JSTV 频道: {channel_key}",
        )

    return {
        "ok": True,
        "adapter": "jstv",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {"Referer": "https://live.jstv.com/"},
        "ttl": 3 * 60,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": channel_key,
        "volatile_url": True,
    }
