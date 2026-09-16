#!/usr/bin/env python3
"""Official Huya Provider Plugin.

This provider is independently runnable and keeps the legacy Huya selection
policy inside the Plugin.  Core only receives the public TV descriptor;
selection diagnostics are data-only generic metadata.
"""
from __future__ import annotations

import asyncio
import json
import re
from html import unescape
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from streamlink import Streamlink
from streamlink.exceptions import NoPluginError, NoStreamsError
from streamlink.exceptions import PluginError as StreamlinkPluginError
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


HUYA_TTL_SECONDS = 60


def _first_stable_cover(*objects: dict[str, Any]) -> str:
    """Read only explicitly stable room-art fields from Huya metadata.

    The room-card art field (``liveData.screenshot``) is handled separately by
    ``_split_card_cover``.  The aliases below are upstream metadata names, not
    URL templates; no anchorpost URL is ever synthesized by the Plugin.
    """
    stable_fields = (
        "stableCoverUrl", "stable_cover_url", "roomCover", "room_cover",
        "roomPoster", "room_poster", "anchorPost", "anchorpost", "anchor_post",
    )
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for field in stable_fields:
            value = obj.get(field)
            if isinstance(value, dict):
                value = value.get("url") or value.get("imageUrl") or value.get("image_url")
            value = str(value or "").strip()
            if value.startswith(("http://", "https://")):
                return value
    return ""


def _split_card_cover(raw: str) -> tuple[str, str]:
    """Split Huya's single card-cover field into (stable poster, live frame).

    Huya reuses ONE field for the room-card art (``liveData.screenshot`` in
    profileRoom, the same value the room page SSR renders as ``<meta
    name="image">``): the anchor poster when the anchor has one, otherwise the
    current live frame.  The URL is passed through verbatim - only its host
    classifies it, and no path/hash/query is ever synthesized.
    """
    url = str(raw or "").strip()
    if not url:
        return "", ""
    if url.startswith("//"):
        # Room-page SSR can emit scheme-relative URLs; the generic visual
        # contract only allows absolute http(s) values.
        url = f"https:{url}"
    host = (urlsplit(url).hostname or "").lower()
    if "anchorpost" in host:
        return url, ""   # stable channel poster
    return "", url       # live frame (or unknown host) -> dynamic


def _room_page_card_cover(room_id: str) -> tuple[str, str]:
    """Best-effort read of the room page SSR card cover; (``"", ""``) on failure.

    Some rooms (notably official replay/announced channels) keep
    ``profileRoom.liveData.screenshot`` empty even though the web room page
    still renders the anchor poster.  Read the exact field the page embeds
    (``TT_ROOM_DATA.screenshot``), falling back to the ``og:image`` meta tag.
    The value goes through the same host classification; nothing is fabricated.
    """
    request = Request(
        f"https://www.huya.com/{room_id.strip('/')}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.huya.com/"},
    )
    with urlopen(request, timeout=6.0) as response:
        html = response.read(512 * 1024).decode("utf-8", errors="replace")
    match = re.search(r"var TT_ROOM_DATA = (\{.*?\});", html, re.S)
    if match:
        field = re.search(r'"screenshot"\s*:\s*"([^"]*)"', match.group(1))
        if field and field.group(1):
            return _split_card_cover(field.group(1))
    for pattern in (
        r'<meta[^>]+itemProp="image"[^>]+content="([^"]+)"',
        r'<meta[^>]+name="image"[^>]+content="([^"]+)"',
    ):
        meta = re.search(pattern, html)
        if meta and meta.group(1):
            return _split_card_cover(unescape(meta.group(1)))
    return "", ""


def _infer_transport(url: str) -> str:
    lower = url.lower().split("?", 1)[0]
    if lower.endswith(".flv"):
        return "http_flv"
    if lower.endswith(".m3u8"):
        return "hls"
    if lower.endswith(".ts"):
        return "mpegts"
    # Preserve the legacy fallback for Streamlink URLs without a recognized
    # extension.
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
        "transport": _infer_transport(play_url),
        "stream_name": stream_name or "best",
        "available_streams": list(streams.keys()),
    }


def _resolve_with_worker_thread(huya_url: str, preferred_cdn: str) -> dict[str, Any]:
    """Keep Streamlink off the SDK request loop, matching legacy to_thread semantics.

    PluginApplication already invokes each provider request in a worker thread;
    this nested coroutine makes the boundary explicit and keeps the blocking
    Streamlink call isolated from the Plugin IPC reader as well.
    """
    return asyncio.run(asyncio.to_thread(_resolve_huya_with_streamlink, huya_url, preferred_cdn))


