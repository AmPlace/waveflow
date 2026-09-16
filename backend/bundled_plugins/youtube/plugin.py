#!/usr/bin/env python3
"""Official YouTube Provider Plugin.

The implementation is intentionally self-contained and uses only the public
Plugin SDK plus the isolated Streamlink dependency.  YouTube identity and
probe observations are carried in the generic descriptor metadata fields;
Core remains the authority for probing, proxying, and security decisions.
"""
from __future__ import annotations

import re
import sys
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from streamlink import Streamlink
from streamlink.exceptions import NoPluginError, NoStreamsError, PluginError as StreamlinkPluginError
from streamlink.exceptions import StreamError, StreamlinkError
from waveflow_plugin_sdk import (
    InvalidResource,
    PluginApplication,
    PluginError,
    ResolveContext,
    StreamDescriptor,
    TemporaryFailure,
    TVProvider,
    TVReference,
    VisualMetadata,
)


YOUTUBE_STREAMLINK_TIMEOUT_SECONDS = 12.0
YOUTUBE_PAGE_TIMEOUT_SECONDS = 6.0
YOUTUBE_PAGE_MAX_BYTES = 4 * 1024 * 1024
YOUTUBE_TTL_SECONDS = 120
YOUTUBE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{20,}$")
_CHANNEL_PATTERNS = (
    re.compile(r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{20,})"'),
    re.compile(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{20,})"'),
    re.compile(r'<meta\s+itemprop=["\']channelId["\']\s+content=["\'](UC[A-Za-z0-9_-]{20,})["\']', re.I),
    re.compile(r"/channel/(UC[A-Za-z0-9_-]{20,})"),
)
_VIDEO_PATTERNS = (
    re.compile(r'<link\s+rel=["\']canonical["\']\s+href=["\']https?://(?:www\.)?youtube\.com/watch\?v=([A-Za-z0-9_-]{11})["\']', re.I),
    re.compile(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"'),
)
_COMMAND_VIDEO_RE = re.compile(
    r"window\[[\'\"]ytCommand[\'\"]\]\s*=\s*\{.*?\"watchEndpoint\"\s*:\s*\{.*?\"videoId\"\s*:\s*\"([A-Za-z0-9_-]{11})\"",
    re.S,
)
_YOUTUBE_INPUT_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
})


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def _explicit_channel_id(value: str) -> str:
    raw = unquote((value or "").strip().strip("/"))
    parts = [part for part in raw.split("/") if part]
    if parts and _CHANNEL_ID_RE.fullmatch(parts[0]):
        return parts[0]
    if len(parts) >= 2 and parts[0].lower() == "channel" and _CHANNEL_ID_RE.fullmatch(parts[1]):
        return parts[1]
    return ""


def _validated_youtube_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise InvalidResource("YouTube URL is malformed") from None
    if (
        parsed.scheme.lower() != "https"
        or host not in _YOUTUBE_INPUT_HOSTS
        or parsed.username
        or parsed.password
        or (port and port != 443)
    ):
        raise InvalidResource("YouTube URL host is not supported")

    path_parts = [part for part in parsed.path.split("/") if part]
    if host in {"youtu.be", "www.youtu.be"}:
        valid = bool(path_parts and _VIDEO_ID_RE.fullmatch(path_parts[0]))
    elif path_parts and path_parts[0].lower() == "watch":
        valid = bool(_VIDEO_ID_RE.fullmatch((parse_qs(parsed.query).get("v") or [""])[0]))
    elif len(path_parts) >= 2 and path_parts[0].lower() in {"live", "embed", "shorts"}:
        valid = bool(_VIDEO_ID_RE.fullmatch(path_parts[1]))
    elif len(path_parts) >= 2 and path_parts[0].lower() == "channel":
        valid = bool(_CHANNEL_ID_RE.fullmatch(path_parts[1]))
    elif path_parts and path_parts[0].startswith("@"):
        valid = len(path_parts[0]) > 1
    elif len(path_parts) >= 2 and path_parts[0].lower() in {"c", "user"}:
        valid = bool(path_parts[1])
    else:
        valid = False
    if not valid:
        raise InvalidResource("YouTube URL does not identify a supported video or channel")
    return value


