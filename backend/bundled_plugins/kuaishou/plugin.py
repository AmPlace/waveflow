#!/usr/bin/env python3
"""Kuaishou TV Provider implemented with direct curl-cffi networking.

The implementation intentionally mirrors the legacy resolver's public result
semantics.  It has no dependency on WaveFlow Core state or the legacy adapter;
Core remains responsible for permission, ownership, probing, and proxy policy.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any, Callable

from curl_cffi.requests import AsyncSession
from waveflow_plugin_sdk import (
    InvalidResource,
    PluginApplication,
    PluginError,
    ResolveContext,
    StreamDescriptor,
    TVProvider,
    TVReference,
    VisualMetadata,
)


KUAISHOU_TTL_SECONDS = 30 * 60
KUAISHOU_URL = "https://live.kuaishou.com/u/{room_id}"
_INITIAL_STATE_RE = re.compile(
    r'<script>window\.__INITIAL_STATE__=(.*?);\(function\(\)\{var s;'
)
_PLAYLIST_RE = re.compile(r'(\{"liveStream".*?),"gameInfo')


def _provider_failure(
    provider_code: str,
    message: str,
    *,
    retryable: bool = True,
    category: str = "network",
    status: int | None = None,
) -> PluginError:
    details: dict[str, Any] = {"provider_code": provider_code}
    if status is not None:
        details["status"] = int(status)
    return PluginError(
        "TEMPORARY_UPSTREAM_FAILURE",
        message,
        retryable=retryable,
        category=category,
        details=details,
    )


def _not_live() -> PluginError:
    return PluginError(
        "NOT_LIVE",
        "Kuaishou room is not live",
        retryable=False,
        category="provider",
        details={"provider_code": "kuaishou_not_live"},
    )


def _parse_initial_state(html: str) -> dict[str, Any]:
    match = _INITIAL_STATE_RE.search(html)
    if not match:
        raise _provider_failure(
            "kuaishou_parse_failed",
            "Kuaishou page did not contain INITIAL_STATE",
        )
    try:
        # Keep the legacy distinction: a missing script marker is a parse
        # failure, while an incomplete/malformed INITIAL_STATE reaches the
        # resolver's generic upstream-failure path.
        raw = _PLAYLIST_RE.findall(match.group(1))[0] + "}"
        value = json.loads(raw)
    except (IndexError, TypeError, ValueError):
        raise _provider_failure(
            "kuaishou_resolve_failed",
            "Kuaishou INITIAL_STATE was malformed",
        ) from None
    if not isinstance(value, dict):
        raise _provider_failure(
            "kuaishou_resolve_failed",
            "Kuaishou INITIAL_STATE was not an object",
        )
    return value


async def direct_fetch(
    url: str,
    *,
    session_factory: Callable[..., Any] = AsyncSession,
) -> tuple[int, str]:
    """Fetch one room using the same curl-cffi browser profile as legacy."""
    async with session_factory(impersonate="chrome") as session:
        response = await session.get(url)
    return int(response.status_code), str(response.text)


def _resolve_direct(url: str) -> tuple[int, str]:
    return asyncio.run(direct_fetch(url))


class Provider(TVProvider):
    def resolve_stream(self, reference: TVReference, context: ResolveContext) -> StreamDescriptor:
        context.raise_if_cancelled()
        room_id = reference.resource_id.strip("/")
        if not room_id:
            raise InvalidResource("Kuaishou room id is empty")

        url = KUAISHOU_URL.format(room_id=room_id)
        try:
            status, html = _resolve_direct(url)
            if status != 200:
                raise _provider_failure(
                    "kuaishou_http_error",
                    "Kuaishou page request failed",
                    status=status,
                )
            playlist = _parse_initial_state(html)
            live_stream = playlist.get("liveStream", {})
            author = playlist.get("author", {})
            anchor_name = author.get("name", "")
        except PluginError:
            raise
        except Exception:
            raise _provider_failure(
                "kuaishou_resolve_failed",
                "Kuaishou page resolution failed",
            ) from None

        if not live_stream:
            raise _not_live()

        play_urls = live_stream.get("playUrls")
        if not play_urls:
            raise _provider_failure(
                "kuaishou_no_play_url",
                "Kuaishou did not return a playable URL",
            )

        if "h264" in play_urls:
            adaptation = play_urls["h264"].get("adaptationSet", {})
        else:
            adaptation = play_urls[0].get("adaptationSet", {}) if isinstance(play_urls, list) else {}

        representation = adaptation.get("representation", [])
        if not representation:
            raise _provider_failure(
                "kuaishou_no_play_url",
                "Kuaishou did not return a playable URL",
            )

        flv_url = representation[0].get("url", "")
        if not isinstance(flv_url, str) or not flv_url:
            raise _provider_failure(
                "kuaishou_no_play_url",
                "Kuaishou did not return a playable URL",
            )

        return StreamDescriptor.flv(
            flv_url,
            ttl_seconds=KUAISHOU_TTL_SECONDS,
            volatile_url=False,
            requires_proxy=False,
            provider_diagnostics={
                "anchor_name": str(anchor_name or ""),
                "dependency_origin": str(sys.modules["curl_cffi"].__file__),
            },
        )

    def visual_metadata(self, reference: TVReference, context: ResolveContext) -> VisualMetadata:
        context.raise_if_cancelled()
        room_id = reference.resource_id.strip("/")
        if not room_id:
            raise InvalidResource("Kuaishou room id is empty")
        try:
            status, html = _resolve_direct(KUAISHOU_URL.format(room_id=room_id))
            if status != 200:
                raise _provider_failure("kuaishou_visual_http_error", "Kuaishou visual request failed", status=status)
            playlist = _parse_initial_state(html)
            live_stream = playlist.get("liveStream") or {}
            author = playlist.get("author") or {}
            return VisualMetadata(
                avatar_url=str(author.get("avatar") or author.get("headurl") or "").strip(),
                cover_url=str(live_stream.get("poster") or live_stream.get("coverUrl") or "").strip(),
                is_live=bool(live_stream),
                title=str(live_stream.get("caption") or "").strip(),
                owner_name=str(author.get("name") or "").strip(),
                ttl_seconds=300,
                cover_role="live",
            )
        except PluginError:
            raise
        except Exception as exc:
            raise _provider_failure("kuaishou_visual_failed", "Kuaishou visual request failed") from exc


def main() -> None:
    identity, version = PluginApplication.identity_args("org.waveflow/kuaishou")
    provider = Provider()
    PluginApplication(identity=identity, version=version, permissions=["network"]).register_tv(
        "kuaishou", provider
    ).register_tv_visual("kuaishou", provider).run()


if __name__ == "__main__":
    main()
