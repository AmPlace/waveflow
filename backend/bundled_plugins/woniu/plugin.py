#!/usr/bin/env python3
"""Independent Woniu TV Provider using the generic direct-network boundary."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

from waveflow_plugin_sdk import InvalidResource, PluginApplication, PluginError, ResolveContext, StreamDescriptor, TVProvider, TVReference


WONIU_CONNECT_IP = "119.44.12.12"
WONIU_HOST_HEADER = "liveepg.hunancatv.com"
WONIU_PATH_GET_PLAY_URLS = "/v1/getPlayUrls"
WONIU_QD_V = "1ph"
WONIU_SALT = "d7npemiwtm0wiogrbw84kmbkl6d5ih38"
WONIU_PARTNER = "3"
WONIU_CITYCODE = ""
WONIU_ADCODE = ""
WONIU_SRC = "05025022911000000000"
WONIU_DEVICE_ID = "tv_fixture_device"
WONIU_VERSION = "8.3"
WONIU_USER_AGENT = "okhttp/3.10.0.7"
WONIU_AUTH_HEADERS = {
    # Auth values are release-time configuration.  The unsigned source
    # fixture intentionally remains fail-closed when they are absent.
    "Cookie": "",
    "Authorization": "",
    "X-Device-Id": WONIU_DEVICE_ID,
    "Open-token": "",
    "userId": "",
    "userMac": "00:00:00:00:00:00",
}
WONIU_ATTEMPTS = 12
WONIU_RETRY_SLEEP_SECONDS = 0.1
WONIU_TIMEOUT_SECONDS = 8.0
WONIU_TTL_SKEW_SECONDS = 600
WONIU_CHANNELS: dict[str, dict[str, str]] = {'hnws4k': {'name': '湖南卫视4K', 'channel_id': '5216'}, 'hnjs': {'name': '湖南经视高清', 'channel_id': '5377'}, 'hnyl': {'name': '湖南娱乐高清', 'channel_id': '5391'}, 'hndy': {'name': '湖南电影高清', 'channel_id': '5405'}, 'hnds': {'name': '湖南都市高清', 'channel_id': '5398'}, 'hndsj': {'name': '湖南电视剧高清', 'channel_id': '5412'}, 'klg': {'name': '快乐购高清', 'channel_id': '5426'}, 'jyjs': {'name': '金鹰纪实高清', 'channel_id': '5384'}, 'hnaw': {'name': '湖南爱晚高清', 'channel_id': '5419'}, 'jykt': {'name': '金鹰卡通高清', 'channel_id': '5349'}, 'guide': {'name': '导视频道', 'channel_id': '5370'}, 'hnjy': {'name': '湖南教育频道', 'channel_id': '5356'}, 'cctv1': {'name': 'CCTV-1高清', 'channel_id': '5104'}, 'cctv2': {'name': 'CCTV-2高清', 'channel_id': '5111'}, 'cctv3': {'name': 'CCTV-3高清', 'channel_id': '5118'}, 'cctv4': {'name': 'CCTV-4高清', 'channel_id': '5125'}, 'cctv5': {'name': 'CCTV-5高清', 'channel_id': '5139'}, 'cctv6': {'name': 'CCTV-6高清', 'channel_id': '5146'}, 'cctv7': {'name': 'CCTV-7高清', 'channel_id': '5153'}, 'cctv8': {'name': 'CCTV-8高清', 'channel_id': '5160'}, 'cctv9': {'name': 'CCTV-9高清', 'channel_id': '5167'}, 'cctv10': {'name': 'CCTV-10高清', 'channel_id': '5174'}, 'cctv11': {'name': 'CCTV-11戏曲', 'channel_id': '5517'}, 'cctv12': {'name': 'CCTV-12高清', 'channel_id': '5538'}, 'cctv13': {'name': 'CCTV-13新闻', 'channel_id': '5524'}, 'cctv14': {'name': 'CCTV-14高清', 'channel_id': '5545'}, 'cctv15': {'name': 'CCTV-15音乐', 'channel_id': '5531'}, 'cctv17': {'name': 'CCTV-17高清', 'channel_id': '5552'}, 'cctv4k': {'name': 'CCTV-4K', 'channel_id': '6834'}, 'cctv5plus': {'name': 'CCTV-5+高清', 'channel_id': '5132'}, 'jsws': {'name': '江苏卫视高清', 'channel_id': '5244'}, 'zjws': {'name': '浙江卫视高清', 'channel_id': '5237'}, 'dfws': {'name': '东方卫视高清', 'channel_id': '5258'}, 'bjws': {'name': '北京卫视高清', 'channel_id': '5223'}, 'gdws': {'name': '广东卫视高清', 'channel_id': '5251'}, 'szws': {'name': '深圳卫视高清', 'channel_id': '5230'}, 'tjws': {'name': '天津卫视高清', 'channel_id': '5300'}, 'hbws': {'name': '湖北卫视高清', 'channel_id': '5265'}, 'ahws': {'name': '安徽卫视高清', 'channel_id': '5272'}, 'cqws': {'name': '重庆卫视高清', 'channel_id': '5307'}, 'hljws': {'name': '黑龙江卫视高清', 'channel_id': '5293'}, 'lnws': {'name': '辽宁卫视高清', 'channel_id': '5279'}, 'gxws': {'name': '广西卫视高清', 'channel_id': '5209'}, 'qhws': {'name': '青海卫视高清', 'channel_id': '5601'}, 'shanxi-ws': {'name': '山西卫视高清', 'channel_id': '5559'}, 'nmgws': {'name': '内蒙古卫视高清', 'channel_id': '5608'}, 'nxws': {'name': '宁夏卫视高清', 'channel_id': '5573'}, 'ynws': {'name': '云南卫视高清', 'channel_id': '5181'}, 'xzws': {'name': '西藏卫视高清', 'channel_id': '5195'}, 'shaanxi-ws': {'name': '陕西卫视高清', 'channel_id': '5580'}, 'xjws': {'name': '新疆卫视高清', 'channel_id': '5202'}, 'btws': {'name': '兵团卫视高清', 'channel_id': '5861'}, 'kaku': {'name': '卡酷动画', 'channel_id': '5882'}, 'cetv1': {'name': 'CETV-1', 'channel_id': '5896'}, 'cetv4': {'name': 'CETV-4', 'channel_id': '6827'}, 'hxdj': {'name': '红星党建', 'channel_id': '6778'}, 'cgtn': {'name': 'CGTN', 'channel_id': '6841'}, 'cgtn-a': {'name': 'CGTN-A', 'channel_id': '6848'}, 'cgtn-r': {'name': 'CGTN-R', 'channel_id': '6855'}, 'cgtn-e': {'name': 'CGTN-E', 'channel_id': '6862'}, 'cgtn-f': {'name': 'CGTN-F', 'channel_id': '6869'}, 'klcd': {'name': '快乐垂钓高清', 'channel_id': '5685'}, 'shdy': {'name': '四海钓鱼高清', 'channel_id': '5454'}, 'tea': {'name': '茶频道高清', 'channel_id': '5692'}, 'xfpy': {'name': '先锋乒羽', 'channel_id': '5657'}, 'shss': {'name': '生活时尚高清', 'channel_id': '5503'}, 'jbty': {'name': '劲爆体育高清', 'channel_id': '5468'}, 'dcwt4k': {'name': '多彩文体4K', 'channel_id': '5475'}, 'chc-hd': {'name': 'CHC高清电影', 'channel_id': '5664'}, 'yxfy': {'name': '游戏风云高清', 'channel_id': '5489'}, 'tywq': {'name': '天元围棋高清', 'channel_id': '5433'}, 'dmxc': {'name': '动漫秀场高清', 'channel_id': '5671'}, 'shpd': {'name': '书画频道', 'channel_id': '5636'}, 'leyou': {'name': '乐游高清', 'channel_id': '5482'}, 'zhtc': {'name': '中华特产', 'channel_id': '6876'}, 'fztd': {'name': '法治天地高清', 'channel_id': '5678'}, 'sypd': {'name': '摄影频道', 'channel_id': '5440'}, 'cftx': {'name': '财富天下', 'channel_id': '5461'}, 'qsjl': {'name': '求索记录高清', 'channel_id': '6932'}, 'dsjc': {'name': '都市剧场高清', 'channel_id': '5847'}, 'qssh': {'name': '求索生活高清', 'channel_id': '6939'}, 'qskx': {'name': '求索科学高清', 'channel_id': '6953'}, 'chc-action': {'name': 'CHC动作电影高清', 'channel_id': '6911'}, 'chc-family': {'name': 'CHC家庭影院高清', 'channel_id': '6918'}}


def _failure(code: str, message: str, *, retryable: bool = True) -> PluginError:
    return PluginError("TEMPORARY_UPSTREAM_FAILURE", message, retryable=retryable,
                       category="provider", details={"provider_code": code})


def _dynamic_k_uid(seed: str, millis: int) -> str:
    parts = seed.split("_")
    if len(parts) >= 3 and parts[-2].isdigit():
        parts[-2] = str(millis)
    return "_".join(parts)


def _build_query_params(channel_id: str, *, now: float | None = None) -> list[tuple[str, str]]:
    current = time.time() if now is None else now
    return [
        ("channel", channel_id), ("program", ""), ("partner", WONIU_PARTNER),
        ("citycode", WONIU_CITYCODE), ("adcode", WONIU_ADCODE), ("delay", ""),
        ("definition", ""), ("tm", str(int(current))), ("src", WONIU_SRC),
        ("k_uid", _dynamic_k_uid(WONIU_DEVICE_ID, int(current * 1000))),
    ]


def _build_signed_path(path: str, params: list[tuple[str, str]]) -> str:
    query = urlencode(params, doseq=False, quote_via=quote)
    sign_text = f"{path}?{query}"
    vf = hashlib.md5(f"{sign_text}&qd_v={WONIU_QD_V}{WONIU_SALT}".encode()).hexdigest()
    return f"{sign_text}&qd_v={quote(WONIU_QD_V)}&vf={vf}"


def _build_headers() -> dict[str, str]:
    return {
        **WONIU_AUTH_HEADERS,
        "version": WONIU_VERSION,
        "Host": WONIU_HOST_HEADER,
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "User-Agent": WONIU_USER_AGENT,
    }


def _is_http_m3u8(url: str) -> bool:
    return url.startswith(("http://", "https://")) and ".m3u8" in url


def _live_url_expire_ts(url: str) -> int:
    try:
        params = parse_qs(urlparse(url).query)
    except ValueError:
        return 0
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


def _fetch_play_urls(channel_id: str) -> dict[str, Any]:
    signed_path = _build_signed_path(WONIU_PATH_GET_PLAY_URLS, _build_query_params(channel_id))
    request = Request(f"http://{WONIU_CONNECT_IP}{signed_path}", headers=_build_headers(), method="GET")
    with urlopen(request, timeout=WONIU_TIMEOUT_SECONDS) as response:
        body = response.read()
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Woniu response is not an object")
    return value


def _extract_live_url(payload: dict[str, Any]) -> str:
    data = payload.get("ret_data") or []
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return ""
    value = data[0].get("live_url") or ""
    return value if isinstance(value, str) else ""


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        context.raise_if_cancelled()
        channel_key = reference.resource_id.strip("/").lower()
        entry = WONIU_CHANNELS.get(channel_key)
        if entry:
            channel_id, channel_name = entry["channel_id"], entry["name"]
        elif channel_key.isdigit():
            channel_id, channel_name = channel_key, channel_key
        else:
            raise InvalidResource(f"Unsupported Woniu channel: {channel_key}")

        last_payload: dict[str, Any] | None = None
        for attempt in range(WONIU_ATTEMPTS):
            if attempt:
                time.sleep(WONIU_RETRY_SLEEP_SECONDS)
            try:
                payload = _fetch_play_urls(channel_id)
            except Exception:
                continue
            last_payload = payload
            live_url = _extract_live_url(payload)
            if not _is_http_m3u8(live_url):
                continue
            expires_at = _live_url_expire_ts(live_url)
            ttl = max(60, expires_at - int(time.time()) - WONIU_TTL_SKEW_SECONDS) if expires_at else 30 * 60
            return StreamDescriptor.hls(
                live_url, ttl_seconds=ttl, expires_at=expires_at or None,
                volatile_url=True, requires_proxy=False,
                provider_diagnostics={"channel_id": channel_id, "channel_name": channel_name},
            )
        if last_payload is None:
            raise _failure("woniu_request_failed", "Woniu endpoint was unavailable")
        raise _failure("woniu_no_stream", f"Woniu channel {channel_name} returned no HTTP m3u8")


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/woniu")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "woniu", Provider()
    ).run()


if __name__ == "__main__":
    main()
