from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest import mock

from routers import media_proxy
from security.dependencies import MediaAccessContext
from security.proxy_context import ProxyContext, get_registry, reset_for_tests
from security.proxy_handles import decode_for_kind, issue_handle


class RadioPlaybackTransportTest(unittest.TestCase):
    def test_audio_http_source_without_headers_keeps_transport_in_handle_context(self):
        reset_for_tests()
        previous_main = sys.modules.get("main")
        sys.modules["main"] = types.SimpleNamespace()
        try:
            response = asyncio.run(media_proxy._serve_resolved_source_playlist(
                resolved_url="https://radio.example/live.mp3",
                resolved_st="audio_http",
                custom_ua="",
                referer="",
                cookie="",
                no_ua=False,
                canonical_key="radio_station_one",
                source_id="src_radio_one",
                source_revision="revision-one",
                access=MediaAccessContext(source="anonymous"),
                domain="radio",
            ))
        finally:
            if previous_main is None:
                sys.modules.pop("main", None)
            else:
                sys.modules["main"] = previous_main

        handle = response.headers["location"].rsplit("/", 1)[-1]
        payload = decode_for_kind(handle, "stream")
        context = get_registry().get(payload.ctx)
        self.assertIsNotNone(context)
        self.assertEqual(context.source_type, "audio_http")
        reset_for_tests()

    def test_stream_proxy_uses_signed_context_transport(self):
        reset_for_tests()
        context_id = get_registry().put(ProxyContext(
            custom_ua="Radio UA",
            referer="https://radio.example/",
            source_type="audio_http",
            upstream_url="https://radio.example/live.mp3",
            source_id="radio:src-one",
        ))
        handle = issue_handle(
            kind="stream",
            url="https://radio.example/live.mp3",
            ctx=context_id,
            ttl_seconds=300,
        )
        captured: dict[str, object] = {}

        async def serve_iptv_proxy_stream_response(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(status_code=200)

        fake_main = types.SimpleNamespace(
            CDN_REQUEST_HEADERS={"User-Agent": "Fallback UA"},
            serve_iptv_proxy_stream_response=serve_iptv_proxy_stream_response,
        )
        previous_main = sys.modules.get("main")
        try:
            sys.modules["main"] = fake_main
            with mock.patch.object(media_proxy, "_validate_handle_url_or_403", new=mock.AsyncMock()):
                response = asyncio.run(media_proxy.media_proxy_stream(
                    handle,
                    types.SimpleNamespace(headers={}),
                    "",
                    MediaAccessContext(source="anonymous"),
                ))
        finally:
            if previous_main is None:
                sys.modules.pop("main", None)
            else:
                sys.modules["main"] = previous_main
            reset_for_tests()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["stream_type"], "audio_http")
        self.assertEqual(captured["upstream_headers"]["User-Agent"], "Radio UA")
        self.assertEqual(captured["upstream_headers"]["Referer"], "https://radio.example/")


if __name__ == "__main__":
    unittest.main()
