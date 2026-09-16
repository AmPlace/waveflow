import asyncio
import re
from typing import Any
from urllib.parse import unquote

import httpx
from streamlink import Streamlink
from streamlink.exceptions import NoPluginError, NoStreamsError, PluginError, StreamError, StreamlinkError

from . import AdapterRequest, AdapterResolveError
from m3u8_parser import parse_youtube_video_id


_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
_YOUTUBE_CHANNEL_ID_RE = re.compile(r"^UC[a-zA-Z0-9_-]{20,}$")
_YOUTUBE_CHANNEL_ID_PATTERNS = (
    re.compile(r'"externalId"\s*:\s*"(UC[a-zA-Z0-9_-]{20,})"'),
    re.compile(r'"channelId"\s*:\s*"(UC[a-zA-Z0-9_-]{20,})"'),
    re.compile(r'<meta\s+itemprop=["\']channelId["\']\s+content=["\'](UC[a-zA-Z0-9_-]{20,})["\']', re.I),
    re.compile(r'/channel/(UC[a-zA-Z0-9_-]{20,})'),
)
_YOUTUBE_VIDEO_ID_PATTERNS = (
    re.compile(r'<link\s+rel=["\']canonical["\']\s+href=["\']https?://(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})["\']', re.I),
    re.compile(r'"videoId"\s*:\s*"([a-zA-Z0-9_-]{11})"'),
)

_YOUTUBE_COMMAND_VIDEO_ID_RE = re.compile(r'window\[[\'"]ytCommand[\'"]\]\s*=\s*\{.*?"watchEndpoint"\s*:\s*\{.*?"videoId"\s*:\s*"([a-zA-Z0-9_-]{11})"', re.S)
YOUTUBE_STREAMLINK_TIMEOUT_SECONDS = 12.0


def _infer_source_type(url: str) -> str:
    lower_url = url.lower()
    lower_path = lower_url.split("?", 1)[0]
    if ".m3u8" in lower_path or "manifest/hls" in lower_url:
        return "hls"
    if lower_path.endswith(".flv"):
        return "http_flv"
    if lower_path.endswith(".ts"):
        return "mpegts"
    return ""


def _explicit_channel_id(value: str) -> str:
    raw = unquote((value or "").strip().strip("/"))
    if not raw:
        return ""
    parts = [part for part in raw.split("/") if part]
    if parts and _YOUTUBE_CHANNEL_ID_RE.fullmatch(parts[0]):
        return parts[0]
    if len(parts) >= 2 and parts[0] == "channel" and _YOUTUBE_CHANNEL_ID_RE.fullmatch(parts[1]):
        return parts[1]
    return ""


def _build_youtube_url(request: AdapterRequest) -> str:
    query_url = (request.query.get("url") or [""])[0].strip()
    if query_url:
        return query_url

    raw = unquote((request.resource_id or "").strip().strip("/"))
    if not raw:
        raise AdapterResolveError("invalid_youtube_url", "YouTube 地址不能为空")
    if raw.startswith(("http://", "https://")):
        return raw
    if _YOUTUBE_VIDEO_ID_RE.fullmatch(raw):
        return f"https://www.youtube.com/watch?v={raw}"
    if raw.startswith(("live/", "embed/", "shorts/")):
        return f"https://www.youtube.com/{raw}"
    if raw.startswith("@"):
        return f"https://www.youtube.com/{raw}"
    if raw.startswith("UC"):
        if raw.endswith("/live"):
            channel_id = raw.split("/", 1)[0]
            return f"https://www.youtube.com/channel/{channel_id}/live"
        return f"https://www.youtube.com/channel/{raw}"
    if raw.startswith(("channel/", "c/", "user/")):
        return f"https://www.youtube.com/{raw}"
    return f"https://www.youtube.com/{raw}"


def _pick_stream(streams: dict[str, Any], quality: str = "best") -> tuple[str, Any] | tuple[None, None]:
    quality = (quality or "best").strip()
    if quality in streams:
        return quality, streams[quality]
    if "best" in streams:
        return "best", streams["best"]
    return next(iter(streams.items()), (None, None))


def _resolve_youtube_with_streamlink(url: str, quality: str = "best") -> dict[str, Any]:
    session = Streamlink()
    for option_name, option_value in (
        ("http-timeout", YOUTUBE_STREAMLINK_TIMEOUT_SECONDS),
        ("stream-timeout", YOUTUBE_STREAMLINK_TIMEOUT_SECONDS),
    ):
        try:
            session.set_option(option_name, option_value)
        except Exception:
            pass
    streams = session.streams(url)
    stream_name, stream = _pick_stream(streams, quality)
    if stream is None:
        raise NoStreamsError(url)

    play_url = stream.to_url()
    if not play_url:
        raise StreamError("streamlink did not return a stream URL")
    source_type = _infer_source_type(play_url)
    if not source_type:
        raise StreamError(f"unsupported stream URL type: {play_url[:120]}")

    return {
        "url": play_url,
        "source_type": source_type,
        "stream_name": stream_name or "best",
        "available_streams": list(streams.keys()),
    }


