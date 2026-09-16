#!/usr/bin/env python3
"""Independent HBTV Provider using the generic managed HTTP capability."""
from __future__ import annotations

import hashlib
import time
from typing import Any

from waveflow_plugin_sdk import (
    InvalidResource,
    PluginApplication,
    PluginError,
    ResolveContext,
    StreamDescriptor,
    TVProvider,
    TVReference,
)


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


def _failure(code: str, message: str, *, retryable: bool = True) -> PluginError:
    return PluginError(
        "TEMPORARY_UPSTREAM_FAILURE", message, retryable=retryable,
        category="provider", details={"provider_code": code},
    )


def _channel(reference: TVReference) -> tuple[str, int, str]:
    key = reference.resource_id.strip("/").lower()
    configured = HBTv_CHANNELS.get(key)
    if configured:
        return key, int(configured["id"]), str(configured["name"])
    if key.isdigit():
        return key, int(key), key
    raise InvalidResource(f"Unsupported HBTV channel: {key}")


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        channel_key, target_id, fallback_name = _channel(reference)
        try:
            response = context.capabilities.managed_http(
                HBTv_LIST_URL,
                query={"catalogId": "32557", "siteId": "1"},
                headers=HBTv_HEADERS,
                response_mode="json",
                timeout=15,
            )
            data = response.body
            if not isinstance(data, dict):
                raise ValueError("HBTV response is not an object")
            news = (data.get("returnData") or {}).get("news")
            if not isinstance(news, list):
                raise _failure("hbtv_parse_failed", "HBTV response structure is invalid")
            target = next((item for item in news if isinstance(item, dict) and item.get("id") == target_id), None)
            if not target:
                raise _failure("hbtv_channel_not_found", "HBTV channel was not found")
            channel_name = str(target.get("title") or fallback_name)
            live_video = target.get("liveVideo")
            if not isinstance(live_video, list) or not live_video or not isinstance(live_video[0], dict):
                raise _failure("hbtv_no_live_url", "HBTV returned no live URL")
            formats = live_video[0].get("formats")
            base_url = formats[0].get("url") if isinstance(formats, list) and formats and isinstance(formats[0], dict) else ""
            if not base_url:
                raise _failure("hbtv_no_live_url", "HBTV returned an empty live URL")
            movie = (target.get("appCustomParams") or {}).get("movie") or {}
            live_uri, live_key = movie.get("liveUri"), movie.get("liveKey")
            if not live_uri or not live_key:
                raise _failure("hbtv_no_sign_params", "HBTV signing parameters are missing")
            expires = int(time.time()) + 7200
            signature = hashlib.md5((str(live_uri) + str(live_key) + str(expires)).encode()).hexdigest()
            url = f"{base_url}?t={expires}&k={signature}"
        except PluginError:
            raise
        except Exception:
            raise _failure("hbtv_request_failed", "HBTV upstream response failed") from None
        return StreamDescriptor.hls(url, ttl_seconds=3600, volatile_url=True, requires_proxy=False)


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/hbtv")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "hbtv", Provider()
    ).run()


if __name__ == "__main__":
    main()
