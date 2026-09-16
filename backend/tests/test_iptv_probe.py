import unittest
import os
import tempfile
import types
import json
from unittest import mock

from adapters import AdapterResolveError, parse_adapter_url
from adapters.youtube import resolve_youtube
import database
import iptv_probe
from plugin_runtime import PluginError
from iptv_probe import (
    _empty_result,
    _enrich_with_ffprobe,
    _ffmpeg_input_timeout_args,
    _parse_ffmpeg_output,
    probe_channel_source,
)


class IptvProbeFfmpegArgsTest(unittest.TestCase):
    def test_rtsp_uses_rtsp_demuxer_timeout(self):
        self.assertEqual(_ffmpeg_input_timeout_args("rtsp://example/live", 7.0), ["-timeout", "7000000"])

    def test_http_uses_protocol_read_timeout(self):
        self.assertEqual(_ffmpeg_input_timeout_args("https://example/live.m3u8", 7.0), ["-rw_timeout", "7000000"])

    def test_ffmpeg_banner_parses_quality_fields(self):
        output = """
Input #0, rtsp, from 'rtsp://example/live':
  Duration: N/A, start: 0.000000, bitrate: 2048 kb/s
  Stream #0:0: Video: h264 (Main), yuv420p, 1920x1080, 25 fps, 25 tbr, 90k tbn
  Stream #0:1: Audio: aac, 48000 Hz, stereo, fltp, 128 kb/s
"""

        parsed = _parse_ffmpeg_output(output)

        self.assertEqual(parsed["resolution"], "1920x1080")
        self.assertEqual(parsed["video_codec"], "h264")
        self.assertEqual(parsed["audio_codec"], "aac")
        self.assertEqual(parsed["fps"], 25)
        self.assertEqual(parsed["speed_mbps"], 0.25)


class RemovedLegacyProviderSchemeTest(unittest.TestCase):
    def test_removed_schemes_are_not_registered_or_parseable(self):
        from adapters import _ADAPTER_REGISTRY

        for scheme in ("haixiu", "liveme", "lehai"):
            with self.subTest(scheme=scheme):
                self.assertNotIn(scheme, _ADAPTER_REGISTRY)
                with self.assertRaises(AdapterResolveError) as direct_ctx:
                    parse_adapter_url(f"{scheme}://room-1")
                self.assertEqual(direct_ctx.exception.error_code, "invalid_adapter_url")

                with self.assertRaises(AdapterResolveError) as compat_ctx:
                    parse_adapter_url(f"adapter://{scheme}/room-1")
                self.assertEqual(compat_ctx.exception.error_code, "unsupported_adapter")


class IptvProbeRealtimeStreamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        self._tmp = tempfile.TemporaryDirectory(prefix="waveflow-probe-")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmp.name, "waveflow.db")
        await database.initialize()

    async def asyncTearDown(self):
        if self._old_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self._old_db_path
        self._tmp.cleanup()

    async def test_production_probe_fails_closed_for_plugin_owner_but_keeps_legacy_owner(self):
        from provider_resolver import ProviderResolver

        legacy = mock.AsyncMock(return_value={
            "adapter": "jstv", "url": "https://legacy.example/live.m3u8",
            "source_type": "hls", "headers": {},
        })
        plugin_owned = ProviderResolver.from_ownership_rows([{
            "scheme": "jstv", "mode": "plugin", "plugin_identity": "org.waveflow/jstv",
        }], runtime=None, legacy_resolver=legacy)
        unavailable = await probe_channel_source(
            {"url": "jstv://jsws", "source_type": "adapter"}, None,
            provider_resolver=plugin_owned,
        )
        self.assertEqual((unavailable["probe_status"], unavailable["probe_method"]),
                         ("error", "adapter_resolve"))
        self.assertIn('"error_code":"PLUGIN_UNAVAILABLE"', unavailable["probe_meta_json"])
        legacy.assert_not_awaited()

        legacy_owned = ProviderResolver.from_ownership_rows([{
            "scheme": "jstv", "mode": "legacy", "plugin_identity": "",
        }], runtime=None, legacy_resolver=legacy)
        with mock.patch.object(
            iptv_probe, "_probe_hls",
            new=mock.AsyncMock(return_value=_empty_result(
                probe_status="online", live_status="live", probe_method="http_segment",
            )),
        ), mock.patch.object(iptv_probe, "_enrich_with_ffprobe", new=mock.AsyncMock(side_effect=lambda result, *_: result)):
            available = await probe_channel_source(
                {"url": "jstv://jsws", "source_type": "adapter"}, None,
                provider_resolver=legacy_owned,
            )
        self.assertEqual(available["probe_status"], "online")
        legacy.assert_awaited_once()

    async def test_plugin_not_live_maps_to_existing_probe_taxonomy(self):
        class Resolver:
            async def resolve(self, _url, _client):
                raise PluginError("NOT_LIVE", "Provider is not live", retryable=True, category="provider")

        result = await probe_channel_source(
            {"url": "fjtv://fjzh", "source_type": "adapter"}, None, provider_resolver=Resolver(),
        )

        self.assertEqual((result["probe_status"], result["live_status"]), ("not_live", "not_live"))
        self.assertIn('"error_code":"NOT_LIVE"', result["probe_meta_json"])

    async def test_rtsp_does_not_run_duplicate_ffmpeg_fallback(self):
        calls = 0
        original_probe = iptv_probe._probe_with_ffmpeg

        async def fake_probe(url, headers, *, reason):
            nonlocal calls
            calls += 1
            return _empty_result(probe_status="offline", probe_method="ffmpeg", last_error="no_stream")

        iptv_probe._probe_with_ffmpeg = fake_probe
        try:
            result = await probe_channel_source({"url": "rtsp://example/live", "source_type": "rtsp"}, None)
        finally:
            iptv_probe._probe_with_ffmpeg = original_probe

        self.assertEqual(result["probe_status"], "offline")
        self.assertEqual(calls, 1)

    async def test_ffmpeg_online_result_skips_ffprobe_enrichment(self):
        original_probe_media_info = iptv_probe._probe_media_info

        async def fail_probe_media_info(url, headers):
            raise AssertionError("ffprobe should not run after ffmpeg already validated the stream")

        iptv_probe._probe_media_info = fail_probe_media_info
        try:
            result = await _enrich_with_ffprobe(
                _empty_result(probe_status="online", probe_method="ffmpeg", video_codec="h264"),
                "rtsp://example/live",
                {},
            )
        finally:
            iptv_probe._probe_media_info = original_probe_media_info

        self.assertEqual(result["probe_status"], "online")
        self.assertEqual(result["video_codec"], "h264")

    async def test_youtube_probe_resolves_before_stream_probe(self):
        resolved_targets = []
        probed_urls = []
        original_probe_hls = iptv_probe._probe_hls
        original_enrich = iptv_probe._enrich_with_ffprobe

        async def fake_resolve(target_url, client):
            resolved_targets.append(target_url)
            return {
                "adapter": "youtube",
                "source_type": "hls",
                "url": "https://example.com/live.m3u8",
                "headers": {},
                "ttl": 120,
            }

        async def fake_probe_hls(client, url, headers):
            probed_urls.append(url)
            return _empty_result(probe_status="online", live_status="live", probe_method="http_segment")

        async def fake_enrich(result, url, headers):
            return result

        resolver = types.SimpleNamespace(resolve=fake_resolve)

        iptv_probe._probe_hls = fake_probe_hls
        iptv_probe._enrich_with_ffprobe = fake_enrich
        try:
            result = await probe_channel_source(
                {"url": "https://www.youtube.com/live/abcDEF123_4", "source_type": "youtube"},
                None, provider_resolver=resolver,
            )
        finally:
            iptv_probe._probe_hls = original_probe_hls
            iptv_probe._enrich_with_ffprobe = original_enrich

        self.assertEqual(result["probe_status"], "online")
        self.assertTrue(resolved_targets[0].startswith("youtube://resolve?probe=1&url="))
        self.assertEqual(probed_urls, ["https://example.com/live.m3u8"])

    async def test_youtube_probe_only_counts_online_without_stream_probe(self):
        original_probe_hls = iptv_probe._probe_hls

        async def fake_resolve(target_url, client):
            return {
                "adapter": "youtube",
                "source_type": "probe_only",
                "url": "",
                "headers": {},
                "youtube_video_id": "abcDEF123_4",
                "youtube_page_is_live": True,
            }

        async def fail_probe_hls(client, url, headers):
            raise AssertionError("probe_only must not read the YouTube stream")

        resolver = types.SimpleNamespace(resolve=fake_resolve)

        iptv_probe._probe_hls = fail_probe_hls
        try:
            result = await probe_channel_source(
                {"url": "https://www.youtube.com/live/abcDEF123_4", "source_type": "youtube"},
                None, provider_resolver=resolver,
            )
        finally:
            iptv_probe._probe_hls = original_probe_hls

        self.assertEqual(result["probe_status"], "online")
        self.assertEqual(result["live_status"], "live")
        self.assertEqual(result["probe_method"], "adapter_probe_only")
        self.assertEqual(result["youtube_video_id"], "abcDEF123_4")

    async def test_generic_descriptor_metadata_reaches_probe_observations_only(self):
        original_probe_hls = iptv_probe._probe_hls
        original_enrich = iptv_probe._enrich_with_ffprobe

        async def fake_resolve(_target_url, _client):
            return {
                "adapter": "synthetic",
                "source_type": "hls",
                "url": "https://example.com/live.m3u8",
                "headers": {},
                "provider_diagnostics": {"channel_id": "channel-1", "page_live": True},
                "probe_hints": {"preferred_probe": "http_segment", "probe_status": "online"},
                "requires_proxy": True,
            }

        async def fake_probe(_client, _url, _headers):
            return _empty_result(probe_status="offline", live_status="error", probe_method="http_segment")

        async def fake_enrich(result, _url, _headers):
            return result

        resolver = types.SimpleNamespace(resolve=fake_resolve)
        iptv_probe._probe_hls = fake_probe
        iptv_probe._enrich_with_ffprobe = fake_enrich
        try:
            result = await probe_channel_source(
                {"url": "synthetic://channel-1", "source_type": "adapter"},
                None,
                provider_resolver=resolver,
            )
        finally:
            iptv_probe._probe_hls = original_probe_hls
            iptv_probe._enrich_with_ffprobe = original_enrich

        self.assertEqual(result["probe_status"], "offline")
        self.assertIn('"channel_id":"channel-1"', result["probe_meta_json"])
        self.assertIn('"preferred_probe":"http_segment"', result["probe_meta_json"])
        self.assertIn('"source_type":"hls"', result["probe_meta_json"])
        metadata = json.loads(result["probe_meta_json"])
        self.assertNotEqual(metadata.get("probe_status"), "online")


