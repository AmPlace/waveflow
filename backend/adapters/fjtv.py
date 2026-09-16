from typing import Any

import httpx

from . import AdapterRequest, AdapterResolveError


# 福建广电系
# 实测：live.fjtv.net / mapi-plus.fjtv.net / mapi1.kxm.xmtv.cn 这些上游
# 不接受任何 Mozilla 系 UA（无论桌面 / 移动），桌面 Chrome UA 直接 403。
# okhttp / 默认 curl 反而能 200。这里统一用安卓 okhttp UA。
_USER_AGENT = "okhttp/3.10.0.7"

_FJTV_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": _USER_AGENT,
}

# 各上游对应播放 CDN 的 Referer。token 之外，CDN 还会检查 Referer 头。
# 实测：
#   - KXM 厦门系（live*.kxm.xmtv.cn）必须带 https://www.xmtv.cn/
#   - 福建大米 / 海博列表 + live.fjtv.net 系（默认）暂用 https://www.fjtv.net/
#   - 晋江 ijjnews.com / 石狮 chinashishi.net 暂留各自门户
_REFERER_XMTV = "https://www.xmtv.cn/"
_REFERER_FJTV = "https://www.fjtv.net/"
_REFERER_JJ = "https://www.ijjnews.com/"
_REFERER_SS = "https://www.chinashishi.net/"

_MAPI_PLUS_LIST_URL = (
    "https://mapi-plus.fjtv.net/api/open/haibo8/tv_channel_list.php?sort_id=665226484646215680"
)
_KXM_LIST_URL = (
    "https://mapi1.kxm.xmtv.cn/api/v1/channel.php"
    "?node_id=1&appkey=45920796f66247395069ee6f45d99c5e&appid=m2ohvecbng7leb8ixo"
    "&client_type=iOS&device_token=d25800aafc08fc01eabdf4757762e03c&version=4.6.4"
    "&app_version=4.6.4&avos_device_token=d25800aafc08fc01eabdf4757762e03c"
    "&client_id_ios=1211f05806bca2f5e7d93bc0d1d6f75d"
    "&location_city=%E5%8E%A6%E9%97%A8&language=Chinese"
)

_HLS_LEAF: list[Any] = ["topic_camera", 0, "streams", 0, "hls"]
_M3U8_LEAF: list[Any] = [0, "m3u8"]


def _channel_info(channel_id: str) -> str:
    return f"https://live.fjtv.net/m2o/channel/channel_info.php?channel_id={channel_id}"


