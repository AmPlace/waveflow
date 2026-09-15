from __future__ import annotations

import types
import unittest
from unittest import mock

from starlette.requests import Request


def _request() -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/",
        "scheme": "https", "query_string": b"",
        "headers": [(b"host", b"waveflow.example")],
        "server": ("waveflow.example", 443), "client": ("127.0.0.1", 1),
    })


def _source(url: str, **extra) -> dict:
    return {
        "url": url, "source_type": "hls", "is_working": 1, "enabled": True,
        "source_id": f"src-{len(url)}", **extra,
    }


class SubscriptionExportContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_modes_are_single_source_semantics(self):
        import main

        channel = {
            "canonical_key": "fixture",
            "urls": [
                _source("https://direct.example/live.m3u8"),
                _source("https://header.example/live.m3u8", custom_ua="UA/1"),
                _source("https://adapter.example/live", source_type="adapter", adapter_provider="fixture"),
                _source("https://rtsp.example/live", source_type="rtsp"),
            ],
        }
        request = _request()
        access = types.SimpleNamespace(propagated_access_token="credential-token")

        smart = main._subscription_urls_for_channel(
            channel, "smart", request, True, access=access,
        )
        hybrid = main._subscription_urls_for_channel(
            channel, "hybrid", request, True, access=access,
        )
        direct = main._subscription_urls_for_channel(
            channel, "direct", request, True, access=access,
        )
        proxy = main._subscription_urls_for_channel(
            channel, "proxy", request, True, access=access,
        )

        self.assertEqual(len(smart), 1)
        self.assertIn("access_token=credential-token", smart[0][0])
        self.assertEqual(len(hybrid), 4)
        self.assertEqual(len(direct), 1)
        self.assertEqual(direct[0][0], "https://direct.example/live.m3u8")
        self.assertEqual(len(proxy), 4)
        self.assertTrue(all('source_id=' in url for url, _ in proxy))
        self.assertTrue(all("/api/media/channel/fixture/playlist.m3u8" in url for url, _ in proxy))
        self.assertTrue(all("direct.example" not in url for url, _ in proxy))

    async def test_smart_redirects_direct_safe_and_proxies_other_sources_with_token(self):
        import main

        request = _request()
        access = types.SimpleNamespace(propagated_access_token="credential-token")
        direct_channel = {"canonical_key": "direct", "urls": [_source("https://direct.example/live.m3u8")]}
        proxy_channel = {"canonical_key": "proxy", "urls": [_source("https://proxy.example/live.m3u8", custom_ua="UA/1")]}

        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([direct_channel], []),
        )), mock.patch.object(main, "assert_safe_target_url", new=mock.AsyncMock()):
            direct_response = await main.iptv_smart_playlist("direct", request, access)
        self.assertEqual(direct_response.status_code, 307)
        self.assertEqual(direct_response.headers["location"], "https://direct.example/live.m3u8")

        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([proxy_channel], []),
        )):
            proxy_response = await main.iptv_smart_playlist("proxy", request, access)
        self.assertEqual(proxy_response.status_code, 307)
        self.assertIn("/api/media/channel/proxy/playlist.m3u8", proxy_response.headers["location"])
        self.assertIn("access_token=credential-token", proxy_response.headers["location"])

    async def test_distinct_proxy_sources_keep_their_identity(self):
        import main
        channel = {'canonical_key': 'one', 'urls': [
            _source('https://example.test/a', source_id='a'),
            _source('https://example.test/b', source_id='b'),
        ]}
        urls = main._subscription_urls_for_channel(channel, 'proxy', _request(), True)
        self.assertEqual(len({url for url, _ in urls}), 2)
        self.assertIn('source_id=a', urls[0][0])
        self.assertIn('source_id=b', urls[1][0])

    async def test_explicit_constraints_are_never_direct(self):
        import main
        for field, value in [('custom_ua', 'UA'), ('referer', 'https://example.test'),
                             ('headers', {'X-Test': 'value'}), ('volatile_url', True),
                             ('credential_refs', ['ref']), ('requires_proxy', True),
                             ('direct_playable', False), ('hidden_upstream', True)]:
            with self.subTest(field=field):
                self.assertFalse(main._source_direct_safe(_source('https://example.test/a', **{field: value})))

    async def test_smart_prefers_healthy_source_but_does_not_require_it(self):
        """Health is ordering, not a gate: a channel whose only source is not yet
        marked healthy must still get the stable Smart URL instead of a false 503.
        """
        import main

        request = _request()
        access = types.SimpleNamespace(propagated_access_token="")
        unhealthy_only = {
            "canonical_key": "unhealthy",
            "urls": [_source("https://only.example/live.m3u8", is_working=0)],
        }
        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([unhealthy_only], []),
        )), mock.patch.object(main, "assert_safe_target_url", new=mock.AsyncMock()):
            response = await main.iptv_smart_playlist("unhealthy", request, access)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "https://only.example/live.m3u8")

        # A healthy source still wins over an unhealthy one.
        mixed = {
            "canonical_key": "mixed",
            "urls": [
                _source("https://stale.example/live.m3u8", is_working=0, source_id="stale"),
                _source("https://fresh.example/live.m3u8", is_working=1, source_id="fresh"),
            ],
        }
        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([mixed], []),
        )), mock.patch.object(main, "assert_safe_target_url", new=mock.AsyncMock()):
            response = await main.iptv_smart_playlist("mixed", request, access)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "https://fresh.example/live.m3u8")

    async def test_smart_proxy_redirect_keeps_channel_key_and_source_identity(self):
        """The proxy fallback must carry the aggregated channel key plus the
        explicit source_id, and must never leak the upstream URL.
        """
        import main

        request = _request()
        access = types.SimpleNamespace(propagated_access_token="credential-token")
        channel = {
            "canonical_key": "cctv5",
            "urls": [_source("https://hidden.example/live.m3u8", custom_ua="UA/1", source_id="src-abc")],
        }
        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([channel], []),
        )):
            response = await main.iptv_smart_playlist("cctv5", request, access)
        location = response.headers["location"]
        self.assertEqual(response.status_code, 307)
        self.assertIn("/api/media/channel/cctv5/playlist.m3u8", location)
        self.assertIn("source_id=src-abc", location)
        self.assertIn("access_token=credential-token", location)
        self.assertNotIn("hidden.example", location)

    async def test_channel_proxy_url_requires_explicit_channel_identity(self):
        """canonical_key is the channel boundary; a missing key must fail loudly
        instead of fabricating an unresolvable URL-hash channel entry.
        """
        import main
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            main._iptv_proxy_url_for_channel("", _source("https://example.test/a"), _request())
        self.assertEqual(ctx.exception.status_code, 500)

    async def test_include_rtsp_query_cannot_re_enable_raw_rtsp_export(self):
        """The legacy include_rtsp parameter stays accepted but is inert: RTSP is
        Core-only in every export mode.
        """
        import main

        channel = {
            "canonical_key": "camera",
            "name": "Camera",
            "urls": [
                _source("https://direct.example/live.m3u8", source_id="direct"),
                _source("rtsp://camera.example/live", source_type="rtsp", source_id="camera"),
            ],
        }
        access = types.SimpleNamespace(propagated_access_token="")
        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([channel], []),
        )):
            response = await main.export_iptv_subscription(
                request=_request(), mode="direct", include_rtsp=True, access=access,
            )
        body = response.body.decode()
        self.assertIn("https://direct.example/live.m3u8", body)
        self.assertNotIn("rtsp://", body)

    async def test_smart_error_semantics_are_core_shaped(self):
        """Unknown channel -> 404; a channel with no eligible source -> 503.
        Both stay stable because the route owns the contract, not the transport.
        """
        import main
        from fastapi import HTTPException

        request = _request()
        access = types.SimpleNamespace(propagated_access_token="")

        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([], []),
        )):
            with self.assertRaises(HTTPException) as missing:
                await main.iptv_smart_playlist("nope", request, access)
        self.assertEqual(missing.exception.status_code, 404)

        # A YouTube-only channel is not export-eligible in any mode, so Smart has
        # no candidate and must fail closed rather than redirect something raw.
        youtube_only = {
            "canonical_key": "yt",
            "urls": [_source("https://www.youtube.com/watch?v=fixture", source_type="youtube")],
        }
        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([youtube_only], []),
        )):
            with self.assertRaises(HTTPException) as unavailable:
                await main.iptv_smart_playlist("yt", request, access)
        self.assertEqual(unavailable.exception.status_code, 503)

        # A disabled source is not eligible either.
        disabled = {
            "canonical_key": "off",
            "urls": [_source("https://off.example/live.m3u8", enabled=False)],
        }
        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([disabled], []),
        )):
            with self.assertRaises(HTTPException) as disabled_error:
                await main.iptv_smart_playlist("off", request, access)
        self.assertEqual(disabled_error.exception.status_code, 503)

    async def test_smart_skips_ssrf_rejected_direct_source(self):
        """A direct-safe source still has to pass the SSRF check before Smart
        hands its raw URL to an external client. A rejected source must fall
        through to the next candidate, and a channel whose only candidate is
        rejected must fail closed instead of redirecting it anyway.
        """
        import main
        from fastapi import HTTPException

        request = _request()
        access = types.SimpleNamespace(propagated_access_token="")
        blocked = _source("https://blocked.example/live.m3u8", source_id="blocked")
        fallback = _source("https://fallback.example/live.m3u8", source_id="fallback")
        channel = {"canonical_key": "mixed", "urls": [blocked, fallback]}

        def reject(url, **_kwargs):
            if "blocked.example" in url:
                raise main.UnsafeTargetError("unsafe target")
            return None

        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([channel], []),
        )), mock.patch.object(main, "assert_safe_target_url", new=mock.AsyncMock(side_effect=reject)):
            response = await main.iptv_smart_playlist("mixed", request, access)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "https://fallback.example/live.m3u8")

        only_blocked = {"canonical_key": "blocked", "urls": [blocked]}
        with mock.patch.object(main, "_get_aggregated_iptv_channels", new=mock.AsyncMock(
            return_value=([only_blocked], []),
        )), mock.patch.object(main, "assert_safe_target_url", new=mock.AsyncMock(side_effect=reject)):
            with self.assertRaises(HTTPException) as rejected:
                await main.iptv_smart_playlist("blocked", request, access)
        self.assertEqual(rejected.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
