#!/usr/bin/env python3
"""Official multi-scheme StreamGet Provider Plugin.

The adapter table below is the provider boundary for this Plugin.  It is
deliberately independent from WaveFlow's legacy adapters: the Plugin owns the
StreamGet URL/quality/result conventions and the Core only sees the public TV
Provider contract.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from streamget import (
    AcfunLiveStream,
    BaiduLiveStream,
    BilibiliLiveStream,
    BigoLiveStream,
    BluedLiveStream,
    ChangliaoLiveStream,
    ChzzkLiveStream,
    DouyuLiveStream,
    DouyinLiveStream,
    FaceitLiveStream,
    FlexTVLiveStream,
    HuajiaoLiveStream,
    HuamaoLiveStream,
    InkeLiveStream,
    JDLiveStream,
    KugouLiveStream,
    LangLiveStream,
    LaixiuLiveStream,
    LianJieLiveStream,
    LookLiveStream,
    MaoerLiveStream,
    NeteaseLiveStream,
    PandaLiveStream,
    PicartoLiveStream,
    PopkonTVLiveStream,
    ShopeeLiveStream,
    ShowRoomLiveStream,
    SixRoomLiveStream,
    SoopLiveStream,
    TwitchLiveStream,
    TwitCastingLiveStream,
    TikTokLiveStream,
    WeiboLiveStream,
    YYLiveStream,
    ZhihuLiveStream,
)
from waveflow_plugin_sdk import (
    InvalidResource,
    PluginApplication,
    PluginError,
    ResolveContext,
    StreamDescriptor,
    TVProvider,
    TVReference,
    TemporaryFailure,
    VisualMetadataProvider,
    VisualMetadata,
)


TTL_SECONDS = 30 * 60
DIRECT_NETWORK_PERMISSION = "network.direct"


@dataclass(frozen=True)
class ProviderSpec:
    stream_class: type | None
    url_builder: Callable[[str], str]
    quality: str = "OD"
    play_fields: tuple[str, ...] = ("flv_url", "m3u8_url", "record_url")
    transport: str | None = None
    volatile_url: bool = True
    ttl_seconds: int = TTL_SECONDS
    play_url_selector: Callable[[Mapping[str, Any]], str | None] | None = None
    transport_selector: Callable[[Mapping[str, Any], str], str] | None = None
    fetcher: Callable[[str], Any] | None = None
    resource_normalizer: Callable[[str], str] | None = None


def _url(template: str) -> Callable[[str], str]:
    return lambda room_id: template.format(room_id=room_id)


def _weibo_url(room_id: str) -> str:
    if room_id.startswith("1022:"):
        return f"https://weibo.com/show/{room_id}"
    if room_id.isdigit():
        return f"https://weibo.com/u/{room_id}"
    return f"https://weibo.com/show/{room_id}"


# Douyu returns a primary URL plus optional CDN fallbacks.  Keep this policy
# inside the Plugin rather than importing the legacy adapter: the Plugin must
# remain independently runnable in its isolated StreamGet environment.
_DOUYU_PREFERRED_CDN = "douyucdn2.cn"
_DOUYU_BLOCKED_CDN_HOSTS = {"edgesrv.com"}


def _douyu_cdn_score(url: str) -> int:
    """Lower score means higher priority; -1 is a known-bad endpoint."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return 100
    if any(blocked in host for blocked in _DOUYU_BLOCKED_CDN_HOSTS):
        if ":8443" in url:
            return -1
        return 50
    if _DOUYU_PREFERRED_CDN in host:
        return 0
    return 10


def _select_douyu_cdn(urls: list[str]) -> str | None:
    if not urls:
        return None
    scored = [(url, _douyu_cdn_score(url)) for url in urls]
    candidates = [(url, score) for url, score in scored if score >= 0]
    if not candidates:
        # Preserve legacy last-resort behavior if every advertised endpoint
        # is an edgesrv:8443 URL.
        return urls[0]
    candidates.sort(key=lambda item: item[1])
    return candidates[0][0]


