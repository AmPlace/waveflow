#!/usr/bin/env python3
"""Independent Qukan TV Provider using the generic managed HTTP capability."""
from __future__ import annotations

from urllib.parse import urlencode

from waveflow_plugin_sdk import (
    InvalidResource,
    PluginApplication,
    PluginError,
    ResolveContext,
    StreamDescriptor,
    TVProvider,
    TVReference,
)


QUKAN_API_URL = "https://www.qukanvideo.com/h5/channel/view/item/AntiTheft/playUrl"
QUKAN_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://www.qukanvideo.com",
    "Referer": "https://www.qukanvideo.com/",
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
    ),
}
QUKAN_CHANNELS = {
    "jimei": {
        "name": "厦门集美电视台综合频道",
        "live_id": "1778569138673121",
        "sign": "18d34b7bfe26df9912192850e3eb36c3",
    },
}


def _failure(code: str, message: str) -> PluginError:
    return PluginError(
        "TEMPORARY_UPSTREAM_FAILURE", message, retryable=True,
        category="provider", details={"provider_code": code},
    )


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        channel_key = reference.resource_id.strip("/").lower()
        entry = QUKAN_CHANNELS.get(channel_key)
        if not entry:
            raise InvalidResource(f"Unsupported Qukan channel: {channel_key}")
        payload = {"source": "web", "liveId": entry["live_id"], "sign": entry["sign"]}
        headers = {**QUKAN_HEADERS, "Referer": f"https://www.qukanvideo.com/cloud/h5/{entry['live_id']}"}
        try:
            response = context.capabilities.managed_http(
                QUKAN_API_URL, method="POST", headers=headers, text_body=urlencode(payload),
                response_mode="json", timeout=10,
            )
            data = response.body
            if not isinstance(data, dict):
                raise _failure("qukan_parse_failed", "Qukan response is not an object")
            if data.get("code") != 0:
                raise _failure("qukan_business_error", "Qukan returned a business error")
            value = data.get("value") or {}
            url = str(value.get("url") or "").strip() if isinstance(value, dict) else ""
            if not url.startswith(("http://", "https://")):
                raise _failure("qukan_no_stream", "Qukan returned no playable stream")
        except PluginError:
            raise
        except Exception:
            raise _failure("qukan_request_failed", "Qukan upstream request failed") from None
        return StreamDescriptor.hls(url, ttl_seconds=1800, volatile_url=True, requires_proxy=False)


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/qukan")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "qukan", Provider()
    ).run()


if __name__ == "__main__":
    main()
