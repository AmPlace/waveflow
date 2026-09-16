import time
from typing import Any

import httpx

from . import AdapterRequest, AdapterResolveError

# ================= TVB adapter — 2026-08-09 修正 =================
# 修正依据：agent_hk1_tvb.md（HK IP 实测）
#   ① 旧固定 IP 34.117.164.167 证书失效 → 直连 https://inews-api.tvb.com（无需 Host override）
#   ② news 走 Edgeware 链路：302 session URL token 在 query，**无需 hdntl cookie / no_ua**（旧 Akamai cookie 逻辑删掉）
#   ③ C2/I-NEWS/C/C3 都是「無線新聞台 Channel 83」的冗余 Edgeware 编码实例（同 seq 同时间线证实）
#   ④ 主台（Jade/TVB Plus/Pearl）走 myTV SUPER checkout → Widevine CENC DASH（DRM，需 DRM 播放器）
#      84 Pearl 额外地理墙：需 HK residential ISP IP（数据中心 IP 也被拒 S_00001001）

TVB_NEWS_CHANNELS = {
    "I-NEWS": "I-NEWS",   # 無線新聞台 83（AES-128，匿名）
    "C": "C",             # 無線新聞台 83
    "C2": "C2",           # 無線新聞台 83（冗余实例）
    "C3": "C3",           # 無線新聞台 83（冗余实例）
}

# myTV SUPER 主台：network_code → checkout（内容 ott_<code>_h264，Widevine CENC，免费但 DRM）
MYTV_CHANNELS = {
    "JADE":   {"network": "J", "name": "翡翠台 (81)"},
    "TVBPLUS": {"network": "B", "name": "TVB Plus (82)"},
    "PEARL":  {"network": "P", "name": "明珠台 (84)"},
}
MYTV_ALIAS = {"81": "JADE", "82": "TVBPLUS", "84": "PEARL", "J": "JADE", "B": "TVBPLUS", "P": "PEARL"}
MYTV_API = "https://user-api.mytvsuper.com/v1/channel/checkout"
# myTV SUPER web bundle 的 getGuestToken()，匿名共享凭证（RSA 签名被接受，尽管 exp=2022）
MYTV_GUEST_TOKEN = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJib3NzX2lkIjoiMDAwMDAwMDAxIiwiZGV2aWNlX3Rva2VuIjoiQ3ZmTUNzVTh4UGlpYmtDUUVrSzM5NUpnIiwiZGV2aWNlX2lkIjoiMCIsImRldmljZV90eXBlIjoid2ViIiwiZGV2aWNlX29zIjoiYnJvd3NlciIsImRybV9pZCI6bnVsbCwiZXh0cmEiOnsicHJvZmlsZV9pZCI6MX0sImlhdCI6MTY0NjI5MzQxNCwiZXhwIjoxNjQ2Mjk3MDE0fQ.t5qYMiV4RJkAZ9FfmmJtigpzNca0P5ZnI4AEXU61HWVIJd5cIUQlNufOJbN4R3MPJxs7msOVBdosIMaIhF49so_ubufqSNDDK9s3qZRpAUaHvRtiXQWCuuL3Am07IwaR6vO-yNFpNtnhTWp7V-5KkmJjmjgwtbQlwK5FU424Ef9iFu64aeounen8o5cuBuql5nRl6mFOX7QMx3Cr0XmLyJBRsuuoXlivaGzNchqT4rkmck0SUqeeBSzcpoDdFry4SXZO9I_CIK75bOX4Icw5p8ZFwAzYvE5xhTpAEdRUKMPSDMRD9Vak-WKPWhQBeV8X5LJONhaofMaq0j0HC5sM6arPQR6x2r5y5IPZwVOcUaYqJVlgXOAP72iFwCkZBm30qJV9p5eLSNWizpVUbYIEiwjcqBQ9ZZR2jqszzSEZpsTO1kwQ3jIViewwFJjffBljrp5ZsRDj-vXrdZ-tXVY4ecsgrjUXJJEEMKMCBVFLzuu5is6Hgdr8BUdm8QAPQqvvkqu7W0Gt-2YAgcU4eEG2wzx1485wxNxLgXXG10SwzH12OHxqoMl3_KP22JN9JgP6uS1Br4yLFqo-v3Z-UOAo3x_yfivgcW34uI4VHSF1JiQfJinsSWeHOGPJrDSDvrCNLZbFonX2xaWVOQ3Uf8hXum55xNufLM8Trt4Ga8CBZMY"
)

# 缓存: news 解析后的 Edgeware session URL
_tvb_news_cache: dict[str, Any] = {
    "resolved_url": "",
    "signed_url": "",
    "expires_at": 0,
}