def _build_youtube_url(reference: TVReference) -> str:
    query_url = (reference.query.get("url") or [""])[0].strip()
    if query_url:
        return _validated_youtube_url(query_url)
    raw = unquote((reference.resource_id or "").strip().strip("/"))
    if not raw:
        raise InvalidResource("YouTube URL or reference is empty")
    if raw.startswith(("http://", "https://")):
        return _validated_youtube_url(raw)
    if _VIDEO_ID_RE.fullmatch(raw):
        return f"https://www.youtube.com/watch?v={raw}"
    if raw.startswith(("live/", "embed/", "shorts/")):
        parts = raw.split("/", 1)
        if len(parts) == 2 and _VIDEO_ID_RE.fullmatch(parts[1]):
            return f"https://www.youtube.com/{raw}"
        raise InvalidResource("YouTube video reference is invalid")
    if raw.startswith("channel/"):
        parts = raw.split("/", 1)
        if len(parts) == 2 and _CHANNEL_ID_RE.fullmatch(parts[1]):
            return f"https://www.youtube.com/{raw}"
        raise InvalidResource("YouTube channel reference is invalid")
    if raw.startswith(("@", "c/", "user/")):
        return f"https://www.youtube.com/{raw}"
    if _CHANNEL_ID_RE.fullmatch(raw):
        return f"https://www.youtube.com/channel/{raw}"
    raise InvalidResource("YouTube reference is unsupported")


