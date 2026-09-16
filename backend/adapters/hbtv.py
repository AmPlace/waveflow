import hashlib
import time
from typing import Any

import httpx

from . import ADAPTER_SUCCESS_TTL_SECONDS, AdapterRequest, AdapterResolveError

HBTv_CHANNELS = {
    "hbws": {"id": 10524916, "name": "河北卫视"},
    "hbjjsh": {"id": 10516507, "name": "河北经济生活"},
    "hbsn": {"id": 10516508, "name": "河北三农频道"},
    "hbds": {"id": 10516509, "name": "河北都市"},
    "hbysj": {"id": 10516510, "name": "河北影视剧"},
    "hbse": {"id": 10516511, "name": "河北少儿科教"},
    "hbwl": {"id": 10516512, "name": "河北文旅·公共"},
    "hbgw": {"id": 10516513, "name": "河北三佳购物"},
}

HBTv_LIST_URL = "https://api.cmc.hebtv.com/scms/api/com/article/getArticleList"
HBTv_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://web.cmc.hebrts.cn/",
}


async def resolve_hbtv(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    channel = HBTv_CHANNELS.get(channel_key)
    if not channel:
        # 支持直接传数字 id
        if channel_key.isdigit():
            target_id = int(channel_key)
        else:
            supported = ", ".join(HBTv_CHANNELS)
            raise AdapterResolveError(
                "invalid_hbtv_channel_id",
                f"不支持的河北频道 ID: {channel_key}，支持的频道有: {supported}",
            )
    else:
        target_id = channel["id"]

    # 请求频道列表
    try:
        resp = await client.get(
            HBTv_LIST_URL,
            params={"catalogId": "32557", "siteId": "1"},
            headers=HBTv_HEADERS,
            follow_redirects=True,
            timeout=15.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError(
            "hbtv_request_failed",
            f"河北广电接口请求失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    try:
        data = resp.json()
    except ValueError:
        raise AdapterResolveError(
            "hbtv_parse_failed",
            "河北广电接口返回非 JSON",
            status_code=502,
            retryable=True,
        )

    news = (data.get("returnData") or {}).get("news")
    if not isinstance(news, list):
        raise AdapterResolveError(
            "hbtv_parse_failed",
            "河北广电返回结构异常",
            status_code=502,
            retryable=True,
        )

    # 按 id 查找频道
    target = None
    for n in news:
        if n.get("id") == target_id:
            target = n
            break

    if not target:
        raise AdapterResolveError(
            "hbtv_channel_not_found",
            f"河北广电未找到频道 ID: {target_id}",
            status_code=502,
            retryable=True,
        )

    channel_name = target.get("title") or (channel["name"] if channel else str(target_id))

    # 提取播放基础地址
    live_video = target.get("liveVideo")
    if not live_video or not live_video[0].get("formats"):
        raise AdapterResolveError(
            "hbtv_no_live_url",
            "河北广电没有返回直播地址",
            status_code=502,
            retryable=True,
        )

    base_url = live_video[0]["formats"][0].get("url")
    if not base_url:
        raise AdapterResolveError(
            "hbtv_no_live_url",
            "河北广电直播地址为空",
            status_code=502,
            retryable=True,
        )

    # 提取签名参数
    movie = (target.get("appCustomParams") or {}).get("movie") or {}
    live_uri = movie.get("liveUri")
    live_key = movie.get("liveKey")

    if not live_uri or not live_key:
        raise AdapterResolveError(
            "hbtv_no_sign_params",
            "河北广电缺少签名参数 liveUri/liveKey",
            status_code=502,
            retryable=True,
        )

    # 生成签名
    t = int(time.time()) + 7200
    k = hashlib.md5((live_uri + live_key + str(t)).encode()).hexdigest()
    play_url = f"{base_url}?t={t}&k={k}"

    return {
        "ok": True,
        "adapter": "hbtv",
        "source_type": "hls",
        "url": play_url,
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {},
        "ttl": 60 * 60,  # 签名有效期 2 小时，缓存 1 小时
        "expires_at": None,
        "warnings": [],
        "channel_id": channel_key,
        "channel_name": channel_name,
        "volatile_url": True,
    }
