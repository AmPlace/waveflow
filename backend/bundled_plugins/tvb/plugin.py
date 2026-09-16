#!/usr/bin/env python3
"""TVB's production-supported AES-128 HLS channels.

The myTV SUPER channels (Jade, TVB Plus, and Pearl) currently expose a
Widevine DASH checkout in the legacy adapter, but WaveFlow V1 has no DASH/
EME media path.  They are therefore rejected here rather than represented as
an apparently playable descriptor.
"""

import json
from typing import Any

from waveflow_plugin_sdk import InvalidResource, PluginApplication, PluginError, ResolveContext, StreamDescriptor, TVProvider, TVReference


NEWS_CHECKOUT_PREFIX = "https://inews-api.tvb.com/news/checkout/live/hd"
TVB_NEWS_CHANNELS = {
    "I-NEWS": "I-NEWS",
    "C": "C",
    "C2": "C2",
    "C3": "C3",
}
TVB_DASH_CHANNELS = {"JADE", "TVBPLUS", "PEARL"}
TVB_DASH_ALIASES = {"81": "JADE", "82": "TVBPLUS", "84": "PEARL", "J": "JADE", "B": "TVBPLUS", "P": "PEARL"}
PASSTHROUGH_CAPABILITY_ERRORS = {"CAPABILITY_DENIED", "PLUGIN_CANCELLED", "PLUGIN_TIMEOUT", "RATE_LIMITED"}


def _provider_failure(code: str, message: str, *, status: int | None = None) -> PluginError:
    details: dict[str, Any] = {"provider_code": code}
    if status is not None:
        details["status"] = status
    return PluginError("TEMPORARY_UPSTREAM_FAILURE", message, retryable=True, details=details)


def _parse_json(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        return body
    if isinstance(body, (bytes, bytearray, str)):
        try:
            value = json.loads(body)
        except (TypeError, ValueError):
            raise _provider_failure("tvb_parse_failed", "TVB news API returned non-JSON") from None
        if isinstance(value, dict):
            return value
    raise _provider_failure("tvb_parse_failed", "TVB news API returned an invalid payload")


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        context.raise_if_cancelled()
        resource = reference.resource_id.strip("/").upper()
        content_id = TVB_NEWS_CHANNELS.get(resource)
        if content_id is None:
            canonical = TVB_DASH_ALIASES.get(resource, resource)
            if canonical in TVB_DASH_CHANNELS:
                raise InvalidResource("TVB DASH/Widevine channels are unsupported by Plugin V1")
            raise InvalidResource("TVB channel is not supported")

        url = f"{NEWS_CHECKOUT_PREFIX}/ott_{content_id}_h264"
        try:
            response = context.capabilities.managed_http(
                url,
                query={"profile": "safari"},
                response_mode="text",
                timeout=15,
            )
        except PluginError as exc:
            if exc.code in PASSTHROUGH_CAPABILITY_ERRORS:
                raise
            raise _provider_failure("tvb_api_failed", "TVB news API request failed") from None
        except Exception:
            raise _provider_failure("tvb_api_failed", "TVB news API request failed") from None

        if not 200 <= int(response.status) < 300:
            raise _provider_failure("tvb_api_failed", "TVB news API request failed", status=int(response.status))
        data = _parse_json(response.body)
        meta = data.get("meta")
        if not isinstance(meta, dict) or meta.get("status") != "success":
            message = meta.get("error_message") if isinstance(meta, dict) else "unknown"
            raise _provider_failure("tvb_channel_failed", f"TVB news {content_id} failed: {message}")
        content = data.get("content")
        urls = content.get("url") if isinstance(content, dict) else None
        signed_url = urls.get("hd") or urls.get("sd") if isinstance(urls, dict) else None
        if not isinstance(signed_url, str) or not signed_url.startswith(("http://", "https://")):
            raise _provider_failure("tvb_no_url", "TVB news returned no playable URL")

        # The generic managed HTTP capability intentionally returns the signed
        # checkout URL.  Core media_proxy follows its redirect while fetching
        # the HLS playlist, preserving the legacy redirect/session behavior and
        # keeping the plugin's host permission limited to the API endpoint.
        return StreamDescriptor.hls(
            signed_url,
            headers={},
            ttl_seconds=1800,
            volatile_url=True,
            requires_proxy=True,
            warnings=["AES-128 encrypted HLS; key host requires the existing proxy path"],
        )


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/tvb")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "tvb", Provider()
    ).run()


if __name__ == "__main__":
    main()
