import asyncio
import hashlib
import logging
import os
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from . import AdapterRequest, AdapterResolveError

logger = logging.getLogger("waveflow.adapters.woniu")

# 蜗牛视频 / 湖南有线 直播 token 化接口。
#
# 相对其他 adapter 的特殊点：
#   - 接口走 119.44.12.12 直连，Host 头必须写 liveepg.hunancatv.com。
#   - vf 签名：md5(path + "?" + query + "&qd_v=1ph" + salt)。
#   - live_url 偶尔返回 ?project=WNTV 占位 URL，要重试到 12 次才稳。
#   - URL 自带 timestamp= 或 e=YYYYMMDDhhmmss 过期时间，对应 adapter 的 ttl/expires_at。
#
# token / 设备号默认走 env，env 缺省则退回内置示例值（开发用，正式部署应配 env）。

_QD_V = "1ph"
_SALT = os.getenv("WONIU_SALT", "d7npemiwtm0wiogrbw84kmbkl6d5ih38")
_CONNECT_IP = os.getenv("WONIU_CONNECT_IP", "119.44.12.12")
_HOST_HEADER = os.getenv("WONIU_HOST_HEADER", "liveepg.hunancatv.com")
_PATH_GET_PLAY_URLS = "/v1/getPlayUrls"

_PARTNER = os.getenv("WONIU_PARTNER", "3")
_CITYCODE = os.getenv("WONIU_CITYCODE", "")
_ADCODE = os.getenv("WONIU_ADCODE", "")
_SRC = os.getenv("WONIU_SRC", "05025022911000000000")

_COOKIE = os.getenv(
    "WONIU_COOKIE",
    "P00001=a1ykQ1ccO5UMAh75qkPjyXc4zu4HCR6r9Q7nuE6ctRu6kLbsx1yaVRJt6zTm12PuCE5d3",
)
_AUTHORIZATION = os.getenv(
    "WONIU_AUTHORIZATION",
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiI5ODQwODlhNjc1OGU0ZjJlOTViMjk4NWM4YjA1MDNmYiIsImNvbXBhbnkiOiJxaXlpIiwibmFtZSI6InRlcm1pbmFsIn0."
    "1gDPpBcHJIE8dLiq7UekUlPWMtJOYymI8zoIYlsVgc4",
)
_DEVICE_ID = os.getenv("WONIU_DEVICE_ID", "tv_ad4815165f316f13739780350c446070_1763386468635_1392465788")
_OPEN_TOKEN = os.getenv(
    "WONIU_OPEN_TOKEN",
    "LGmjLEn7m3X30qQqOxiWvcyadZeOq3JMiEgj77COsbeO/gX6F6lN1NjUgZxd9zNE",
)
_USER_ID = os.getenv("WONIU_USER_ID", "HNTV00QS00016155")
_USER_MAC = os.getenv("WONIU_USER_MAC", "00:00:00:00:00:00")
_VERSION = os.getenv("WONIU_VERSION", "8.3")
_USER_AGENT = os.getenv("WONIU_USER_AGENT", "okhttp/3.10.0.7")

_ATTEMPTS = int(os.getenv("WONIU_ATTEMPTS", "12"))
_RETRY_SLEEP = float(os.getenv("WONIU_RETRY_SLEEP", "0.1"))
_TIMEOUT = float(os.getenv("WONIU_TIMEOUT", "8.0"))
_TTL_SKEW = int(os.getenv("WONIU_TTL_SKEW", "600"))  # 提前 10 分钟过期，避免临界状态