def _douyu_play_url(result: Mapping[str, Any]) -> str | None:
    urls: list[str] = []
    primary_url = result.get("flv_url") or result.get("m3u8_url") or ""
    if isinstance(primary_url, str) and primary_url:
        urls.append(primary_url)
    extra = result.get("extra")
    backup_urls = extra.get("backup_url_list") if isinstance(extra, Mapping) else []
    if isinstance(backup_urls, (list, tuple)):
        urls.extend(url for url in backup_urls if isinstance(url, str) and url)
    return _select_douyu_cdn(urls)


REDNOTE_MOBILE_HEADERS = {
    "user-agent": "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))",
    "xy-common-params": "platform=iOS&sid=session.1722166379345546829388",
    "referer": "https://app.xhs.cn/",
}
_REDNOTE_INITIAL_STATE = re.compile(r"<script>window\.__INITIAL_STATE__=(.*?)</script>")


def _redbook_target(resource_id: str) -> str:
    """Preserve room-id references and accept the legacy link form."""
    value = resource_id.strip("/")
    if value.startswith(("http://", "https://")):
        return value
    if "xhslink.com/" in value:
        return f"https://{value}"
    return f"https://www.xiaohongshu.com/livestream/{value}"


async def _redbook_request(url: str) -> tuple[str, str]:
    """Fetch one page in the Plugin's direct-network boundary.

    This intentionally does not use Core HTTP/session state or mutate
    StreamGet classes.  The client and headers remain local to the Plugin.
    """
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, verify=False, http2=True) as client:
        response = await client.get(url, headers=REDNOTE_MOBILE_HEADERS)
        response.raise_for_status()
        return response.text, str(response.url)


def _redbook_query_value(url: str, name: str) -> str | None:
    return parse_qs(urlparse(url).query).get(name, [None])[0]


def _parse_redbook_initial_state(page: str) -> dict[str, Any] | None:
    match = _REDNOTE_INITIAL_STATE.search(page)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1).replace("undefined", "null"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Redbook INITIAL_STATE is malformed") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Redbook INITIAL_STATE is not an object")
    return parsed


async def _fetch_redbook(url: str) -> dict[str, Any]:
    """Own the Redbook parser instead of monkey-patching RedNoteLiveStream."""
    resolved_url = url
    if "xhslink.com" in resolved_url:
        _unused_page, resolved_url = await _redbook_request(resolved_url)

    host_id = _redbook_query_value(resolved_url, "host_id")
    user_match = re.search(r"/user/profile/(.*?)(?=/|\?|$)", resolved_url)
    user_id = user_match.group(1) if user_match else host_id
    result: dict[str, Any] = {"anchor_name": "", "is_live": False, "live_url": resolved_url}
    page, _final_url = await _redbook_request(resolved_url)
    initial_state = _parse_redbook_initial_state(page)

    if initial_state and initial_state.get("liveStream"):
        stream_data = initial_state["liveStream"]
        if not isinstance(stream_data, dict):
            raise ValueError("Redbook liveStream is malformed")
        if stream_data.get("liveStatus") == "success":
            room_info = stream_data["roomData"]["roomInfo"]
            title = room_info.get("roomTitle")
            if title and "回放" not in title:
                live_link = room_info["deeplink"]
                anchor_name = _redbook_query_value(live_link, "host_nickname")
                flv_url = _redbook_query_value(live_link, "flvUrl")
                flv_url = unquote(flv_url) if flv_url else flv_url

                if flv_url and "live/" in flv_url:
                    room_id = flv_url.split("live/", 1)[1].split(".", 1)[0]
                    flv_url = f"http://live-source-play.xhscdn.com/live/{room_id}.flv"
                    m3u8_url = flv_url.replace(".flv", ".m3u8")
                    result |= {
                        "anchor_name": anchor_name or "",
                        "is_live": True,
                        "title": title,
                        "flv_url": flv_url,
                        "m3u8_url": m3u8_url,
                        "record_url": flv_url,
                    }
                    return result

    profile_url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
    profile_page, _profile_final_url = await _redbook_request(profile_url)
    anchor_name = re.search(r"<title>@(.*?) 的个人主页</title>", profile_page)
    if anchor_name:
        result["anchor_name"] = anchor_name.group(1)
    return result


