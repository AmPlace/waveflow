#!/usr/bin/env python3
"""Independent MyRadio.tw Radio Provider.

The provider publishes explicit station IDs and stream URLs.  It does not
consult Core's legacy cache or infer a station from a name/frequency.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from waveflow_plugin_sdk import (
    InvalidResource, PluginApplication, PluginError, RadioProvider,
    RadioReference, ResolveContext, StreamDescriptor, load_resource_text,
)


BASE = "https://myradio-dev.zeabur.app"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
STATIC_RESOURCE = "static.json"
STATION_ID_RE = re.compile(r"^A[0-9]{4}$")
SITEMAP_RE = re.compile(r"<loc>https?://myradio\.com\.tw/radios/(A[0-9]{4})</loc>")
ALLOWED_DYNAMIC_HOSTS = [
    "myradio-dev.zeabur.app", "myradio.com.tw", "pop.olis.com.tw", "best.olis.com.tw",
    "ipget.apple-line.com", "hichannel.com.tw",
]


def _failure(code: str, message: str, *, retryable: bool = True) -> PluginError:
    return PluginError(
        "TEMPORARY_UPSTREAM_FAILURE", message, retryable=retryable,
        category="provider", details={"provider_code": code},
    )


def _load_static() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        value = json.loads(load_resource_text(STATIC_RESOURCE, anchor=__file__))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _failure("static_catalog_invalid", "MyRadio static catalog is unavailable", retryable=False) from exc
    static = value.get("static") if isinstance(value, dict) else None
    dynamic = value.get("dynamic") if isinstance(value, dict) else None
    if not isinstance(static, list) or not isinstance(dynamic, list):
        raise _failure("static_catalog_invalid", "MyRadio static catalog is malformed", retryable=False)
    return [item for item in static if isinstance(item, dict)], [item for item in dynamic if isinstance(item, dict)]


def _station(item: dict[str, Any], *, fallback_url: str = "") -> dict[str, Any] | None:
    station_id = str(item.get("id") or "").strip().upper()
    name = str(item.get("name") or "").strip()
    url = str(item.get("url") or fallback_url).strip()
    if not STATION_ID_RE.fullmatch(station_id) or not name or not url.startswith(("http://", "https://")):
        return None
    return {
        "id": station_id,
        "name": name,
        "url": url,
        "logo": str(item.get("logo") or f"https://images.myradio.com.tw/images/{station_id}.jpg"),
        "freq": str(item.get("freq") or ""),
        "tag": str(item.get("tag") or ""),
        "codec": item.get("codec", 0),
    }


def _response_body(response: Any) -> Any:
    body = getattr(response, "body", None)
    if isinstance(body, (dict, list, str)):
        if isinstance(body, str):
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise _failure("malformed_response", "MyRadio returned invalid JSON") from exc
        return body
    raise _failure("malformed_response", "MyRadio returned an invalid response")


class Provider(RadioProvider):
    def _http(self, context: ResolveContext, url: str, *, method: str = "GET",
              headers: dict[str, str] | None = None, query: dict[str, Any] | None = None,
              text_body: str | None = None) -> Any:
        request_headers = {"User-Agent": UA, **(headers or {})}
        return _response_body(context.capabilities.managed_http(
            url, method=method, headers=request_headers, query=query,
            text_body=text_body, response_mode="json" if text_body is None else "json", timeout=15,
        ))

    def _resolve_special_url(self, value: str, context: ResolveContext) -> str:
        if value.startswith(("http://", "https://")):
            return value
        if ":" not in value:
            raise InvalidResource("MyRadio station URL is invalid")
        kind, station = value.split(":", 1)
        station = station.strip()
        if not station:
            raise InvalidResource("MyRadio station URL is invalid")
        if kind == "myPop":
            body = self._http(
                context, "https://pop.olis.com.tw/pop_api/index.php/Basic/GetHLS",
                method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"},
                text_body=f"station={station}",
            )
            value = ((body.get("data") or {}).get("hlsurl") or {}).get(station) if isinstance(body, dict) else None
        elif kind == "myBest":
            body = self._http(
                context, "https://best.olis.com.tw/best_api/index.php/Basic/GetHLS",
                method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"},
                text_body=f"station={station}",
            )
            value = ((body.get("data") or {}).get("hlsurl")) if isinstance(body, dict) else None
        elif kind == "myAline":
            endpoint = "https://ipget.apple-line.com/alinePlayer.php" if station == "1" else "https://ipget.apple-line.com/youngPlayer.php"
            response = context.capabilities.managed_http(endpoint, headers={"User-Agent": UA}, response_mode="text", timeout=15)
            value = str(response.body or "").strip()
        else:
            raise InvalidResource("Unsupported MyRadio stream URL")
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            raise _failure("no_stream", "MyRadio returned no playable stream")
        return value

    def _dynamic_ids(self, context: ResolveContext, static_ids: set[str]) -> list[str]:
        try:
            response = context.capabilities.managed_http(
                f"{BASE}/sitemap.xml", headers={"User-Agent": UA}, response_mode="text", timeout=15,
            )
            ids = sorted(set(SITEMAP_RE.findall(str(response.body or ""))) - static_ids)
            if ids:
                return ids
        except PluginError:
            pass
        _static, dynamic = _load_static()
        return sorted({str(item.get("id") or "").upper() for item in dynamic} - static_ids)

    def _dynamic_station(self, station_id: str, context: ResolveContext) -> dict[str, Any] | None:
        # The Next data endpoint is a stable public JSON surface used by the
        # legacy resolver.  A fallback build id keeps this provider bounded
        # when the landing page is unavailable; it does not use Core state.
        for build_id in ("NbGrNnycPXoV9eY5v4Kt-",):
            url = f"{BASE}/_next/data/{build_id}/zh-TW/radios/{station_id}.json?id={station_id}"
            try:
                body = self._http(context, url, headers={"x-nextjs-data": "1"})
                radio = ((body.get("pageProps") or {}).get("radio")) if isinstance(body, dict) else None
                if not isinstance(radio, dict):
                    continue
                stream_url = self._resolve_special_url(str(radio.get("url") or ""), context)
                return _station({
                    "id": radio.get("id"), "name": radio.get("name"), "url": stream_url,
                    "freq": radio.get("des"), "tag": radio.get("tag"),
                })
            except PluginError:
                return None
        return None

    def catalog(self, payload: dict[str, Any], context: ResolveContext) -> dict[str, Any]:
        static, _dynamic = _load_static()
        stations = [item for raw in static if (item := _station(raw)) is not None]
        by_id = {item["id"]: item for item in stations}
        dynamic_ids = self._dynamic_ids(context, set(by_id))
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="myradio-catalog") as pool:
            futures = {pool.submit(self._dynamic_station, station_id, context): station_id for station_id in dynamic_ids}
            for future in as_completed(futures):
                context.raise_if_cancelled()
                try:
                    item = future.result()
                except PluginError:
                    item = None
                if item is not None:
                    by_id.setdefault(item["id"], item)
        result = []
        for station_id in sorted(by_id):
            item = by_id[station_id]
            result.append({
                "station_ref": {"provider_key": "myradio", "provider_station_id": station_id},
                "name": item["name"], "logo_url": item["logo"], "group_name": "MyRadio",
                "country": "TW", "language": "zh-TW", "frequency": item["freq"],
                "metadata": {"tag": item["tag"], "codec": item["codec"]},
                "playback_config": {"stream_url": item["url"]},
                "ttl_seconds": 24 * 60 * 60,
            })
        if not result:
            raise _failure("catalog_unavailable", "MyRadio catalog unavailable")
        return {"stations": result}

    def resolve_stream(self, reference: RadioReference, context: ResolveContext) -> StreamDescriptor:
        value = str(reference.playback_config.get("stream_url") or "").strip()
        if not value.startswith(("http://", "https://")):
            raise InvalidResource("MyRadio source does not contain an explicit stream URL")
        transport = "hls" if ".m3u8" in value.lower() or "playlist" in value.lower() else "audio_http"
        return StreamDescriptor(
            url=value, transport=transport, ttl_seconds=24 * 60 * 60,
            volatile_url=True, requires_proxy=False,
        )


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/myradio")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_radio(
        "myradio", Provider()
    ).run()


if __name__ == "__main__":
    main()
