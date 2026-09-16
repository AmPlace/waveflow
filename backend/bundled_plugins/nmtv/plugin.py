#!/usr/bin/env python3
"""NMTV Provider implemented with the SDK and isolated xxtea dependency."""
import base64
import json
import xxtea

from waveflow_plugin_sdk import InvalidResource, PluginApplication, ResolveContext, StreamDescriptor, TVProvider, TVReference, TemporaryFailure

API_URL = "https://api-bt.nmtv.cn/broadcast/list"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REFERER = "https://www.nmtv.cn/"
KEY = b"5b28bae827e651b3"
CHANNELS = {"nmws": 262, "nmmyws": 126, "nmxwzh": 127, "nmjjsh": 128, "nmse": 129,
    "nmwtyl": 130, "nmnm": 131, "nmwh": 132, "hhht1": 141, "xlgl1": 156,
    "als1": 157, "byle1": 158, "erds1": 159, "cf1": 161, "tl1": 163,
    "wlcb1": 164, "wh1": 165, "hlbe1": 166, "xa1": 167, "bt1": 168}


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        resource = reference.resource_id.strip("/").lower()
        target_id = CHANNELS.get(resource)
        if target_id is None:
            if resource.isdigit(): target_id = int(resource)
            else: raise InvalidResource("NMTV channel is not supported")
        try:
            response = context.capabilities.managed_http(API_URL, query={"size": "100", "type": "1"},
                headers={"User-Agent": USER_AGENT, "Referer": REFERER}, response_mode="text", timeout=10)
            raw = str(response.body or "").strip().strip('"')
            text = xxtea.decrypt(base64.b64decode(raw), KEY, padding=False).decode("utf-8", errors="ignore")
            data = json.JSONDecoder().raw_decode(text)[0]
            target = next((v.get("data", {}) for v in data.get("data", []) if v.get("data", {}).get("id") == target_id), None)
            streams = target.get("streamUrls", []) if target else []
            url = streams[0] if streams else ""
            if not isinstance(url, str) or not url.startswith(("http://", "https://")): raise ValueError("missing stream")
        except Exception as exc:
            from waveflow_plugin_sdk import PluginError
            if isinstance(exc, PluginError): raise
            raise TemporaryFailure("NMTV response decode failed") from None
        return StreamDescriptor.hls(url, headers={"User-Agent": USER_AGENT, "Referer": REFERER},
            ttl_seconds=1800, volatile_url=True, provider_diagnostics={"dependency_origin": str(xxtea.__file__)})


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/nmtv")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "nmtv", Provider()
    ).run()


if __name__ == "__main__":
    main()
