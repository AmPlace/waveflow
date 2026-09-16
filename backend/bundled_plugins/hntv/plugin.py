#!/usr/bin/env python3
"""Independent HNTV Provider using the generic managed HTTP capability."""
from __future__ import annotations

import hashlib
import time
from waveflow_plugin_sdk import InvalidResource, PluginApplication, PluginError, ResolveContext, StreamDescriptor, TVProvider, TVReference


HNTV_CHANNELS = {
    "hnws": {"id": 145, "name": "河南卫视"}, "hnds": {"id": 141, "name": "河南都市"},
    "hnms": {"id": 146, "name": "河南民生"}, "hmfz": {"id": 147, "name": "河南法治"},
    "hndsj": {"id": 148, "name": "河南电视剧"}, "hnxw": {"id": 149, "name": "河南新闻"},
    "htgw": {"id": 150, "name": "欢腾购物"}, "hngg": {"id": 151, "name": "河南公共"},
    "hnxc": {"id": 152, "name": "河南乡村"}, "hngj": {"id": 153, "name": "河南国际"},
    "hnly": {"id": 154, "name": "河南梨园"}, "wwbk": {"id": 155, "name": "文物宝库"},
    "wspd": {"id": 156, "name": "武术世界"}, "jczy": {"id": 157, "name": "睛彩中原"},
    "ydxj": {"id": 163, "name": "移动戏曲"}, "xsj": {"id": 183, "name": "象视界"},
    "gxpd": {"id": 194, "name": "国学频道"},
    "zz1": {"id": 197, "name": "郑州新闻综合"}, "kf1": {"id": 198, "name": "开封新闻综合"},
    "ly1": {"id": 204, "name": "洛阳新闻综合"}, "pds1": {"id": 205, "name": "平顶山新闻综合"},
    "ay1": {"id": 206, "name": "安阳新闻综合"}, "hb1": {"id": 207, "name": "鹤壁新闻综合"},
    "xx1": {"id": 208, "name": "新乡新闻综合"}, "jz1": {"id": 209, "name": "焦作新闻综合"},
    "py1": {"id": 219, "name": "濮阳新闻综合"}, "xc1": {"id": 220, "name": "许昌新闻综合"},
    "lh1": {"id": 221, "name": "漯河新闻综合"}, "smx1": {"id": 222, "name": "三门峡新闻综合"},
    "ny1": {"id": 223, "name": "南阳新闻综合"}, "sq1": {"id": 224, "name": "商丘新闻综合"},
    "xy1": {"id": 225, "name": "信阳新闻综合"}, "zk1": {"id": 226, "name": "周口新闻综合"},
    "zmd1": {"id": 227, "name": "驻马店新闻综合"}, "jy1": {"id": 228, "name": "济源新闻综合"},
}
_SIGN_SALT = "6ca114a836ac7d73"
HNTV_API = "https://pubmod.hntv.tv/program/getAuth/channel/channelIds/1"


def _failure(code: str, message: str) -> PluginError:
    return PluginError("TEMPORARY_UPSTREAM_FAILURE", message, retryable=True,
                       category="provider", details={"provider_code": code})


def _target(resource: str) -> tuple[str, int]:
    key = resource.strip("/").lower()
    channel = HNTV_CHANNELS.get(key)
    if channel:
        return key, int(channel["id"])
    if key.isdigit():
        return key, int(key)
    raise InvalidResource(f"Unsupported HNTV channel: {key}")


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        channel_key, channel_id = _target(reference.resource_id)
        timestamp = str(int(time.time()))
        signature = hashlib.sha256((_SIGN_SALT + timestamp).encode()).hexdigest()
        try:
            response = context.capabilities.managed_http(
                f"{HNTV_API}/{channel_id}",
                headers={"timestamp": timestamp, "sign": signature},
                response_mode="json",
                timeout=15,
            )
            data = response.body
            if isinstance(data, str):
                import json
                try:
                    data = json.loads(data)
                except ValueError:
                    raise _failure("hntv_parse_failed", "HNTV upstream returned invalid JSON") from None
            if not isinstance(data, list) or not data or not isinstance(data[0], dict):
                raise _failure("hntv_channel_not_found", f"HNTV channel was not found: {channel_key}")
            item = data[0]
            play_url = None
            for field in ("video_streams", "streams"):
                urls = item.get(field)
                if isinstance(urls, list) and urls:
                    play_url = urls[0]
                    break
            if not isinstance(play_url, str) or not play_url:
                raise _failure("hntv_no_stream", "HNTV returned no playable stream")
            if play_url.startswith("rtmp"):
                # RTMP is a legacy-only fallback.  Plugin V1 deliberately
                # does not claim an unsupported transport.  The current
                # endpoint returns HTTP HLS playlists (verified against the
                # live API), so keep the dead fallback out of the Plugin
                # contract rather than adding a provider-specific capability.
                raise _failure("hntv_rtmp_unsupported", "HNTV returned the legacy RTMP fallback")
            descriptor = StreamDescriptor.hls(
                play_url, ttl_seconds=1800, volatile_url=True, requires_proxy=False,
            )
        except PluginError:
            raise
        except Exception:
            raise _failure("hntv_request_failed", "HNTV upstream response failed") from None
        return descriptor


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/hntv")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "hntv", Provider()
    ).run()


if __name__ == "__main__":
    main()