TIKTOK_TTL_SECONDS = 12 * 24 * 60 * 60


def _tiktok_resource(resource_id: str) -> str:
    return resource_id.strip("/").lstrip("@")


async def _fetch_tiktok(url: str) -> dict[str, Any]:
    """Use StreamGet's public TikTok app-data path without legacy code."""
    live = TikTokLiveStream(cookies="")
    data = await live.fetch_app_stream_data(url)
    stream_obj = await live.fetch_stream_url(data, "OD")
    decoded = json.loads(stream_obj.to_json())
    if not isinstance(decoded, dict):
        raise ValueError("StreamGet returned a non-object result")
    return decoded


# This is the complete bundle boundary.  Only wheel-compatible StreamGet
# providers with deterministic runtime dependencies belong in this Plugin.
PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "yy": ProviderSpec(YYLiveStream, _url("https://www.yy.com/{room_id}/{room_id}"),
                       play_fields=("flv_url", "record_url"), transport="http_flv"),
    "bigo": ProviderSpec(BigoLiveStream, _url("https://bigo.tv/{room_id}"),
                         play_fields=("m3u8_url", "record_url"), transport="hls"),
    "blued": ProviderSpec(BluedLiveStream, _url("https://blued.com/live/{room_id}"),
                          play_fields=("m3u8_url", "record_url"), transport="hls"),
    "soop": ProviderSpec(SoopLiveStream, _url("https://play.sooplive.com/{room_id}"),
                         play_fields=("m3u8_url", "record_url"), transport="hls"),
    "netease": ProviderSpec(NeteaseLiveStream, _url("https://cc.163.com/{room_id}"), quality="blueray"),
    "pandatv": ProviderSpec(PandaLiveStream, _url("https://www.pandalive.co.kr/{room_id}")),
    "maoer": ProviderSpec(MaoerLiveStream, _url("https://fm.missevan.com/{room_id}")),
    "look": ProviderSpec(LookLiveStream, _url("https://www.look.163.com/live?id={room_id}&")),
    "flextv": ProviderSpec(FlexTVLiveStream, _url("https://www.ttinglive.com/channels/{room_id}/live")),
    "popkontv": ProviderSpec(PopkonTVLiveStream, _url("https://www.popkontv.com/live/view?castId={room_id}")),
    "twitcasting": ProviderSpec(TwitCastingLiveStream, _url("https://twitcasting.tv/{room_id}")),
    "baidu": ProviderSpec(BaiduLiveStream, _url("https://live.baidu.com/?room_id={room_id}&")),
    "weibo": ProviderSpec(WeiboLiveStream, _weibo_url),
    "kugou": ProviderSpec(KugouLiveStream, _url("https://fanxing.kugou.com/{room_id}")),
    "twitch": ProviderSpec(TwitchLiveStream, _url("https://www.twitch.tv/{room_id}")),
    "huajiao": ProviderSpec(HuajiaoLiveStream, _url("https://www.huajiao.com/l/{room_id}")),
    "showroom": ProviderSpec(ShowRoomLiveStream, _url("https://www.showroom-live.com/room/profile?room_id={room_id}")),
    "inke": ProviderSpec(InkeLiveStream, _url("https://webapi.busi.inke.cn/web/live_share_pc?id={room_id}")),
    "acfun": ProviderSpec(AcfunLiveStream, _url("https://live.acfun.cn/{room_id}")),
    "zhihu": ProviderSpec(ZhihuLiveStream, _url("https://www.zhihu.com/theater/{room_id}")),
    "chzzk": ProviderSpec(ChzzkLiveStream, _url("https://chzzk.naver.com/live/{room_id}")),
    "live17": ProviderSpec(LangLiveStream, _url("https://www.lang.live/{room_id}")),
    "langlive": ProviderSpec(LangLiveStream, _url("https://www.lang.live/{room_id}")),
    "changliao": ProviderSpec(ChangliaoLiveStream, _url("https://wap.tlclw.com/{room_id}")),
    "jd": ProviderSpec(JDLiveStream, _url("https://lives.jd.com/{room_id}")),
    "faceit": ProviderSpec(FaceitLiveStream, _url("https://www.faceit.com/players/{room_id}/stream")),
    "lianjie": ProviderSpec(LianJieLiveStream, _url("https://www.lailianjie.com/{room_id}")),
    "sixroom": ProviderSpec(SixRoomLiveStream, _url("https://v.6.cn/{room_id}")),
    "huamao": ProviderSpec(HuamaoLiveStream, _url("https://www.huamao.com/{room_id}")),
    "shopee": ProviderSpec(ShopeeLiveStream, _url("https://live.shopee.com/{room_id}")),
    "laixiu": ProviderSpec(LaixiuLiveStream, _url("https://www.laixiu.com/{room_id}")),
    "picarto": ProviderSpec(PicartoLiveStream, _url("https://picarto.tv/{room_id}")),
    # Keep the legacy Bilibili semantics: the provider always advertises FLV
    # transport, selects FLV before record_url, and leaves volatile_url at
    # the bridge's legacy false/default value.
    "bilibili": ProviderSpec(
        BilibiliLiveStream,
        _url("https://live.bilibili.com/{room_id}"),
        play_fields=("flv_url", "record_url"),
        transport="http_flv",
        volatile_url=False,
    ),
    "douyu": ProviderSpec(
        DouyuLiveStream,
        _url("https://www.douyu.com/{room_id}"),
        play_url_selector=_douyu_play_url,
        ttl_seconds=0,
    ),
    "douyin": ProviderSpec(
        DouyinLiveStream,
        _url("https://live.douyin.com/{room_id}"),
        play_fields=("m3u8_url", "flv_url"),
        volatile_url=False,
        transport_selector=lambda result, _play_url: (
            "hls" if result.get("m3u8_url") else "http_flv"
        ),
    ),
    "redbook": ProviderSpec(
        None,
        _redbook_target,
        play_fields=("m3u8_url", "flv_url"),
        volatile_url=False,
        transport_selector=lambda result, _play_url: (
            "hls" if result.get("m3u8_url") else "http_flv"
        ),
        fetcher=_fetch_redbook,
    ),
    "tiktok": ProviderSpec(
        TikTokLiveStream,
        _url("https://www.tiktok.com/@{room_id}/live"),
        play_fields=("flv_url", "m3u8_url"),
        volatile_url=False,
        ttl_seconds=TIKTOK_TTL_SECONDS,
        fetcher=_fetch_tiktok,
        resource_normalizer=_tiktok_resource,
    ),
}