FJTV_CHANNELS: dict[str, dict[str, Any]] = {
    # ---- live.fjtv.net m2o channel_info ----
    "fjzh":      {"name": "福建综合",     "url": _channel_info("665248990102917120"), "path": _M3U8_LEAF, "referer": _REFERER_FJTV},
    "fjdn":      {"name": "东南卫视",     "url": _channel_info("665248966136664064"), "path": _M3U8_LEAF, "referer": _REFERER_FJTV},
    "fjnews":    {"name": "福建新闻",     "url": _channel_info("665248914378952704"), "path": _M3U8_LEAF, "referer": _REFERER_FJTV},
    "fjculture": {"name": "福建文旅体育", "url": _channel_info("665248752898248704"), "path": _M3U8_LEAF, "referer": _REFERER_FJTV},
    "fjkid":     {"name": "福建少儿",     "url": _channel_info("665248553475870720"), "path": _M3U8_LEAF, "referer": _REFERER_FJTV},
    "fjhxws":    {"name": "海峡卫视",     "url": _channel_info("665248523855695872"), "path": _M3U8_LEAF, "referer": _REFERER_FJTV},

    # ---- mapi-plus.fjtv.net haibo8 列表（按下标取） ----
    "xmws":      {"name": "厦门卫视",     "url": _MAPI_PLUS_LIST_URL, "path": [0, *_HLS_LEAF], "referer": _REFERER_FJTV},
    "fznews":    {"name": "福州新闻综合", "url": _MAPI_PLUS_LIST_URL, "path": [1, *_HLS_LEAF], "referer": _REFERER_FJTV},
    "zznews":    {"name": "漳州新闻综合", "url": _MAPI_PLUS_LIST_URL, "path": [2, *_HLS_LEAF], "referer": _REFERER_FJTV},
    "smtv":      {"name": "三明综合",     "url": _MAPI_PLUS_LIST_URL, "path": [3, *_HLS_LEAF], "referer": _REFERER_FJTV},
    "qznews":    {"name": "泉州新闻综合", "url": _MAPI_PLUS_LIST_URL, "path": [4, *_HLS_LEAF], "referer": _REFERER_FJTV},
    "nptv":      {"name": "南平综合",     "url": _MAPI_PLUS_LIST_URL, "path": [5, *_HLS_LEAF], "referer": _REFERER_FJTV},
    "lytv":      {"name": "龙岩综合",     "url": _MAPI_PLUS_LIST_URL, "path": [6, *_HLS_LEAF], "referer": _REFERER_FJTV},
    "puttv":     {"name": "莆田新闻综合", "url": _MAPI_PLUS_LIST_URL, "path": [7, *_HLS_LEAF], "referer": _REFERER_FJTV},
    "pttv":      {"name": "平潭综合",     "url": _MAPI_PLUS_LIST_URL, "path": [8, *_HLS_LEAF], "referer": _REFERER_FJTV},
    "ndtv":      {"name": "宁德新闻综合", "url": _MAPI_PLUS_LIST_URL, "path": [9, *_HLS_LEAF], "referer": _REFERER_FJTV},

    # ---- mapi1.kxm.xmtv.cn 厦门系 ----
    "xmws-xmtv":   {"name": "厦门卫视(XMTV)",     "url": _KXM_LIST_URL, "path": [0, "m3u8"], "referer": _REFERER_XMTV},
    "xmtv-1":      {"name": "厦视一套",           "url": _KXM_LIST_URL, "path": [1, "m3u8"], "referer": _REFERER_XMTV},
    "xmtv-2":      {"name": "厦视二套",           "url": _KXM_LIST_URL, "path": [2, "m3u8"], "referer": _REFERER_XMTV},
    "xmtv-mobile": {"name": "厦门电视台移动电视", "url": _KXM_LIST_URL, "path": [3, "m3u8"], "referer": _REFERER_XMTV},

    # ---- 晋江、石狮 ----
    "jjtv": {
        "name": "晋江综合",
        "url": (
            "https://mapi.ijjnews.com/cloudlive-manage-mapi/api/topic/detail"
            "?preview=&id=657527900022525952&app_secret=31ca2c44a23e6cd127ddee647fa9cf92"
            "&tenant_id=0&company_id=1067&lang_type=zh"
        ),
        "path": _HLS_LEAF,
        "referer": _REFERER_JJ,
    },
    "sstv": {
        "name": "石狮新闻综合",
        "url": (
            "https://mapi-new.chinashishi.net/cloudlive-manage-mapi/api/topic/detail"
            "?preview=&id=662611405685436416&app_secret=5c03f9843fa239c14b52222e83098919"
            "&tenant_id=0&company_id=492&lang_type=zh"
        ),
        "path": _HLS_LEAF,
        "referer": _REFERER_SS,
    },
}

# 兼容旧 resolver 的 station id（仅保留已知出现过的，未来需要再加）
_FJTV_ALIASES: dict[str, str] = {
    "fjzhpd": "fjzh",
    "fjdnws": "fjdn",
}


def _walk(data: Any, path: list[Any]) -> Any:
    cur: Any = data
    for key in path:
        if isinstance(key, int):
            if not isinstance(cur, list) or key >= len(cur):
                raise KeyError(f"list index {key} out of range")
            cur = cur[key]
        else:
            if not isinstance(cur, dict) or key not in cur:
                raise KeyError(f"missing key {key!r}")
            cur = cur[key]
    return cur


async def resolve_fjtv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    channel_key = _FJTV_ALIASES.get(channel_key, channel_key)
    entry = FJTV_CHANNELS.get(channel_key)
    if not entry:
        supported = ", ".join(sorted(FJTV_CHANNELS))
        raise AdapterResolveError(
            "invalid_fjtv_channel_id",
            f"不支持的福建频道: {channel_key}，支持的频道有: {supported}",
        )

    api_url: str = entry["url"]
    path: list[Any] = entry["path"]

    try:
        resp = await client.get(
            api_url,
            headers=_FJTV_HEADERS,
            timeout=12.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "fjtv_request_failed",
            f"福建广电接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise AdapterResolveError(
            "fjtv_parse_failed",
            "福建广电接口返回非 JSON",
            status_code=502,
            retryable=True,
        ) from exc

    if isinstance(data, dict) and data.get("error_code", 0) not in (0, None):
        raise AdapterResolveError(
            "fjtv_business_error",
            f"福建广电业务报错: {data!r}",
            status_code=502,
            retryable=True,
        )

    try:
        play_url = _walk(data, path)
    except (KeyError, IndexError, TypeError) as exc:
        raise AdapterResolveError(
            "fjtv_parse_failed",
            f"福建广电频道 {entry['name']} 接口结构异常: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    if not isinstance(play_url, str) or not play_url.startswith(("http://", "https://")):
        raise AdapterResolveError(
            "fjtv_no_stream",
            f"福建广电频道 {entry['name']} 没有返回有效播放地址",
            status_code=502,
            retryable=True,
        )

    return {
        "ok": True,
        "adapter": "fjtv",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {"Referer": entry["referer"]} if entry.get("referer") else {},
        "ttl": 3 * 60,
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": entry["name"],
        "volatile_url": True,
    }
