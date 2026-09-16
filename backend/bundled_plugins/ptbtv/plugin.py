#!/usr/bin/env python3
"""PTBTV Provider implemented with SDK, direct curl-cffi, and managed fallback."""
import asyncio
import hashlib
import json
import sys
import time
from typing import Callable

from curl_cffi.requests import AsyncSession
from waveflow_plugin_sdk import InvalidResource, PluginApplication, ResolveContext, StreamDescriptor, TVProvider, TVReference, TemporaryFailure

API_KEY = "f33ba15effa5c10e873bf3842afb46a6"
API_SECRET = "YWFkZDYwMjNkNzMzNzUwZWJjYjE4NWFjZjY3YmQyYzE="
API_VERSION = "1.0.0"
API_URL = "https://www.ptbtv.com/m2o/channel/channel_info.php"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
CHANNELS = {"1": {"channel_id": "4", "referer": "https://www.ptbtv.com/live/pt1t/"},
            "2": {"channel_id": "5", "referer": "https://www.ptbtv.com/live/pt2t/"},
            "xianyou": {"channel_id": "6", "referer": "https://www.ptbtv.com/live/xyds/"}}
ALIASES = {"ptbtv-1": "1", "ptbtv-2": "2", "pt1": "1", "pt2": "2", "xianyoutv": "xianyou", "xy": "xianyou"}


def build_headers(referer: str, *, now: Callable[[], float] = time.time) -> dict[str, str]:
    timestamp = str(int(now()))
    signature = hashlib.md5(f"{API_KEY}&{API_SECRET}&{API_VERSION}&{timestamp}".encode()).hexdigest()
    return {"Accept": "application/json, text/javascript, */*; q=0.01", "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache", "Origin": "https://www.ptbtv.com", "Pragma": "no-cache",
        "Referer": referer, "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
        "User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest", "X-API-TIMESTAMP": timestamp,
        "X-API-KEY": API_KEY, "X-AUTH-TYPE": "md5", "X-API-VERSION": API_VERSION, "X-API-SIGNATURE": signature}


async def direct_fetch(channel_id: str, headers: dict[str, str], *, session_factory=AsyncSession):
    try:
        async with session_factory(impersonate="chrome", timeout=10) as session:
            response = await session.get(API_URL, params={"channel_id": str(channel_id)}, headers=headers)
        return response.status_code, response.text
    except Exception:
        return None


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        resource = ALIASES.get(reference.resource_id.strip("/").lower(), reference.resource_id.strip("/").lower())
        channel = CHANNELS.get(resource)
        if channel is None: raise InvalidResource("PTBTV channel is not supported")
        headers = build_headers(channel["referer"])
        try:
            direct = None if "--fixture-direct-unavailable" in sys.argv else asyncio.run(direct_fetch(channel["channel_id"], headers))
            text = direct[1] if direct is not None and direct[0] == 200 and direct[1] else None
            if text is None:
                text = str(context.capabilities.managed_http(API_URL, query={"channel_id": channel["channel_id"]},
                    headers=headers, response_mode="text", timeout=10).body or "")
            data = json.loads(text); url = data[0]["m3u8"]
            if not isinstance(url, str) or not url.startswith(("http://", "https://")): raise ValueError("missing stream")
        except Exception as exc:
            from waveflow_plugin_sdk import PluginError
            if isinstance(exc, PluginError):
                if exc.code in {"CAPABILITY_DENIED", "PLUGIN_TIMEOUT", "RATE_LIMITED"}:
                    raise
                raise TemporaryFailure("PTBTV upstream request failed") from None
            raise TemporaryFailure("PTBTV response structure is invalid") from None
        return StreamDescriptor.hls(url, ttl_seconds=180, volatile_url=True,
            provider_diagnostics={"dependency_origin": str(sys.modules["curl_cffi"].__file__)})


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/ptbtv")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "ptbtv", Provider()
    ).run()


if __name__ == "__main__":
    main()
