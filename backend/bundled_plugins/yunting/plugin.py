#!/usr/bin/env python3
"""Independent Yunting Radio Provider using the managed HTTP capability."""
from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import unquote, urlparse

from waveflow_plugin_sdk import InvalidResource, PluginApplication, PluginError, RadioProvider, RadioReference, ResolveContext, StreamDescriptor

API = "https://ytmsout.radio.cn/web/appBroadcast/list"
SIGN_KEY = "f0fc4c668392f9f9a447e48584c214ee"
PROVINCES = (
    "340000", "110000", "500000", "350000", "620000", "440000", "450000", "520000",
    "460000", "130000", "410000", "230000", "420000", "430000", "220000", "320000",
    "360000", "210000", "150000", "640000", "630000", "370000", "140000", "610000",
    "310000", "510000", "540000", "650000", "660000", "530000", "330000",
)
CENTRAL_PROVINCE = "0"
CATALOG_PROVINCES = PROVINCES + (CENTRAL_PROVINCE,)
YUNTING_MAX_CONCURRENCY = 4
YUNTING_REQUEST_TIMEOUT_SECONDS = 8
PROVINCE_NAMES = {
    "110000": "北京", "130000": "河北", "140000": "山西", "150000": "内蒙古", "210000": "辽宁",
    "220000": "吉林", "230000": "黑龙江", "310000": "上海", "320000": "江苏", "330000": "浙江",
    "340000": "安徽", "350000": "福建", "360000": "江西", "370000": "山东", "410000": "河南",
    "420000": "湖北", "430000": "湖南", "440000": "广东", "450000": "广西", "460000": "海南",
    "500000": "重庆", "510000": "四川", "520000": "贵州", "530000": "云南", "540000": "西藏",
    "610000": "陕西", "620000": "甘肃", "630000": "青海", "640000": "宁夏", "650000": "新疆",
    "660000": "新疆兵团",
    CENTRAL_PROVINCE: "中央广播",
}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

_HLS_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
}
_STREAM_SEMANTIC_FIELDS = (
    "transport", "stream_type", "streamType", "protocol", "format",
    "play_type", "playType", "content_type", "contentType", "mime_type", "mimeType",
)


def _is_hls_semantic(value: Any) -> bool:
    normalized = unquote(str(value or "")).strip().lower()
    if not normalized:
        return False
    media_type = normalized.split(";", 1)[0].strip()
    return (
        media_type in _HLS_CONTENT_TYPES
        or normalized in {"hls", "m3u8", "mpegurl"}
        or "m3u8" in normalized
    )


def _stream_transport(url: str, record: dict[str, Any]) -> str:
    """Infer the transport from the URL and provider-declared media semantics."""
    parsed = urlparse(url)
    path = unquote(parsed.path or "").strip().lower().rstrip("/")
    if path.endswith(".m3u8") or any(_is_hls_semantic(value) for value in parsed.query.split("&")):
        return "hls"
    for field in _STREAM_SEMANTIC_FIELDS:
        if _is_hls_semantic(record.get(field)):
            return "hls"
    return "audio_http"


def _headers(params: dict[str, Any]) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    query = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    signature = hashlib.md5(f"{query}&timestamp={timestamp}&key={SIGN_KEY}".encode()).hexdigest().upper()
    return {
        "User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json",
        "Origin": "https://www.radio.cn", "Referer": "https://www.radio.cn/", "equipmentId": "0000",
        "platformCode": "WEB", "timestamp": timestamp, "sign": signature,
    }


def _failure(
    code: str,
    message: str,
    *,
    retryable: bool = True,
    details: dict[str, Any] | None = None,
) -> PluginError:
    return PluginError("TEMPORARY_UPSTREAM_FAILURE", message, retryable=retryable, category="provider",
                       details={"provider_code": code, **(details or {})})


def _records(response: Any) -> list[dict[str, Any]]:
    if getattr(response, "status", 200) in {401, 403}:
        raise PluginError("AUTH_FAILED", "Yunting API authentication failed", category="auth")
    body = getattr(response, "body", None)
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise _failure("malformed_response", "Yunting API returned an invalid response")
    records = []
    for raw in body["data"]:
        if not isinstance(raw, dict):
            continue
        content_id = str(raw.get("contentId") or "").strip()
        title = str(raw.get("title") or raw.get("name") or "").strip()
        if content_id and title:
            records.append({"raw": raw, "content_id": content_id, "title": title})
    return records