# 频道映射表（80+）。
# key 是 WaveFlow adapter 的 channel_key（M3U 里直接写数字 channel_id 也可以，
# resolve_woniu 找不到 key 时会把纯数字当成 channel_id 直接用）。
# value: {"name": str, "channel_id": str}
WONIU_CHANNELS: dict[str, dict[str, str]] = {
    # 湖南本地高清
    "hnws4k": {"name": "湖南卫视4K",   "channel_id": "5216"},
    "hnjs":   {"name": "湖南经视高清", "channel_id": "5377"},
    "hnyl":   {"name": "湖南娱乐高清", "channel_id": "5391"},
    "hndy":   {"name": "湖南电影高清", "channel_id": "5405"},
    "hnds":   {"name": "湖南都市高清", "channel_id": "5398"},
    "hndsj":  {"name": "湖南电视剧高清", "channel_id": "5412"},
    "klg":    {"name": "快乐购高清",   "channel_id": "5426"},
    "jyjs":   {"name": "金鹰纪实高清", "channel_id": "5384"},
    "hnaw":   {"name": "湖南爱晚高清", "channel_id": "5419"},
    "jykt":   {"name": "金鹰卡通高清", "channel_id": "5349"},
    "guide":  {"name": "导视频道",     "channel_id": "5370"},
    "hnjy":   {"name": "湖南教育频道", "channel_id": "5356"},
    # 央视
    "cctv1":     {"name": "CCTV-1高清",  "channel_id": "5104"},
    "cctv2":     {"name": "CCTV-2高清",  "channel_id": "5111"},
    "cctv3":     {"name": "CCTV-3高清",  "channel_id": "5118"},
    "cctv4":     {"name": "CCTV-4高清",  "channel_id": "5125"},
    "cctv5":     {"name": "CCTV-5高清",  "channel_id": "5139"},
    "cctv6":     {"name": "CCTV-6高清",  "channel_id": "5146"},
    "cctv7":     {"name": "CCTV-7高清",  "channel_id": "5153"},
    "cctv8":     {"name": "CCTV-8高清",  "channel_id": "5160"},
    "cctv9":     {"name": "CCTV-9高清",  "channel_id": "5167"},
    "cctv10":    {"name": "CCTV-10高清", "channel_id": "5174"},
    "cctv11":    {"name": "CCTV-11戏曲", "channel_id": "5517"},
    "cctv12":    {"name": "CCTV-12高清", "channel_id": "5538"},
    "cctv13":    {"name": "CCTV-13新闻", "channel_id": "5524"},
    "cctv14":    {"name": "CCTV-14高清", "channel_id": "5545"},
    "cctv15":    {"name": "CCTV-15音乐", "channel_id": "5531"},
    "cctv17":    {"name": "CCTV-17高清", "channel_id": "5552"},
    "cctv4k":    {"name": "CCTV-4K",     "channel_id": "6834"},
    "cctv5plus": {"name": "CCTV-5+高清", "channel_id": "5132"},
    # 卫视
    "jsws":       {"name": "江苏卫视高清",   "channel_id": "5244"},
    "zjws":       {"name": "浙江卫视高清",   "channel_id": "5237"},
    "dfws":       {"name": "东方卫视高清",   "channel_id": "5258"},
    "bjws":       {"name": "北京卫视高清",   "channel_id": "5223"},
    "gdws":       {"name": "广东卫视高清",   "channel_id": "5251"},
    "szws":       {"name": "深圳卫视高清",   "channel_id": "5230"},
    "tjws":       {"name": "天津卫视高清",   "channel_id": "5300"},
    "hbws":       {"name": "湖北卫视高清",   "channel_id": "5265"},
    "ahws":       {"name": "安徽卫视高清",   "channel_id": "5272"},
    "cqws":       {"name": "重庆卫视高清",   "channel_id": "5307"},
    "hljws":      {"name": "黑龙江卫视高清", "channel_id": "5293"},
    "lnws":       {"name": "辽宁卫视高清",   "channel_id": "5279"},
    "gxws":       {"name": "广西卫视高清",   "channel_id": "5209"},
    "qhws":       {"name": "青海卫视高清",   "channel_id": "5601"},
    "shanxi-ws":  {"name": "山西卫视高清",   "channel_id": "5559"},
    "nmgws":      {"name": "内蒙古卫视高清", "channel_id": "5608"},
    "nxws":       {"name": "宁夏卫视高清",   "channel_id": "5573"},
    "ynws":       {"name": "云南卫视高清",   "channel_id": "5181"},
    "xzws":       {"name": "西藏卫视高清",   "channel_id": "5195"},
    "shaanxi-ws": {"name": "陕西卫视高清",   "channel_id": "5580"},
    "xjws":       {"name": "新疆卫视高清",   "channel_id": "5202"},
    "btws":       {"name": "兵团卫视高清",   "channel_id": "5861"},
    # 教育/少儿/CGTN/红星
    "kaku":   {"name": "卡酷动画",  "channel_id": "5882"},
    "cetv1":  {"name": "CETV-1",    "channel_id": "5896"},
    "cetv4":  {"name": "CETV-4",    "channel_id": "6827"},
    "hxdj":   {"name": "红星党建",  "channel_id": "6778"},
    "cgtn":   {"name": "CGTN",      "channel_id": "6841"},
    "cgtn-a": {"name": "CGTN-A",    "channel_id": "6848"},
    "cgtn-r": {"name": "CGTN-R",    "channel_id": "6855"},
    "cgtn-e": {"name": "CGTN-E",    "channel_id": "6862"},
    "cgtn-f": {"name": "CGTN-F",    "channel_id": "6869"},
    # 专业频道
    "klcd":       {"name": "快乐垂钓高清", "channel_id": "5685"},
    "shdy":       {"name": "四海钓鱼高清", "channel_id": "5454"},
    "tea":        {"name": "茶频道高清",   "channel_id": "5692"},
    "xfpy":       {"name": "先锋乒羽",     "channel_id": "5657"},
    "shss":       {"name": "生活时尚高清", "channel_id": "5503"},
    "jbty":       {"name": "劲爆体育高清", "channel_id": "5468"},
    "dcwt4k":     {"name": "多彩文体4K",   "channel_id": "5475"},
    "chc-hd":     {"name": "CHC高清电影",   "channel_id": "5664"},
    "yxfy":       {"name": "游戏风云高清", "channel_id": "5489"},
    "tywq":       {"name": "天元围棋高清", "channel_id": "5433"},
    "dmxc":       {"name": "动漫秀场高清", "channel_id": "5671"},
    "shpd":       {"name": "书画频道",     "channel_id": "5636"},
    "leyou":      {"name": "乐游高清",     "channel_id": "5482"},
    "zhtc":       {"name": "中华特产",     "channel_id": "6876"},
    "fztd":       {"name": "法治天地高清", "channel_id": "5678"},
    "sypd":       {"name": "摄影频道",     "channel_id": "5440"},
    "cftx":       {"name": "财富天下",     "channel_id": "5461"},
    "qsjl":       {"name": "求索记录高清", "channel_id": "6932"},
    "dsjc":       {"name": "都市剧场高清", "channel_id": "5847"},
    "qssh":       {"name": "求索生活高清", "channel_id": "6939"},
    "qskx":       {"name": "求索科学高清", "channel_id": "6953"},
    "chc-action": {"name": "CHC动作电影高清", "channel_id": "6911"},
    "chc-family": {"name": "CHC家庭影院高清", "channel_id": "6918"},
}