def _video_id(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.netloc.lower() in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/")[0]
    elif parsed.path.lower().startswith(("/live/", "/embed/", "/shorts/")):
        candidate = parsed.path.strip("/").split("/")[1]
    else:
        candidate = parse_qs(parsed.query).get("v", [""])[0]
    return candidate if _VIDEO_ID_RE.fullmatch(candidate or "") else ""


def _page_live_marker(text: str) -> bool:
    primary_start = text.find('{"videoPrimaryInfoRenderer"')
    if primary_start < 0:
        return False
    primary_end = text.find('"videoSecondaryInfoRenderer"', primary_start)
    primary = text[primary_start:primary_end if primary_end >= 0 else primary_start + 80_000]
    return '"isLive":true' in primary and "watching now" in primary


def _parse_page(text: str) -> dict[str, Any]:
    channel_id = ""
    for pattern in _CHANNEL_PATTERNS:
        match = pattern.search(text)
        if match:
            channel_id = match.group(1)
            break
    video_id = ""
    match = _COMMAND_VIDEO_RE.search(text)
    if match:
        video_id = match.group(1)
    for pattern in _VIDEO_PATTERNS:
        if video_id:
            break
        match = pattern.search(text)
        if match:
            video_id = match.group(1)
    return {"channel_id": channel_id, "video_id": video_id, "page_live": _page_live_marker(text)}


def _fetch_page(url: str) -> tuple[str, bool]:
    request = Request(url, headers={"User-Agent": YOUTUBE_UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urlopen(request, timeout=YOUTUBE_PAGE_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200) or 200)
            if status >= 400:
                return "", False
            return response.read(YOUTUBE_PAGE_MAX_BYTES + 1)[:YOUTUBE_PAGE_MAX_BYTES].decode("utf-8", errors="replace"), True
    except (HTTPError, URLError, TimeoutError, OSError):
        return "", False


def _infer_transport(url: str) -> str:
    lower = url.lower().split("?", 1)[0]
    if ".m3u8" in lower or "manifest/hls" in lower:
        return "hls"
    if lower.endswith(".flv"):
        return "http_flv"
    if lower.endswith(".ts"):
        return "mpegts"
    raise TemporaryFailure("YouTube returned an unsupported Streamlink URL")


def _streamlink_result(url: str, quality: str) -> dict[str, Any]:
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
    stream = streams.get(quality) or streams.get("best")
    stream_name = quality if quality in streams else ("best" if "best" in streams else "")
    if stream is None:
        stream_name, stream = next(iter(streams.items()), ("", None))
    if stream is None:
        raise NoStreamsError(url)
    play_url = stream.to_url()
    if not isinstance(play_url, str) or not play_url:
        raise StreamError("Streamlink returned no playback URL")
    return {
        "url": play_url,
        "transport": _infer_transport(play_url),
        "stream_name": stream_name or "best",
        "available_streams": [str(value) for value in streams],
    }


def _details(provider_code: str, **values: Any) -> dict[str, Any]:
    return {"provider_code": provider_code, **values}


def _temporary(message: str, provider_code: str, **values: Any) -> TemporaryFailure:
    error = TemporaryFailure(message)
    error.details.update(_details(provider_code, **values))
    return error


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        context.raise_if_cancelled()
        youtube_url = _build_youtube_url(reference)
        quality = (reference.query.get("quality") or ["best"])[0]
        probe_only = _truthy((reference.query.get("probe") or [""])[0])
        explicit_channel_id = _explicit_channel_id(reference.resource_id)
        page_info = {"channel_id": explicit_channel_id, "video_id": _video_id(youtube_url), "page_live": False}
        page_checked = False
        if probe_only or (not explicit_channel_id and ("/live" in youtube_url or "/@" in youtube_url)):
            text, page_checked = _fetch_page(youtube_url)
            if page_checked:
                page_info.update(_parse_page(text))
                page_info["channel_id"] = page_info["channel_id"] or explicit_channel_id
                page_info["video_id"] = page_info["video_id"] or _video_id(youtube_url)

        if probe_only:
            if not page_checked:
                raise _temporary(
                    "YouTube page is unreachable; stream probing was not attempted",
                    "youtube_page_unreachable", video_id=page_info["video_id"],
                )
            if not page_info["page_live"]:
                raise PluginError(
                    "NOT_LIVE", "YouTube page is not live", retryable=False,
                    details=_details(
                        "youtube_not_live", channel_id=page_info["channel_id"], video_id=page_info["video_id"],
                    ),
                )
            return StreamDescriptor(
                url="", transport="probe_only", headers={}, ttl_seconds=YOUTUBE_TTL_SECONDS,
                expires_at=None, volatile_url=True, requires_proxy=True, warnings=["probe_only"],
                provider_diagnostics={
                    "provider": "youtube", "channel_id": page_info["channel_id"],
                    "video_id": page_info["video_id"], "page_live": True, "page_checked": True,
                },
                probe_hints={
                    "page_live": True, "video_id": page_info["video_id"],
                    "channel_id": page_info["channel_id"], "probe_only": True,
                },
            )

        try:
            result = _streamlink_result(youtube_url, quality)
        except NoStreamsError as exc:
            message = str(exc).upper()
            if any(value in message for value in ("LOGIN_REQUIRED", "SIGN IN", "PO TOKEN", "BOT")):
                raise _temporary(
                    "YouTube Streamlink requires login or risk-control clearance",
                    "youtube_risk_control", video_id=page_info["video_id"], page_live=page_info["page_live"],
                ) from None
            raise PluginError(
                "NOT_LIVE", "YouTube returned no playable stream", retryable=False,
                details=_details(
                    "youtube_not_live", video_id=page_info["video_id"], page_live=page_info["page_live"],
                ),
            ) from None
        except TimeoutError:
            error = PluginError(
                "PLUGIN_TIMEOUT", "YouTube Streamlink resolution timed out", retryable=True, category="timeout",
                details=_details("youtube_resolve_timeout", video_id=page_info["video_id"]),
            )
            raise error from None
        except (NoPluginError, StreamlinkPluginError, StreamError, StreamlinkError, OSError) as exc:
            raise _temporary(
                "YouTube Streamlink resolution failed", "youtube_resolve_failed",
                video_id=page_info["video_id"], page_live=page_info["page_live"],
            ) from exc
        except PluginError:
            raise
        except Exception as exc:
            raise _temporary(
                "YouTube Streamlink resolution failed", "youtube_resolve_failed",
                video_id=page_info["video_id"], page_live=page_info["page_live"],
            ) from exc
        context.raise_if_cancelled()
        return StreamDescriptor(
            url=result["url"], transport=result["transport"], headers={},
            ttl_seconds=YOUTUBE_TTL_SECONDS, expires_at=None, volatile_url=True, requires_proxy=True,
            provider_diagnostics={
                "provider": "youtube", "channel_id": page_info["channel_id"],
                "video_id": page_info["video_id"], "page_live": page_info["page_live"],
                "page_checked": page_checked, "stream_name": result["stream_name"],
                "available_streams": result["available_streams"],
            },
            probe_hints={
                "page_live": page_info["page_live"], "video_id": page_info["video_id"],
                "channel_id": page_info["channel_id"],
            },
        )

    def visual_metadata(self, reference: TVReference, context: ResolveContext) -> VisualMetadata:
        context.raise_if_cancelled()
        youtube_url = _build_youtube_url(reference)
        video_id = _video_id(youtube_url)
        if not video_id:
            return VisualMetadata(ttl_seconds=300, cover_role="content")
        return VisualMetadata(
            # Thumbnail is a dynamic content visual; channel-avatar discovery
            # is intentionally not part of this migration.
            cover_url=f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            is_live=False,
            ttl_seconds=1800,
            cover_role="content",
        )


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/youtube")
    provider = Provider()
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "youtube", provider
    ).register_tv_visual("youtube", provider).run()


if __name__ == "__main__":
    main()