class Provider(RadioProvider):
    def _request(self, province: str, context: ResolveContext) -> list[dict[str, Any]]:
        if province not in CATALOG_PROVINCES:
            raise InvalidResource("Unsupported Yunting province")
        params = {"categoryId": 0, "provinceCode": province}
        return _records(context.capabilities.managed_http(
            API, query=params, headers=_headers(params), response_mode="json",
            timeout=YUNTING_REQUEST_TIMEOUT_SECONDS,
        ))

    @staticmethod
    def _logo_url(raw: dict[str, Any]) -> str:
        value = raw.get("image")
        if not isinstance(value, str):
            return ""
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            return ""
        return value

    def catalog(self, payload: dict[str, Any], context: ResolveContext) -> dict[str, Any]:
        records_by_province: dict[str, list[dict[str, Any]]] = {}
        failures: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=YUNTING_MAX_CONCURRENCY, thread_name_prefix="yunting-catalog") as pool:
            futures = {pool.submit(self._request, province, context): province for province in CATALOG_PROVINCES}
            for future in as_completed(futures):
                context.raise_if_cancelled()
                province = futures[future]
                try:
                    records_by_province[province] = future.result()
                except PluginError as exc:
                    failures[province] = str(getattr(exc, "code", "PROVIDER_ERROR"))
                except Exception as exc:
                    if hasattr(exc, "code") and hasattr(exc, "message"):
                        raise
                    failures[province] = "PROVIDER_ERROR"
        if failures:
            failed_regions = [province for province in CATALOG_PROVINCES if province in failures]
            failed_errors = {province: failures[province] for province in failed_regions}
            summary = ", ".join(f"{province}={failures[province]}" for province in failed_regions)
            raise _failure(
                "catalog_incomplete",
                f"Yunting catalog incomplete: {summary}",
                details={"failed_regions": failed_regions, "failed_errors": failed_errors},
            )
        stations: list[dict[str, Any]] = []
        seen_content_ids: set[str] = set()
        for province in CATALOG_PROVINCES:
            context.raise_if_cancelled()
            for record in records_by_province.get(province, []):
                if record["content_id"] in seen_content_ids:
                    continue
                seen_content_ids.add(record["content_id"])
                raw = record["raw"]
                stations.append({
                    "station_ref": {"provider_key": "yunting", "provider_station_id": record["content_id"]},
                    "name": record["title"], "logo_url": self._logo_url(raw),
                    "group_name": PROVINCE_NAMES.get(province, province), "country": "CN", "language": "zh-CN",
                    "frequency": str(raw.get("frequency") or ""),
                    "metadata": {"province_code": province, "subtitle": str(raw.get("subtitle") or "")},
                    "playback_config": {"province_code": province}, "ttl_seconds": 7200,
                })
        if not stations:
            raise _failure("catalog_unavailable", "Yunting catalog unavailable")
        return {"stations": stations}

    def resolve_stream(self, reference: RadioReference, context: ResolveContext) -> StreamDescriptor:
        province = str(reference.playback_config.get("province_code") or "")
        if not province:
            raise InvalidResource("Yunting station is missing province configuration")
        record = next((x for x in self._request(province, context) if x["content_id"] == reference.provider_station_id), None)
        if record is None:
            raise _failure("station_not_found", "Yunting station is no longer available")
        url = str(record["raw"].get("playUrlLow") or "").strip()
        if url.startswith("http://"):
            url = "https://" + url[7:]
        if not url.startswith(("http://", "https://")):
            raise _failure("no_stream", "Yunting station returned no playable stream")
        return StreamDescriptor(url=url, transport=_stream_transport(url, record["raw"]),
                                headers={"User-Agent": UA, "Referer": "https://www.radio.cn/"},
                                ttl_seconds=3600, volatile_url=True, requires_proxy=False)

    def programme(self, reference: RadioReference, context: ResolveContext) -> dict[str, Any]:
        province = str(reference.playback_config.get("province_code") or "")
        record = next((x for x in self._request(province, context) if x["content_id"] == reference.provider_station_id), None)
        if record is None:
            raise _failure("station_not_found", "Yunting station is no longer available")
        subtitle = str(record["raw"].get("subtitle") or "").strip()
        now = int(time.time())
        programmes = []
        if subtitle:
            programmes.append({"provider_programme_id": "current:" + hashlib.sha256(subtitle.encode()).hexdigest()[:16],
                               "title": subtitle, "subtitle": subtitle, "updated_at": now, "expires_at": now + 600})
        return {"station_ref": {"provider_key": "yunting", "provider_station_id": reference.provider_station_id},
                "revision": hashlib.sha256((subtitle or "empty").encode()).hexdigest()[:24], "programmes": programmes}


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/yunting")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_radio("yunting", Provider()).run()


if __name__ == "__main__":
    main()
