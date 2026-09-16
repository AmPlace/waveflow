#!/usr/bin/env python3
"""FJTV Provider implemented with the public WaveFlow Plugin SDK."""
from __future__ import annotations

from typing import Any

from waveflow_plugin_sdk import (
    NotLive, PluginApplication, PluginError, ResolveContext, StreamDescriptor,
    TVProvider, TVReference, TemporaryFailure,
)

UA = "okhttp/3.10.0.7"
REFERER_FJTV = "https://www.fjtv.net/"
REFERER_XMTV = "https://www.xmtv.cn/"
REFERER_JJ = "https://www.ijjnews.com/"
REFERER_SS = "https://www.chinashishi.net/"
MAPI_PLUS = "https://mapi-plus.fjtv.net/api/open/haibo8/tv_channel_list.php?sort_id=665226484646215680"
KXM = ("https://mapi1.kxm.xmtv.cn/api/v1/channel.php?node_id=1&appkey=45920796f66247395069ee6f45d99c5e"
       "&appid=m2ohvecbng7leb8ixo&client_type=iOS&device_token=d25800aafc08fc01eabdf4757762e03c"
       "&version=4.6.4&app_version=4.6.4&avos_device_token=d25800aafc08fc01eabdf4757762e03c"
       "&client_id_ios=1211f05806bca2f5e7d93bc0d1d6f75d&location_city=%E5%8E%A6%E9%97%A8&language=Chinese")


def _channel_info(channel_id: str) -> str:
    return f"https://live.fjtv.net/m2o/channel/channel_info.php?channel_id={channel_id}"


CHANNELS: dict[str, tuple[str, list[Any], str, str]] = {
    "fjzh": (_channel_info("665248990102917120"), [0, "m3u8"], "福建综合", REFERER_FJTV),
    "fjdn": (_channel_info("665248966136664064"), [0, "m3u8"], "东南卫视", REFERER_FJTV),
    "fjnews": (_channel_info("665248914378952704"), [0, "m3u8"], "福建新闻", REFERER_FJTV),
    "fjculture": (_channel_info("665248752898248704"), [0, "m3u8"], "福建文旅体育", REFERER_FJTV),
    "fjkid": (_channel_info("665248553475870720"), [0, "m3u8"], "福建少儿", REFERER_FJTV),
    "fjhxws": (_channel_info("665248523855695872"), [0, "m3u8"], "海峡卫视", REFERER_FJTV),
    **{key: (MAPI_PLUS, [index, "topic_camera", 0, "streams", 0, "hls"], name, REFERER_FJTV)
       for index, (key, name) in enumerate((("xmws", "厦门卫视"), ("fznews", "福州新闻综合"),
           ("zznews", "漳州新闻综合"), ("smtv", "三明综合"), ("qznews", "泉州新闻综合"),
           ("nptv", "南平综合"), ("lytv", "龙岩综合"), ("puttv", "莆田新闻综合"),
           ("pttv", "平潭综合"), ("ndtv", "宁德新闻综合")))},
    "xmws-xmtv": (KXM, [0, "m3u8"], "厦门卫视(XMTV)", REFERER_XMTV),
    "xmtv-1": (KXM, [1, "m3u8"], "厦视一套", REFERER_XMTV),
    "xmtv-2": (KXM, [2, "m3u8"], "厦视二套", REFERER_XMTV),
    "xmtv-mobile": (KXM, [3, "m3u8"], "厦门电视台移动电视", REFERER_XMTV),
    "jjtv": ("https://mapi.ijjnews.com/cloudlive-manage-mapi/api/topic/detail?preview=&id=657527900022525952&app_secret=31ca2c44a23e6cd127ddee647fa9cf92&tenant_id=0&company_id=1067&lang_type=zh", ["topic_camera", 0, "streams", 0, "hls"], "晋江综合", REFERER_JJ),
    "sstv": ("https://mapi-new.chinashishi.net/cloudlive-manage-mapi/api/topic/detail?preview=&id=662611405685436416&app_secret=5c03f9843fa239c14b52222e83098919&tenant_id=0&company_id=492&lang_type=zh", ["topic_camera", 0, "streams", 0, "hls"], "石狮新闻综合", REFERER_SS),
}
ALIASES = {"fjzhpd": "fjzh", "fjdnws": "fjdn"}


def walk(value: Any, path: list[Any]) -> Any:
    for key in path:
        value = value[key]
    return value


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        key = ALIASES.get(reference.resource_id.strip("/").lower(), reference.resource_id.strip("/").lower())
        entry = CHANNELS.get(key)
        if entry is None:
            raise PluginError("RESOURCE_NOT_FOUND", "FJTV channel is not supported")
        url, path, _name, referer = entry
        try:
            response = context.capabilities.managed_http(url, headers={
                "Accept": "application/json, text/plain, */*", "User-Agent": UA,
            }, response_mode="json", timeout=12)
            data = response.body
            if isinstance(data, dict) and data.get("error_code", 0) not in (0, None):
                raise TemporaryFailure("FJTV upstream request failed")
            play_url = walk(data, path)
        except PluginError as exc:
            if exc.code in {"CAPABILITY_DENIED", "PLUGIN_TIMEOUT", "AUTH_FAILED", "RATE_LIMITED",
                            "TEMPORARY_UPSTREAM_FAILURE"}:
                raise
            raise TemporaryFailure("FJTV upstream response was invalid") from None
        except (KeyError, IndexError, TypeError):
            raise TemporaryFailure("FJTV upstream response was malformed") from None
        if not isinstance(play_url, str) or not play_url.startswith(("http://", "https://")):
            raise NotLive("FJTV channel has no playable stream")
        return StreamDescriptor.hls(play_url, headers={"Referer": referer}, ttl_seconds=180,
                                    volatile_url=True, requires_proxy=False)


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/fjtv")
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "fjtv", Provider()
    ).run()


if __name__ == "__main__":
    main()