def _failure(message: str, provider_code: str) -> TemporaryFailure:
    error = TemporaryFailure(message)
    error.details.update({"provider": "huya", "provider_code": provider_code})
    return error


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        context.raise_if_cancelled()
        room_id = reference.resource_id.strip("/")
        if not room_id:
            raise InvalidResource("Huya room ID is empty")

        huya_url = f"https://www.huya.com/{room_id}"
        preferred_cdn = (reference.query.get("cdn") or ["tx"])[0]
        try:
            result = _resolve_with_worker_thread(huya_url, preferred_cdn)
        except NoStreamsError:
            raise PluginError(
                "NOT_LIVE", "Huya resource is not live or has no playable stream", retryable=False,
                details={"provider": "huya", "provider_code": "huya_not_live"},
            ) from None
        except (NoPluginError, StreamlinkPluginError, StreamError, StreamlinkError, OSError):
            raise _failure("Huya Streamlink resolution failed", "huya_resolve_failed") from None
        except PluginError:
            raise
        except Exception:
            raise _failure("Huya Streamlink resolution failed", "huya_resolve_failed") from None
        context.raise_if_cancelled()
        return StreamDescriptor(
            url=result["url"],
            transport=result["transport"],
            headers={"Origin": "https://www.huya.com", "Referer": "https://www.huya.com/"},
            ttl_seconds=HUYA_TTL_SECONDS,
            expires_at=None,
            volatile_url=False,
            requires_proxy=False,
            provider_diagnostics={
                "provider": "huya",
                "stream_name": result.get("stream_name", "best"),
                "available_streams": result.get("available_streams", []),
                "preferred_cdn": (preferred_cdn or "tx").lower(),
            },
        )

    def visual_metadata(self, reference: TVReference, context: ResolveContext) -> VisualMetadata:
        context.raise_if_cancelled()
        room_id = reference.resource_id.strip("/")
        if not room_id:
            raise InvalidResource("Huya room ID is empty")
        api = (
            "https://mp.huya.com/cache.php?m=Live&do=profileRoom&showSecret=1"
            f"&roomid={room_id}"
        )
        try:
            request = Request(api, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.huya.com/"})
            with urlopen(request, timeout=6.0) as response:
                payload = json.loads(response.read(1024 * 1024).decode("utf-8", errors="replace"))
            data = (payload or {}).get("data") or {}
            live_data = data.get("liveData") or {}
            profile = data.get("profileInfo") or {}
            live_status = data.get("liveStatus")
            if live_status is None:
                live_status = live_data.get("liveStatus")
            # Huya's room-card art is a single polymorphic field: the anchor
            # poster (anchorpost.*) when one exists, otherwise the current
            # live frame (live-cover.* / tx-live-cover.*).  Both are the exact
            # URL the web card renders; the host split is the classifier.
            raw_cover = str(live_data.get("screenshot") or "").strip()
            stable_cover, dynamic_cover = _split_card_cover(raw_cover)
            if not raw_cover:
                # Replay/announced rooms can omit the cover in profileRoom
                # while the web room page still carries the poster.
                try:
                    poster, frame = _room_page_card_cover(room_id)
                    stable_cover = stable_cover or poster
                    dynamic_cover = dynamic_cover or frame
                except Exception:
                    pass
            if not stable_cover:
                stable_cover = _first_stable_cover(profile, live_data, data)
            is_live = (str(live_status).upper() == "ON") if live_status is not None else bool(dynamic_cover or stable_cover)
            return VisualMetadata(
                avatar_url=str(live_data.get("avatar180") or profile.get("avatar180") or "").strip(),
                stable_cover_url=stable_cover,
                dynamic_cover_url=dynamic_cover,
                # Keep the pre-1.1 field for older Core/Frontend projections.
                cover_url=dynamic_cover,
                is_live=is_live,
                title=str(live_data.get("introduction") or "").strip(),
                owner_name=str(live_data.get("nick") or profile.get("nick") or "").strip(),
                ttl_seconds=300,
                cover_role="live",
            )
        except PluginError:
            raise
        except Exception as exc:
            raise TemporaryFailure("Huya visual metadata request failed") from exc


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/huya")
    provider = Provider()
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "huya", provider
    ).register_tv_visual("huya", provider).run()


if __name__ == "__main__":
    main()
