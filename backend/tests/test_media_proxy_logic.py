import unittest
import asyncio
import inspect
import os
import sys
import time
import types
from unittest import mock

from provider_reference import ProviderReferenceError
from security.dependencies import MediaAccessContext
from security.proxy_context import get_registry as get_proxy_context_registry, reset_for_tests as reset_proxy_context_for_tests
from security.proxy_handles import decode_for_kind, issue_handle
from security.source_ids import source_id_for, source_revision_for
from routers import media_proxy
from routers.media_proxy import _playback_source_supported


class _FakeMain:
    @staticmethod
    def _source_type(source):
        return source.get("source_type") or "hls"


class ThinPlaylistCacheKeyTest(unittest.TestCase):
    def test_cache_key_uses_source_revision_scope_and_dynamic_query(self):
        import main

        url_a = "https://cdn.example/live/index.m3u8?token=a"
        url_b = "https://cdn.example/live/index.m3u8?token=b"

        self.assertNotEqual(
            main._thin_cache_key(url_a, "src_a:rev1"),
            main._thin_cache_key(url_b, "src_a:rev1"),
        )
        self.assertNotEqual(
            main._thin_cache_key(url_a, "src_a:rev1"),
            main._thin_cache_key(url_a, "src_a:rev2"),
        )


class MediaProxyLogicTest(unittest.TestCase):
    def setUp(self):
        # These identity tests do not initialize a database. Use a stable test
        # root rather than depending on another test's temporary secret store.
        secret = mock.patch.dict(os.environ, {"WAVEFLOW_PROXY_HANDLE_SECRET": "media-proxy-test-fixture"})
        secret.start()
        self.addCleanup(secret.stop)

    def test_playback_keeps_untested_sources(self):
        source = {
            "url": "https://example.com/live.m3u8",
            "enabled": True,
            "is_working": 0,
            "probe_status": "untested",
            "source_type": "hls",
        }

        self.assertTrue(_playback_source_supported(_FakeMain, source))

    def test_playback_hard_excludes_disabled_sources(self):
        source = {
            "url": "https://example.com/live.m3u8",
            "enabled": False,
            "is_working": 1,
            "probe_status": "online",
            "source_type": "hls",
        }

        self.assertFalse(_playback_source_supported(_FakeMain, source))

    def test_iptv_playlist_source_id_selects_exact_source(self):
        selected = {}
        first_source = {"url": "http://[2409::1]/live.m3u8", "source_type": "hls", "enabled": True, "source_id": "src_first"}
        second_source = {"url": "https://fjzh.example/live.m3u8", "source_type": "hls", "enabled": True, "source_id": "src_second"}
        fake_main = types.SimpleNamespace(
            _get_aggregated_iptv_channels=lambda: asyncio.sleep(0, result=([
                {"canonical_key": "福建综合", "urls": [first_source, second_source]},
            ], [])),
            _sorted_sources=lambda sources: list(sources),
            _source_type=lambda source: source.get("source_type") or "hls",
        )
        old_main = sys.modules.get("main")
        old_serve = media_proxy._serve_iptv_source_playlist

        async def fake_serve(source, canonical_key, access):
            selected["source"] = source
            selected["canonical_key"] = canonical_key
            return types.SimpleNamespace(status_code=200)

        try:
            sys.modules["main"] = fake_main
            media_proxy._serve_iptv_source_playlist = fake_serve
            response = asyncio.run(media_proxy._serve_iptv_channel_playlist(
                "福建综合",
                types.SimpleNamespace(headers={}),
                MediaAccessContext(source="anonymous"),
                source_id=second_source["source_id"],
            ))
        finally:
            media_proxy._serve_iptv_source_playlist = old_serve
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(response.status_code, 200)
        self.assertIs(selected["source"], second_source)
        self.assertEqual(selected["canonical_key"], "福建综合")

    def test_iptv_playlist_other_channel_source_id_returns_404(self):
        current_source = {"url": "https://a.example/live.m3u8", "source_type": "hls", "enabled": True, "source_id": "src_current"}
        other_source = {"url": "https://b.example/live.m3u8", "source_type": "hls", "enabled": True, "source_id": "src_other"}
        fake_main = types.SimpleNamespace(
            _get_aggregated_iptv_channels=lambda: asyncio.sleep(0, result=([
                {"canonical_key": "当前频道", "urls": [current_source]},
                {"canonical_key": "其他频道", "urls": [other_source]},
            ], [])),
            _sorted_sources=lambda sources: list(sources),
            _source_type=lambda source: source.get("source_type") or "hls",
        )
        old_main = sys.modules.get("main")
        try:
            sys.modules["main"] = fake_main
            with self.assertRaises(media_proxy.HTTPException) as ctx:
                asyncio.run(media_proxy._serve_iptv_channel_playlist(
                    "当前频道",
                    types.SimpleNamespace(headers={}),
                    MediaAccessContext(source="anonymous"),
                    source_id=other_source["source_id"],
                ))
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_iptv_playlist_fake_source_id_returns_404(self):
        source = {"url": "https://a.example/live.m3u8", "source_type": "hls", "enabled": True, "source_id": "src_real"}
        fake_main = types.SimpleNamespace(
            _get_aggregated_iptv_channels=lambda: asyncio.sleep(0, result=([
                {"canonical_key": "频道", "urls": [source]},
            ], [])),
            _sorted_sources=lambda sources: list(sources),
            _source_type=lambda source: source.get("source_type") or "hls",
        )
        old_main = sys.modules.get("main")
        try:
            sys.modules["main"] = fake_main
            with self.assertRaises(media_proxy.HTTPException) as ctx:
                asyncio.run(media_proxy._serve_iptv_channel_playlist(
                    "频道",
                    types.SimpleNamespace(headers={}),
                    MediaAccessContext(source="anonymous"),
                    source_id="src_not_real",
                ))
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_iptv_playlist_disabled_source_id_returns_403(self):
        source = {"url": "https://a.example/live.m3u8", "source_type": "hls", "enabled": False, "source_id": "src_disabled"}
        fake_main = types.SimpleNamespace(
            _get_aggregated_iptv_channels=lambda: asyncio.sleep(0, result=([
                {"canonical_key": "频道", "urls": [source]},
            ], [])),
            _sorted_sources=lambda sources: list(sources),
            _source_type=lambda source: source.get("source_type") or "hls",
        )
        old_main = sys.modules.get("main")
        try:
            sys.modules["main"] = fake_main
            with self.assertRaises(media_proxy.HTTPException) as ctx:
                asyncio.run(media_proxy._serve_iptv_channel_playlist(
                    "频道",
                    types.SimpleNamespace(headers={}),
                    MediaAccessContext(source="anonymous"),
                    source_id=source["source_id"],
                ))
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_same_url_different_headers_get_different_source_ids(self):
        first = {
            "url": "https://same.example/live.m3u8",
            "source_type": "hls",
            "referer": "https://a.example/",
        }
        second = {
            "url": "https://same.example/live.m3u8",
            "source_type": "hls",
            "referer": "https://b.example/",
        }
        self.assertNotEqual(source_id_for(first), source_id_for(second))

    def test_market_auto_source_item_id_does_not_define_source_id(self):
        base = {
            "url": "https://same.example/live.m3u8",
            "source_type": "hls",
            "market_package_id": "pkg",
            "market_source_id": "src",
            "market_channel_id": "channel",
            "market_source_item_id": "auto-derived-from-index",
        }
        same_config_different_auto_id = {
            **base,
            "market_source_item_id": "auto-another-index-derived-value",
        }
        different_config = {
            **base,
            "referer": "https://different.example/",
            "market_source_item_id": "auto-another-index-derived-value",
        }

        self.assertEqual(source_id_for(base), source_id_for(same_config_different_auto_id))
        self.assertNotEqual(source_id_for(base), source_id_for(different_config))

    def test_db_source_revision_changes_without_changing_source_id(self):
        base = {
            "id": 10,
            "subscription_id": 2,
            "url": "https://same.example/live.m3u8",
            "source_type": "hls",
            "referer": "https://a.example/",
        }
        changed_config = {
            **base,
            "referer": "https://b.example/",
        }

        self.assertEqual(source_id_for(base), source_id_for(changed_config))
        self.assertNotEqual(source_revision_for(base), source_revision_for(changed_config))

    def test_adapter_source_id_re_resolves_changing_urls(self):
        source = {"url": "fjtv://fjzh", "source_type": "adapter", "enabled": True, "source_id": "src_fjtv"}
        resolved_urls = ["https://cdn.example/live.m3u8?token=1", "https://cdn.example/live.m3u8?token=2"]
        captured = []

        async def fake_resolve(adapter_url, client):
            return {
                "url": resolved_urls[len(captured)],
                "source_type": "hls",
                "headers": {"Referer": "https://www.fjtv.net/"},
            }

        async def fake_resolved_playlist(**kwargs):
            captured.append(kwargs)
            return types.SimpleNamespace(status_code=200)

        fake_main = types.SimpleNamespace(
            _source_type=lambda s: s.get("source_type") or "hls",
            http_client=object(),
            ProviderReferenceError=ProviderReferenceError,
        )
        fake_main.app = types.SimpleNamespace(
            state=types.SimpleNamespace(provider_resolver=types.SimpleNamespace(resolve=fake_resolve)),
        )
        old_main = sys.modules.get("main")
        old_resolved = media_proxy._serve_resolved_source_playlist
        try:
            sys.modules["main"] = fake_main
            media_proxy._serve_resolved_source_playlist = fake_resolved_playlist
            asyncio.run(media_proxy._serve_iptv_source_playlist(source, "福建综合", MediaAccessContext(source="anonymous")))
            asyncio.run(media_proxy._serve_iptv_source_playlist(source, "福建综合", MediaAccessContext(source="anonymous")))
        finally:
            media_proxy._serve_resolved_source_playlist = old_resolved
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual([item["resolved_url"] for item in captured], resolved_urls)
        self.assertEqual([item["source_id"] for item in captured], ["src_fjtv", "src_fjtv"])

    def test_media_channel_resolve_uses_source_id_and_returns_direct_payload(self):
        source = {"url": "huya://31421", "source_type": "adapter", "enabled": True, "source_id": "src_huya"}

        async def fake_resolve(adapter_url, client):
            self.assertEqual(adapter_url, "huya://31421")
            return {
                "url": "https://example.com/live/room.flv?token=secret",
                "source_type": "http_flv",
                "direct_playable": True,
                "requires_proxy": False,
                "volatile_url": True,
                "headers": {"Referer": "https://www.huya.com/"},
            }

        fake_main = types.SimpleNamespace(
            _get_aggregated_iptv_channels=lambda: asyncio.sleep(0, result=([
                {"canonical_key": "虎牙", "urls": [source]},
            ], [])),
            _source_type=lambda s: s.get("source_type") or "hls",
            http_client=object(),
            ProviderReferenceError=ProviderReferenceError,
        )
        fake_main.app = types.SimpleNamespace(
            state=types.SimpleNamespace(provider_resolver=types.SimpleNamespace(resolve=fake_resolve)),
        )
        old_main = sys.modules.get("main")
        old_assert_safe = media_proxy.assert_safe_target_url

        async def fake_assert_safe(url, **kwargs):
            return None

        try:
            sys.modules["main"] = fake_main
            media_proxy.assert_safe_target_url = fake_assert_safe
            payload = asyncio.run(media_proxy.media_channel_source_resolve(
                "虎牙",
                types.SimpleNamespace(headers={}),
                source_id="src_huya",
                access=MediaAccessContext(source="anonymous"),
            ))
        finally:
            media_proxy.assert_safe_target_url = old_assert_safe
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(payload["source_id"], "src_huya")
        self.assertEqual(payload["source_type"], "http_flv")
        self.assertTrue(payload["direct_playable"])
        self.assertFalse(payload["requires_proxy"])
        self.assertTrue(payload["volatile_url"])
        self.assertIn("/api/media/channel/%E8%99%8E%E7%89%99/playlist.m3u8?source_id=src_huya", payload["proxy_url"])
        self.assertNotIn("token=secret", payload["proxy_url"])

    def test_media_channel_resolve_other_channel_source_id_returns_404(self):
        current_source = {"url": "huya://1", "source_type": "adapter", "enabled": True, "source_id": "src_current"}
        other_source = {"url": "huya://2", "source_type": "adapter", "enabled": True, "source_id": "src_other"}
        fake_main = types.SimpleNamespace(
            _get_aggregated_iptv_channels=lambda: asyncio.sleep(0, result=([
                {"canonical_key": "当前", "urls": [current_source]},
                {"canonical_key": "其他", "urls": [other_source]},
            ], [])),
            _source_type=lambda s: s.get("source_type") or "hls",
        )
        old_main = sys.modules.get("main")
        try:
            sys.modules["main"] = fake_main
            with self.assertRaises(media_proxy.HTTPException) as ctx:
                asyncio.run(media_proxy.media_channel_source_resolve(
                    "当前",
                    types.SimpleNamespace(headers={}),
                    source_id="src_other",
                    access=MediaAccessContext(source="anonymous"),
                ))
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_iptv_playlist_without_source_id_keeps_auto_selection(self):
        selected = {}
        first_source = {"url": "https://first.example/live.m3u8", "source_type": "hls", "enabled": True, "source_id": "src_first"}
        second_source = {"url": "https://second.example/live.m3u8", "source_type": "hls", "enabled": True, "source_id": "src_second"}
        fake_main = types.SimpleNamespace(
            _get_aggregated_iptv_channels=lambda: asyncio.sleep(0, result=([
                {"canonical_key": "频道", "urls": [second_source, first_source]},
            ], [])),
            _sorted_sources=lambda sources: [first_source, second_source],
            _source_type=lambda source: source.get("source_type") or "hls",
        )
        old_main = sys.modules.get("main")
        old_serve = media_proxy._serve_iptv_source_playlist

        async def fake_serve(source, canonical_key, access):
            selected["source"] = source
            return types.SimpleNamespace(status_code=200)

        try:
            sys.modules["main"] = fake_main
            media_proxy._serve_iptv_source_playlist = fake_serve
            response = asyncio.run(media_proxy._serve_iptv_channel_playlist(
                "频道",
                types.SimpleNamespace(headers={}),
                MediaAccessContext(source="anonymous"),
                source_id="",
            ))
        finally:
            media_proxy._serve_iptv_source_playlist = old_serve
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(response.status_code, 200)
        self.assertIs(selected["source"], first_source)

    def test_resolved_stream_handle_and_proxy_context_use_source_id(self):
        reset_proxy_context_for_tests()
        response = asyncio.run(media_proxy._serve_resolved_source_playlist(
            resolved_url="https://stream.example/live.ts",
            resolved_st="mpegts",
            custom_ua="",
            referer="https://referer.example/",
            cookie="",
            no_ua=False,
            canonical_key="频道",
            source_id="src_stream",
            source_revision="rev_stream",
            access=MediaAccessContext(source="anonymous"),
        ))

        self.assertEqual(response.status_code, 307)
        handle = response.headers["location"].split("/api/media/proxy/stream/", 1)[1]
        payload = decode_for_kind(handle, "stream")
        self.assertEqual(payload.src_id, "src_stream")
        ctx = get_proxy_context_registry().get(payload.ctx)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.source_id, "src_stream")
        self.assertEqual(ctx.source_revision, "rev_stream")

    def test_hls_no_ua_alone_creates_proxy_context(self):
        reset_proxy_context_for_tests()
        captured = {}

        async def serve_iptv_playlist_by_source(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(status_code=200)

        fake_main = types.SimpleNamespace(serve_iptv_playlist_by_source=serve_iptv_playlist_by_source)
        old_main = sys.modules.get("main")
        try:
            sys.modules["main"] = fake_main
            response = asyncio.run(media_proxy._serve_resolved_source_playlist(
                resolved_url="https://stream.example/live.m3u8",
                resolved_st="hls",
                custom_ua="",
                referer="",
                cookie="",
                no_ua=True,
                canonical_key="频道",
                source_id="src_hls",
                source_revision="rev_hls",
                access=MediaAccessContext(source="anonymous"),
            ))
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured["ctx_id"])
        ctx = get_proxy_context_registry().get(captured["ctx_id"])
        self.assertIsNotNone(ctx)
        self.assertTrue(ctx.no_ua)
        self.assertEqual(ctx.source_id, "src_hls")
        self.assertEqual(ctx.source_revision, "rev_hls")

    def test_media_channel_playlist_has_no_source_url_parameter(self):
        params = inspect.signature(media_proxy.media_channel_playlist).parameters
        self.assertIn("source_id", params)
        self.assertNotIn("source_url", params)

    def test_media_channel_playlist_rejects_source_url_query(self):
        old_main = sys.modules.get("main")
        fake_main = types.SimpleNamespace(_get_aggregated_iptv_channels=lambda: asyncio.sleep(0, result=([], [])))
        try:
            sys.modules["main"] = fake_main
            with self.assertRaises(media_proxy.HTTPException) as ctx:
                asyncio.run(media_proxy.media_channel_playlist(
                    "频道",
                    types.SimpleNamespace(headers={}, query_params={"source_url": "https://up.example/live.m3u8"}),
                    source_id="",
                    access=MediaAccessContext(source="anonymous"),
                ))
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_chunk_preserves_upstream_404_as_streaming_response(self):
        class FakeUpstreamResponse:
            status_code = 404
            headers = {"content-type": "text/html; charset=utf-8"}
            closed = False

            async def aiter_bytes(self, chunk_size=65536):
                yield b"not found"

            async def aclose(self):
                self.closed = True

        class FakeHttpClient:
            def build_request(self, method, url, headers=None):
                return {"method": method, "url": url, "headers": headers or {}}

            async def send(self, request, stream=False, follow_redirects=False):
                return FakeUpstreamResponse()

        fake_main = types.SimpleNamespace(http_client=FakeHttpClient())
        old_main = sys.modules.get("main")
        old_validate = media_proxy._validate_handle_url_or_403

        async def noop_validate(*args, **kwargs):
            return None

        async def fake_stream(client, method, url, *, headers=None, **_kwargs):
            request = client.build_request(method, url, headers=headers)
            return await client.send(request, stream=True)

        try:
            sys.modules["main"] = fake_main
            media_proxy._validate_handle_url_or_403 = noop_validate
            handle = issue_handle(
                kind="chunk",
                url="https://cdn.example.test/live/expired.ts",
                ttl_seconds=3600,
            )
            with mock.patch.object(media_proxy, "stream_with_safe_redirects", new=fake_stream):
                response = asyncio.run(media_proxy.media_proxy_chunk(
                    handle,
                    types.SimpleNamespace(headers={}, method="GET"),
                    MediaAccessContext(source="anonymous"),
                ))
        finally:
            media_proxy._validate_handle_url_or_403 = old_validate
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.media_type, "text/html; charset=utf-8")

    def test_chunk_get_uses_redirect_guard_as_single_ssrf_boundary(self):
        class FakeUpstreamResponse:
            status_code = 200
            headers = {"content-type": "video/mp2t"}

            async def aiter_raw(self, chunk_size=65536):
                yield b"segment"

            async def aclose(self):
                return None

        fake_main = types.SimpleNamespace(http_client=object())
        old_main = sys.modules.get("main")
        handle = issue_handle(
            kind="chunk",
            url="https://cdn.example.test/live/segment.ts",
            ttl_seconds=3600,
        )
        redirect_guard_calls = []

        async def route_guard(*_args, **_kwargs):
            raise AssertionError("GET must not duplicate the redirect helper's SSRF validation")

        async def fake_stream(_client, _method, url, **_kwargs):
            redirect_guard_calls.append(url)
            return FakeUpstreamResponse()

        try:
            sys.modules["main"] = fake_main
            with mock.patch.object(media_proxy, "_validate_handle_url_or_403", new=route_guard), mock.patch.object(
                media_proxy, "stream_with_safe_redirects", new=fake_stream
            ):
                response = asyncio.run(media_proxy.media_proxy_chunk(
                    handle,
                    types.SimpleNamespace(headers={}, method="GET"),
                    MediaAccessContext(source="anonymous"),
                ))
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(redirect_guard_calls, ["https://cdn.example.test/live/segment.ts"])

    def test_chunk_head_keeps_route_ssrf_validation_without_upstream_request(self):
        fake_main = types.SimpleNamespace(http_client=object())
        old_main = sys.modules.get("main")
        handle = issue_handle(
            kind="chunk",
            url="https://cdn.example.test/live/segment.ts",
            ttl_seconds=3600,
        )
        route_guard_calls = []

        async def route_guard(url, **_kwargs):
            route_guard_calls.append(url)

        async def unexpected_stream(*_args, **_kwargs):
            raise AssertionError("HEAD must not contact upstream")

        try:
            sys.modules["main"] = fake_main
            with mock.patch.object(media_proxy, "_validate_handle_url_or_403", new=route_guard), mock.patch.object(
                media_proxy, "stream_with_safe_redirects", new=unexpected_stream
            ):
                response = asyncio.run(media_proxy.media_proxy_chunk(
                    handle,
                    types.SimpleNamespace(headers={}, method="HEAD"),
                    MediaAccessContext(source="anonymous"),
                ))
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(route_guard_calls, ["https://cdn.example.test/live/segment.ts"])

    def test_chunk_range_and_conditional_headers_preserve_206_response(self):
        captured_headers = {}

        class FakeUpstreamResponse:
            status_code = 206
            headers = {
                "content-type": "video/mp2t",
                "content-length": "100",
                "content-range": "bytes 0-99/1234",
                "accept-ranges": "bytes",
                "etag": '"segment-etag"',
            }

            async def aiter_raw(self, chunk_size=65536):
                yield b"x" * 100

            async def aclose(self):
                return None

        async def fake_stream(_client, _method, _url, *, headers=None, **_kwargs):
            captured_headers.update(headers or {})
            return FakeUpstreamResponse()

        fake_main = types.SimpleNamespace(http_client=object())
        old_main = sys.modules.get("main")
        handle = issue_handle(kind="chunk", url="https://cdn.example.test/live/segment.ts", ttl_seconds=3600)
        request_headers = {
            "range": "bytes=0-99",
            "if-range": '"segment-etag"',
            "if-none-match": '"segment-etag"',
            "if-modified-since": "Wed, 01 Jan 2025 00:00:00 GMT",
        }
        try:
            sys.modules["main"] = fake_main
            with mock.patch.object(media_proxy, "stream_with_safe_redirects", new=fake_stream):
                response = asyncio.run(media_proxy.media_proxy_chunk(
                    handle,
                    types.SimpleNamespace(headers=request_headers, method="GET"),
                    MediaAccessContext(source="anonymous"),
                ))
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-99/1234")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["content-length"], "100")
        self.assertEqual(captured_headers["Range"], "bytes=0-99")
        self.assertEqual(captured_headers["If-Range"], '"segment-etag"')
        self.assertEqual(captured_headers["If-None-Match"], '"segment-etag"')
        self.assertEqual(captured_headers["If-Modified-Since"], "Wed, 01 Jan 2025 00:00:00 GMT")

    def test_chunk_preserves_upstream_416_and_content_range(self):
        class FakeUpstreamResponse:
            status_code = 416
            headers = {
                "content-type": "text/plain",
                "content-length": "0",
                "content-range": "bytes */1234",
                "accept-ranges": "bytes",
            }

            async def aiter_raw(self, chunk_size=65536):
                if False:
                    yield b""

            async def aclose(self):
                return None

        async def fake_stream(*_args, **_kwargs):
            return FakeUpstreamResponse()

        fake_main = types.SimpleNamespace(http_client=object())
        old_main = sys.modules.get("main")
        handle = issue_handle(kind="chunk", url="https://cdn.example.test/live/segment.ts", ttl_seconds=3600)
        try:
            sys.modules["main"] = fake_main
            with mock.patch.object(media_proxy, "stream_with_safe_redirects", new=fake_stream):
                response = asyncio.run(media_proxy.media_proxy_chunk(
                    handle,
                    types.SimpleNamespace(headers={"range": "bytes=9999-"}, method="GET"),
                    MediaAccessContext(source="anonymous"),
                ))
        finally:
            if old_main is not None:
                sys.modules["main"] = old_main
            else:
                sys.modules.pop("main", None)

        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], "bytes */1234")
        self.assertEqual(response.headers["accept-ranges"], "bytes")


if __name__ == "__main__":
    unittest.main()