class StreamGetProvider(TVProvider):
    """Resolve one normalized TV reference with the StreamGet API."""

    def __init__(self, specs: Mapping[str, ProviderSpec] = PROVIDER_SPECS):
        self._specs = specs

    async def _fetch(self, spec: ProviderSpec, url: str) -> dict[str, Any]:
        if spec.fetcher is not None:
            result = await spec.fetcher(url)
            if not isinstance(result, dict):
                raise ValueError("StreamGet returned a non-object result")
            return result
        if spec.stream_class is None:
            raise ValueError("StreamGet provider has no fetch implementation")
        live = spec.stream_class(cookies="")
        data = await live.fetch_web_stream_data(url)
        stream_obj = await live.fetch_stream_url(data, spec.quality)
        decoded = json.loads(stream_obj.to_json())
        if not isinstance(decoded, dict):
            raise ValueError("StreamGet returned a non-object result")
        return decoded

    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        context.raise_if_cancelled()
        spec = self._specs.get(reference.scheme)
        room_id = (
            spec.resource_normalizer(reference.resource_id)
            if spec is not None and spec.resource_normalizer is not None
            else reference.resource_id.strip("/")
        )
        if spec is None or not room_id:
            raise InvalidResource("StreamGet resource is not supported")

        try:
            result = asyncio.run(self._fetch(spec, spec.url_builder(room_id)))
        except PluginError:
            raise
        except Exception:
            raise TemporaryFailure("StreamGet upstream resolution failed") from None
        context.raise_if_cancelled()

        if not result.get("is_live"):
            raise PluginError("NOT_LIVE", "StreamGet resource is not live", retryable=False)

        if spec.play_url_selector is not None:
            play_url = spec.play_url_selector(result)
        else:
            play_url = next(
                (result.get(field) for field in spec.play_fields
                 if isinstance(result.get(field), str) and result.get(field)),
                None,
            )
        if not play_url:
            raise TemporaryFailure("StreamGet returned no playable URL")
        transport = spec.transport or (
            spec.transport_selector(result, play_url)
            if spec.transport_selector is not None
            else ("http_flv" if result.get("flv_url") else "hls")
        )
        return StreamDescriptor(
            url=play_url,
            transport=transport,
            headers={},
            ttl_seconds=spec.ttl_seconds,
            expires_at=None,
            volatile_url=spec.volatile_url,
            requires_proxy=False,
            provider_diagnostics=(
                {
                    "provider": "redbook",
                    "anchor_name": str(result.get("anchor_name") or ""),
                    "live_state": "live" if result.get("is_live") else "not_live",
                }
                if reference.scheme == "redbook" else {}
            ),
        )


