#!/usr/bin/env python3
"""Independent Hit FM / POP Radio provider."""
from __future__ import annotations

from typing import Any

from waveflow_plugin_sdk import InvalidResource, PluginApplication, PluginError, RadioProvider, RadioReference, ResolveContext, StreamDescriptor


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
REFRESH_TTL_SECONDS = 5 * 60 * 60

STATIONS = {
    "hitfm": {"name": "Hit FM 台北", "channel_id": "1", "logo": "", "frequency": "FM 107.7", "api": "https://www.hitoradio.com/newweb/hichannel.php", "referer": "https://www.hitoradio.com/newweb/onair_n_ajax.php", "origin": "https://www.hitoradio.com"},
    "hitfm_taichung": {"name": "Hit FM 台中", "channel_id": "2", "logo": "", "frequency": "FM 91.5", "api": "https://www.hitoradio.com/newweb/hichannel.php", "referer": "https://www.hitoradio.com/newweb/onair_n_ajax.php", "origin": "https://www.hitoradio.com"},
    "hitfm_tainan": {"name": "Hit FM 台南", "channel_id": "3", "logo": "", "frequency": "FM 90.1", "api": "https://www.hitoradio.com/newweb/hichannel.php", "referer": "https://www.hitoradio.com/newweb/onair_n_ajax.php", "origin": "https://www.hitoradio.com"},
    "hitfm_yilan": {"name": "Hit FM 宜兰", "channel_id": "4", "logo": "", "frequency": "FM 97.1", "api": "https://www.hitoradio.com/newweb/hichannel.php", "referer": "https://www.hitoradio.com/newweb/onair_n_ajax.php", "origin": "https://www.hitoradio.com"},
    "hitfm_huadong": {"name": "Hit FM 花东", "channel_id": "5", "logo": "", "frequency": "FM 107.7", "api": "https://www.hitoradio.com/newweb/hichannel.php", "referer": "https://www.hitoradio.com/newweb/onair_n_ajax.php", "origin": "https://www.hitoradio.com"},
    "pop917": {"name": "POP Radio", "channel_id": "1", "logo": "", "frequency": "FM 91.7", "api": "https://www.pop917.com/ajax.aspx", "referer": "https://www.pop917.com/liveStream.aspx?id=1", "origin": "https://www.pop917.com"},
}


def _failure(code: str, message: str) -> PluginError:
    return PluginError("TEMPORARY_UPSTREAM_FAILURE", message, retryable=True, category="provider",
                       details={"provider_code": code})


class Provider(RadioProvider):
    def catalog(self, payload: dict[str, Any], context: ResolveContext) -> dict[str, Any]:
        return {"stations": [{
            "station_ref": {"provider_key": "hitfm", "provider_station_id": station_id},
            "name": item["name"], "logo_url": item["logo"], "group_name": "Hit FM / POP Radio",
            "country": "TW", "language": "zh-TW", "frequency": item["frequency"],
            "metadata": {"channel_id": item["channel_id"], "origin": item["origin"]},
            "playback_config": {
                "channel_id": item["channel_id"], "api": item["api"],
                "referer": item["referer"], "origin": item["origin"],
            },
            "ttl_seconds": 24 * 60 * 60,
        } for station_id, item in STATIONS.items()]}

    def resolve_stream(self, reference: RadioReference, context: ResolveContext) -> StreamDescriptor:
        station_id = reference.provider_station_id.strip().lower()
        item = STATIONS.get(station_id)
        if item is None:
            raise InvalidResource("Unsupported Hit FM / POP station")
        config = reference.playback_config
        api = str(config.get("api") or item["api"])
        channel_id = str(config.get("channel_id") or item["channel_id"])
        headers = {
            "Accept": "*/*", "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest", "User-Agent": UA,
            "Referer": str(config.get("referer") or item["referer"]),
            "Origin": str(config.get("origin") or item["origin"]),
        }
        response = context.capabilities.managed_http(
            api, method="POST", headers=headers,
            text_body=f"channelID={channel_id}&action=getLIVEURL",
            response_mode="text", timeout=15,
        )
        value = str(response.body or "").strip().strip('"')
        if not value.startswith(("http://", "https://")):
            raise _failure("malformed_or_no_stream", "Hit FM / POP returned no playable stream")
        transport = "hls" if ".m3u8" in value.lower() or "playlist" in value.lower() else "audio_http"
        return StreamDescriptor(
            url=value, transport=transport, ttl_seconds=REFRESH_TTL_SECONDS,
            volatile_url=True, requires_proxy=False,
        )


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/hitfm")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_radio(
        "hitfm", Provider()
    ).run()


if __name__ == "__main__":
    main()