def _dynamic_k_uid(seed: str, millis: int) -> str:
    """device_id 倒数第二段是毫秒时间戳，每次请求要刷新。"""
    parts = seed.split("_")
    if len(parts) >= 3 and parts[-2].isdigit():
        parts[-2] = str(millis)
    return "_".join(parts)


def _build_query_params(channel_id: str) -> list[tuple[str, str]]:
    now = time.time()
    tm = str(int(now))
    millis = int(now * 1000)
    return [
        ("channel", channel_id),
        ("program", ""),
        ("partner", _PARTNER),
        ("citycode", _CITYCODE),
        ("adcode", _ADCODE),
        ("delay", ""),
        ("definition", ""),
        ("tm", tm),
        ("src", _SRC),
        ("k_uid", _dynamic_k_uid(_DEVICE_ID, millis)),
    ]


def _build_signed_path(path: str, params: list[tuple[str, str]]) -> str:
    query = urlencode(params, doseq=False, quote_via=quote)
    sign_text = f"{path}?{query}"
    vf_text = f"{sign_text}&qd_v={_QD_V}{_SALT}"
    vf = hashlib.md5(vf_text.encode()).hexdigest()
    return f"{sign_text}&qd_v={quote(_QD_V)}&vf={vf}"


def _build_headers() -> dict[str, str]:
    return {
        "Cookie": _COOKIE,
        "Authorization": _AUTHORIZATION,
        "X-Device-Id": _DEVICE_ID,
        "Open-token": _OPEN_TOKEN,
        "version": _VERSION,
        "userId": _USER_ID,
        "userMac": _USER_MAC,
        "Host": _HOST_HEADER,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "User-Agent": _USER_AGENT,
    }


