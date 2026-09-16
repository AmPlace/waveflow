import asyncio
from typing import Any

import httpx
from streamlink import Streamlink
from streamlink.exceptions import NoPluginError, NoStreamsError, PluginError, StreamError, StreamlinkError

from . import AdapterRequest, AdapterResolveError


ADAPTER_CAPABILITIES = {"cover": True}


def _infer_source_type(url: str) -> str:
    lower = url.lower().split("?", 1)[0]
    if lower.endswith(".flv"):
        return "http_flv"
    if lower.endswith(".m3u8"):
        return "hls"
    if lower.endswith(".ts"):
        return "mpegts"
    return "hls"


def _pick_stream(streams: dict[str, Any], preferred_cdn: str = "tx") -> tuple[str, Any] | tuple[None, None]:
    preferred_cdn = (preferred_cdn or "tx").lower()
    preferred_names = [
        f"{preferred_cdn}_source",
        "tx_source",
        "hs_source",
        "al_source",
        "best",
    ]
    for name in preferred_names:
        stream = streams.get(name)
        if stream is not None:
            return name, stream
    return next(iter(streams.items()), (None, None))


def _resolve_huya_with_streamlink(huya_url: str, preferred_cdn: str = "tx") -> dict[str, Any]:
    session = Streamlink()
    streams = session.streams(huya_url)
    stream_name, stream = _pick_stream(streams, preferred_cdn)
    if stream is None:
        raise NoStreamsError(huya_url)

    play_url = stream.to_url()
    if not play_url:
        raise StreamError("streamlink did not return a stream URL")

    return {
        "url": play_url,
        "source_type": _infer_source_type(play_url),
        "stream_name": stream_name or "best",
        "available_streams": list(streams.keys()),
    }


async def resolve_huya(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    room_id = request.resource_id.strip("/")
    if not room_id:
        raise AdapterResolveError("invalid_huya_room_id", "虎牙房间号不能为空")

    huya_url = f"https://www.huya.com/{room_id}"
    preferred_cdn = (request.query.get("cdn") or ["tx"])[0]

    try:
        result = await asyncio.to_thread(_resolve_huya_with_streamlink, huya_url, preferred_cdn)
    except NoStreamsError as exc:
        raise AdapterResolveError(
            "huya_not_live",
            "该虎牙主播未开播或没有可播放流",
            status_code=502,
            retryable=False,
        ) from exc
    except (NoPluginError, PluginError, StreamError, StreamlinkError, OSError) as exc:
        raise AdapterResolveError(
            "huya_resolve_failed",
            f"虎牙解析失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    return {
        "ok": True,
        "adapter": "huya",
        "source_type": result["source_type"],
        "url": result["url"],
        "direct_playable": True,
        "requires_proxy": False,
        "headers": {
            "Origin": "https://www.huya.com",
            "Referer": "https://www.huya.com/",
        },
        "ttl": 60,
        "expires_at": None,
        "warnings": [],
        "stream_name": result.get("stream_name", "best"),
        "available_streams": result.get("available_streams", []),
    }