async def _resolve_news(client: httpx.AsyncClient, content_id: str, channel_key: str) -> dict[str, Any]:
    """Channel 83 無線新聞台：inews-api checkout → AES-128 HLS（匿名，非 DRM）。"""
    api_url = f"https://inews-api.tvb.com/news/checkout/live/hd/ott_{content_id}_h264?profile=safari"
    try:
        resp = await client.get(api_url, follow_redirects=True, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError("tvb_api_failed", f"TVB news API 失败: {exc}", status_code=502, retryable=True) from exc

    try:
        data = resp.json()
    except ValueError:
        raise AdapterResolveError("tvb_parse_failed", "TVB news API 返回非 JSON", status_code=502, retryable=True)

    if data.get("meta", {}).get("status") != "success":
        raise AdapterResolveError(
            "tvb_channel_failed",
            f"TVB news {content_id} 失败: {data.get('meta', {}).get('error_message', '未知')}",
            status_code=502,
            retryable=True,
        )

    urls = (data.get("content") or {}).get("url") or {}
    signed_url = urls.get("hd") or urls.get("sd")
    if not signed_url:
        raise AdapterResolveError("tvb_no_url", "TVB news 未返回播放地址", status_code=502, retryable=True)

    # 跟随 302 拿 Edgeware session URL（token 在 query，无需 cookie/UA；缓存 ~30min）
    now = time.time()
    if now >= _tvb_news_cache["expires_at"] or signed_url != _tvb_news_cache["signed_url"]:
        try:
            sresp = await client.get(signed_url, follow_redirects=True, timeout=10)
            resolved = str(sresp.url) if sresp.status_code == 200 else signed_url
        except Exception:
            resolved = signed_url
        _tvb_news_cache["resolved_url"] = resolved
        _tvb_news_cache["signed_url"] = signed_url
        _tvb_news_cache["expires_at"] = now + 30 * 60

    return {
        "ok": True,
        "adapter": "tvb",
        "source_type": "hls",
        "url": _tvb_news_cache["resolved_url"],
        "direct_playable": False,
        "requires_proxy": True,  # key 主机 ads.cdn.tvb.com 需 HK ISP IP
        "headers": {},
        "ttl": 30 * 60,
        "expires_at": None,
        "warnings": ["AES-128 加密；解密 key 主机需 HK ISP IP"],
        "channel_id": channel_key,
        "channel_name": f"無線新聞台 {content_id} (Channel 83)",
        "volatile_url": True,
    }


async def _resolve_mytv(client: httpx.AsyncClient, ch: dict[str, str], channel_key: str) -> dict[str, Any]:
    """myTV SUPER 主台：checkout → Edgeware DASH MPD（Widevine CENC，DRM）。"""
    headers = {"Authorization": f"Bearer {MYTV_GUEST_TOKEN}"}
    params = {"platform": "web", "country_code": "HK", "network_code": ch["network"]}
    try:
        resp = await client.get(MYTV_API, params=params, headers=headers, follow_redirects=True, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AdapterResolveError("tvb_mytv_api_failed", f"myTV checkout 失败: {exc}", status_code=502, retryable=True) from exc

    data = resp.json()
    if data.get("error_code") or not data.get("profiles"):
        raise AdapterResolveError(
            "tvb_mytv_failed",
            f"myTV {ch['name']} checkout 失败: {data.get('error_message', data.get('error_code', '未知'))}",
            status_code=502,
            retryable=True,
        )

    streaming_path = (data.get("profiles") or [{}])[0].get("streaming_path")
    if not streaming_path:
        raise AdapterResolveError("tvb_mytv_no_url", f"myTV {ch['name']} 无播放地址", status_code=502, retryable=True)

    warnings = ["Widevine CENC DRM——需 DRM 播放器（inputstream.adaptive / Widevine 内核）"]
    if ch["network"] == "P":
        warnings.append("明珠台地理墙：需 HK residential ISP IP（数据中心 IP 也被拒）")

    return {
        "ok": True,
        "adapter": "tvb",
        "source_type": "dash",
        "url": streaming_path,
        "direct_playable": False,
        "requires_proxy": True,
        "headers": {},
        "ttl": 5 * 60,
        "expires_at": None,
        "warnings": warnings,
        "channel_id": channel_key,
        "channel_name": ch["name"],
        "volatile_url": True,
    }


async def resolve_tvb(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    key = request.resource_id.strip("/").upper()

    # news 系（無線新聞台 83）
    if key in TVB_NEWS_CHANNELS:
        return await _resolve_news(client, TVB_NEWS_CHANNELS[key], key)

    # myTV 主台（Jade/TVB Plus/Pearl）
    if key in MYTV_CHANNELS:
        return await _resolve_mytv(client, MYTV_CHANNELS[key], key)
    if key in MYTV_ALIAS:
        canonical = MYTV_ALIAS[key]
        return await _resolve_mytv(client, MYTV_CHANNELS[canonical], key)

    supported = ", ".join(list(TVB_NEWS_CHANNELS) + list(MYTV_CHANNELS))
    raise AdapterResolveError(
        "invalid_tvb_channel",
        f"不支持的 TVB 频道: {key}，支持: {supported}",
    )