class YoutubeAdapterProbeOnlyTest(unittest.IsolatedAsyncioTestCase):
    def _request(self):
        return parse_adapter_url(
            "youtube://resolve?probe=1&url=https%3A%2F%2Fwww.youtube.com%2Flive%2FabcDEF123_4"
        )

    async def test_probe_only_live_page_returns_metadata_only(self):
        class Client:
            async def get(self, *args, **kwargs):
                return type("Response", (), {
                    "status_code": 200,
                    "text": (
                        "window['ytCommand'] = {\"watchEndpoint\":{\"videoId\":\"abcDEF123_4\"}};"
                        '{"videoPrimaryInfoRenderer":{"viewCount":{"videoViewCountRenderer":'
                        '{"viewCount":{"runs":[{"text":"1"},{"text":" watching now"}]},"isLive":true}}},'
                        '"videoSecondaryInfoRenderer":{}}'
                    ),
                })()

        result = await resolve_youtube(self._request(), Client())

        self.assertEqual(result["source_type"], "probe_only")
        self.assertEqual(result["url"], "")
        self.assertEqual(result["youtube_video_id"], "abcDEF123_4")
        self.assertTrue(result["youtube_page_is_live"])

    async def test_probe_only_prefers_explicit_url_video_id(self):
        request = parse_adapter_url(
            "youtube://resolve?probe=1&url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DcMgDo5cH-Lg"
        )

        class Client:
            async def get(self, *args, **kwargs):
                return type("Response", (), {
                    "status_code": 200,
                    "text": (
                        "window['ytCommand'] = {\"watchEndpoint\":{\"videoId\":\"KvwmJIjntvw\"}};"
                        '{"videoPrimaryInfoRenderer":{"viewCount":{"videoViewCountRenderer":'
                        '{"viewCount":{"runs":[{"text":"1"},{"text":" watching now"}]},"isLive":true}}},'
                        '"videoSecondaryInfoRenderer":{}}'
                    ),
                })()

        result = await resolve_youtube(request, Client())

        self.assertEqual(result["youtube_video_id"], "cMgDo5cH-Lg")

    async def test_probe_only_not_live_page_raises_not_live(self):
        class Client:
            async def get(self, *args, **kwargs):
                return type("Response", (), {"status_code": 200, "text": "<html></html>"})()

        with self.assertRaises(AdapterResolveError) as ctx:
            await resolve_youtube(self._request(), Client())

        self.assertEqual(ctx.exception.error_code, "youtube_not_live")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.youtube_video_id, "abcDEF123_4")

    async def test_probe_only_unreachable_page_is_retryable_error(self):
        class Client:
            async def get(self, *args, **kwargs):
                return type("Response", (), {"status_code": 403, "text": ""})()

        with self.assertRaises(AdapterResolveError) as ctx:
            await resolve_youtube(self._request(), Client())

        self.assertEqual(ctx.exception.error_code, "youtube_page_unreachable")
        self.assertTrue(ctx.exception.retryable)


if __name__ == "__main__":
    unittest.main()