def _is_http_m3u8(url: str) -> bool:
    return url.startswith(("http://", "https://")) and ".m3u8" in url


def _live_url_expire_ts(url: str) -> int:
    """live_url 自带的过期时间。"""
    try:
        query = urlparse(url).query
    except Exception:
        return 0
    params = parse_qs(query)

    timestamp = (params.get("timestamp") or [""])[0]
    if timestamp.isdigit():
        return int(timestamp)

    end_time = (params.get("e") or [""])[0]
    if len(end_time) == 14 and end_time.isdigit():
        try:
            return int(time.mktime(time.strptime(end_time, "%Y%m%d%H%M%S")))
        except ValueError:
            return 0
    return 0


async def _fetch_play_urls(client: httpx.AsyncClient, channel_id: str) -> dict:
    signed_path = _build_signed_path(_PATH_GET_PLAY_URLS, _build_query_params(channel_id))
    url = f"http://{_CONNECT_IP}{signed_path}"
    resp = await client.get(url, headers=_build_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _extract_live_url(payload: dict) -> str:
    data = payload.get("ret_data") or []
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return ""
    live_url = data[0].get("live_url") or ""
    return live_url if isinstance(live_url, str) else ""


async def resolve_woniu(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    channel_key = request.resource_id.strip("/").lower()
    entry = WONIU_CHANNELS.get(channel_key)

    if entry:
        channel_id = entry["channel_id"]
        channel_name = entry["name"]
    elif channel_key.isdigit():
        channel_id = channel_key
        channel_name = channel_key
    else:
        raise AdapterResolveError(
            "invalid_woniu_channel_id",
            f"不支持的蜗牛频道: {channel_key}",
        )

    last_payload: dict | None = None
    last_error: Exception | None = None

    for attempt in range(1, _ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(_RETRY_SLEEP)
        try:
            payload = await _fetch_play_urls(client, channel_id)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            logger.warning("蜗牛 %s 第 %s/%s 次接口失败: %s", channel_name, attempt, _ATTEMPTS, exc)
            continue

        last_payload = payload
        live_url = _extract_live_url(payload)
        if _is_http_m3u8(live_url):
            expires_at = _live_url_expire_ts(live_url)
            ttl = max(60, expires_at - int(time.time()) - _TTL_SKEW) if expires_at else 30 * 60
            return {
                "ok": True,
                "adapter": "woniu",
                "source_type": "hls",
                "url": live_url,
                "direct_playable": True,
                "requires_proxy": False,
                "headers": {},
                "ttl": ttl,
                "expires_at": expires_at or None,
                "warnings": [],
                "channel_id": channel_id,
                "channel_name": channel_name,
                "volatile_url": True,
            }

    # 12 次都没拿到 m3u8。区分两种失败原因：
    #   - last_payload is None: 全程没拿到一个能 parse 的响应，是网络/接口层失败。
    #   - 否则: 接口可达但 live_url 一直是 ?project=WNTV 占位，是上游业务问题。
    if last_payload is None:
        raise AdapterResolveError(
            "woniu_request_failed",
            f"蜗牛接口连续失败: {last_error}",
            status_code=502,
            retryable=True,
        )
    raise AdapterResolveError(
        "woniu_no_stream",
        f"蜗牛频道 {channel_name} 接口未返回 HTTP m3u8（最后响应: {last_payload!r}）",
        status_code=502,
        retryable=True,
    )