class StreamGetVisualProvider(VisualMetadataProvider):
    """Optional visual metadata for the Bundle's explicitly supported schemes."""

    def visual_metadata(self, reference: TVReference, context: ResolveContext) -> VisualMetadata:
        context.raise_if_cancelled()
        room_id = reference.resource_id.strip("/")
        if not room_id:
            raise InvalidResource("StreamGet resource is not supported")
        try:
            if reference.scheme == "bilibili":
                return asyncio.run(self._bilibili(room_id))
            if reference.scheme == "douyu":
                return asyncio.run(self._douyu(room_id))
        except PluginError:
            raise
        except Exception as exc:
            raise TemporaryFailure("StreamGet visual metadata request failed") from exc
        raise PluginError("RESOURCE_NOT_FOUND", "StreamGet visual metadata is not supported")

    @staticmethod
    async def _bilibili(room_id: str) -> VisualMetadata:
        api = f"https://api.live.bilibili.com/xlive/web-room/v1/index/getH5InfoByRoom?room_id={room_id}"
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(
                api,
                headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://live.bilibili.com/{room_id}"},
            )
            response.raise_for_status()
        data = (response.json() or {}).get("data") or {}
        room_info = data.get("room_info") or {}
        base_info = ((data.get("anchor_info") or {}).get("base_info")) or {}
        return VisualMetadata(
            avatar_url=str(base_info.get("face") or "").strip(),
            cover_url=str(room_info.get("cover") or room_info.get("keyframe") or "").strip(),
            is_live=int(room_info.get("live_status") or 0) == 1,
            title=str(room_info.get("title") or "").strip(),
            owner_name=str(base_info.get("uname") or "").strip(),
            ttl_seconds=300,
            cover_role="live",
        )

    @staticmethod
    async def _douyu(room_id: str) -> VisualMetadata:
        api = f"https://www.douyu.com/betard/{room_id}"
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(
                api,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyu.com/"},
            )
            response.raise_for_status()
        room = (response.json() or {}).get("room") or {}
        avatar_field = room.get("avatar")
        if isinstance(avatar_field, Mapping):
            avatar = avatar_field.get("big") or avatar_field.get("middle") or ""
        else:
            avatar = avatar_field or room.get("avatar_mid") or ""
        return VisualMetadata(
            avatar_url=str(avatar).strip(),
            cover_url=str(room.get("room_pic") or "").strip(),
            is_live=int(room.get("show_status") or 0) == 1 and int(room.get("videoLoop") or 0) == 0,
            title=str(room.get("room_name") or "").strip(),
            owner_name=str(room.get("owner_name") or "").strip(),
            ttl_seconds=300,
            cover_role="live",
        )


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/streamget-providers")
    app = PluginApplication(identity=identity, version=version, permissions=["network"])
    provider = StreamGetProvider()
    visual_provider = StreamGetVisualProvider()
    for scheme in PROVIDER_SPECS:
        app.register_tv(scheme, provider)
    for scheme in ("bilibili", "douyu"):
        app.register_tv_visual(scheme, visual_provider)
    app.run()


if __name__ == "__main__":
    main()
