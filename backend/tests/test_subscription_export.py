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
            channel, "smart", request, True, False, access=access,
        )
        hybrid = main._subscription_urls_for_channel(
            channel, "hybrid", request, True, False, access=access,
        )
        direct = main._subscription_urls_for_channel(
            channel, "direct", request, True, False, access=access,
        )
        proxy = main._subscription_urls_for_channel(
            channel, "proxy", request, True, False, access=access,
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
        urls = main._subscription_urls_for_channel(channel, 'proxy', _request(), True, False)
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


if __name__ == "__main__":
    unittest.main()