def _query_truthy(value: str) -> bool:
    return str(value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def _current_video_primary_info_text(text: str) -> str:
    start = text.find('{"videoPrimaryInfoRenderer"')
    if start < 0:
        return ""
    end = text.find('"videoSecondaryInfoRenderer"', start)
    if end < 0:
        end = min(len(text), start + 80_000)
    return text[start:end]


async def _resolve_ids_from_page(url: str, client: httpx.AsyncClient) -> tuple[str, str, bool, bool]:
    """从 YouTube 页面同时提取 channel_id、video_id、is_live，一次请求搞定。"""
    try:
        resp = await client.get(
            url,
            follow_redirects=True,
            timeout=6,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if resp.status_code >= 400:
            return "", "", False, False
        text = resp.text or ""
    except httpx.HTTPError:
        return "", "", False, False

    channel_id = ""
    for pattern in _YOUTUBE_CHANNEL_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            channel_id = match.group(1)
            break

    command_match = _YOUTUBE_COMMAND_VIDEO_ID_RE.search(text)
    video_id = command_match.group(1) if command_match else ""
    for pattern in _YOUTUBE_VIDEO_ID_PATTERNS:
        if video_id:
            break
        match = pattern.search(text)
        if match:
            video_id = match.group(1)
            break

    primary_info = _current_video_primary_info_text(text)
    is_live = '"isLive":true' in primary_info and "watching now" in primary_info
    return channel_id, video_id, is_live, True


async def resolve_youtube(request: AdapterRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    youtube_url = _build_youtube_url(request)
    quality = (request.query.get("quality") or ["best"])[0]
    probe_only = _query_truthy((request.query.get("probe") or [""])[0])
    explicit_channel_id = _explicit_channel_id(request.resource_id)
    page_channel_id = explicit_channel_id
    page_video_id = ""
    page_is_live = False
    page_checked = False
    if probe_only or (not page_channel_id and ("/live" in youtube_url or "/@" in youtube_url)):
        parsed_channel_id, page_video_id, page_is_live, page_checked = await _resolve_ids_from_page(youtube_url, client)
        page_channel_id = parsed_channel_id or explicit_channel_id
    # URL 里直接带 video ID 的（watch?v=、youtu.be/、live/ID）也一并提取
    url_video_id = parse_youtube_video_id(youtube_url)
    effective_video_id = url_video_id or page_video_id

    if probe_only:
        if not page_checked:
            exc = AdapterResolveError(
                "youtube_page_unreachable",
                "YouTube 页面无法访问，未进行流测速",
                status_code=502,
                retryable=True,
            )
            exc.youtube_video_id = effective_video_id
            exc.youtube_page_is_live = False
            raise exc
        if not page_is_live:
            exc = AdapterResolveError(
                "youtube_not_live",
                "YouTube 页面未显示直播中",
                status_code=502,
                retryable=False,
            )
            exc.youtube_video_id = effective_video_id
            exc.youtube_page_is_live = False
            raise exc
        return {
            "ok": True,
            "adapter": "youtube",
            "source_type": "probe_only",
            "url": "",
            "direct_playable": False,
            "requires_proxy": True,
            "headers": {},
            "ttl": 120,
            "expires_at": None,
            "warnings": ["probe_only"],
            "stream_name": "best",
            "available_streams": [],
            "youtube_channel_id": page_channel_id,
            "youtube_video_id": effective_video_id,
            "youtube_page_is_live": True,
        }

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_resolve_youtube_with_streamlink, youtube_url, quality),
            timeout=YOUTUBE_STREAMLINK_TIMEOUT_SECONDS + 2,
        )
    except asyncio.TimeoutError as exc:
        exc.youtube_video_id = effective_video_id
        exc.youtube_page_is_live = page_is_live
        raise AdapterResolveError(
            "youtube_resolve_timeout",
            "YouTube Streamlink 解析超时，尚未拿到真实播放地址",
            status_code=504,
            retryable=True,
        ) from exc
    except NoStreamsError as exc:
        exc.youtube_video_id = effective_video_id
        exc.youtube_page_is_live = page_is_live
        err_msg = str(exc).upper()
        # LOGIN_REQUIRED / Sign in / PO Token → 风控，不是真没播
        if any(kw in err_msg for kw in ("LOGIN_REQUIRED", "SIGN IN", "PO TOKEN", "BOT")):
            raise AdapterResolveError(
                "youtube_resolve_failed",
                f"YouTube 风控: {exc}",
                status_code=502,
                retryable=True,
            ) from exc
        raise AdapterResolveError(
            "youtube_not_live",
            "YouTube 没有返回可播放流，可能未开播",
            status_code=502,
            retryable=False,
        ) from exc
    except (NoPluginError, PluginError, StreamError, StreamlinkError, OSError) as exc:
        exc.youtube_video_id = effective_video_id
        exc.youtube_page_is_live = page_is_live
        raise AdapterResolveError(
            "youtube_resolve_failed",
            f"YouTube Streamlink 解析失败: {exc}",
            status_code=502,
            retryable=True,
        ) from exc

    return {
        "ok": True,
        "adapter": "youtube",
        "source_type": result["source_type"],
        "url": result["url"],
        "direct_playable": False,
        "requires_proxy": True,
        "headers": {},
        "ttl": 120,
        "expires_at": None,
        "warnings": [],
        "stream_name": result.get("stream_name", "best"),
        "available_streams": result.get("available_streams", []),
        "youtube_channel_id": page_channel_id,
        "youtube_video_id": effective_video_id,
        "youtube_page_is_live": page_is_live,
    }
