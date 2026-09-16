from __future__ import annotations

import json
import unittest

from plugin_runtime import validate_manifest
from waveflow_plugin_sdk import PluginError, RadioReference, ResolveContext

from bundled_plugins.myradio.plugin import Provider


class _Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status


class _Capabilities:
    def __init__(self, *, unavailable=False):
        self.calls = []
        self.unavailable = unavailable

    def managed_http(self, url, *, method="GET", headers=None, query=None,
                     json_body=None, text_body=None, response_mode="text", timeout=10):
        self.calls.append({"url": url, "method": method, "headers": headers or {}, "body": text_body,
                           "mode": response_mode})
        if self.unavailable:
            raise PluginError("TEMPORARY_UPSTREAM_FAILURE", "fixture upstream unavailable")
        if url.endswith("/sitemap.xml"):
            return _Response("<loc>https://myradio.com.tw/radios/A0101</loc>")
        if "A0101.json" in url:
            return _Response({"pageProps": {"radio": {"id": "A0101", "name": "POP 台北", "url": "myPop:1"}}})
        if "pop.olis.com.tw" in url:
            return _Response({"data": {"hlsurl": {"1": "https://stream.example/pop.m3u8"}}})
        raise AssertionError(url)


def _context(capabilities):
    return ResolveContext("myradio-test", 0, {}, capabilities, lambda: False)


class MyRadioPluginTest(unittest.TestCase):
    def test_manifest_and_static_dynamic_catalog(self):
        manifest = json.load(open("backend/bundled_plugins/myradio/manifest.json", encoding="utf-8"))
        self.assertEqual(validate_manifest(manifest).identity, "org.waveflow/myradio")
        capabilities = _Capabilities()
        catalog = Provider().catalog({}, _context(capabilities))
        ids = {item["station_ref"]["provider_station_id"] for item in catalog["stations"]}
        self.assertGreaterEqual(len(ids), 85)
        self.assertIn("A0101", ids)
        self.assertEqual(next(item for item in catalog["stations"] if item["station_ref"]["provider_station_id"] == "A0101")["playback_config"]["stream_url"],
                         "https://stream.example/pop.m3u8")
        self.assertTrue(any(call["method"] == "POST" for call in capabilities.calls))

    def test_explicit_station_id_resolve_has_no_name_fallback(self):
        provider = Provider()
        context = _context(_Capabilities())
        reference = RadioReference("myradio", "A0202", {"stream_url": "https://stream.example/live.mp3"})
        descriptor = provider.resolve_stream(reference, context)
        self.assertEqual(descriptor.transport, "audio_http")
        self.assertEqual(descriptor.url, "https://stream.example/live.mp3")
        self.assertEqual(descriptor.ttl_seconds, 24 * 60 * 60)
        self.assertTrue(descriptor.volatile_url)
        self.assertFalse(descriptor.requires_proxy)
        with self.assertRaises(PluginError):
            provider.resolve_stream(RadioReference("myradio", "A0202", {}), context)

    def test_dynamic_upstream_failure_preserves_static_catalog(self):
        catalog = Provider().catalog({}, _context(_Capabilities(unavailable=True)))
        self.assertEqual(len(catalog["stations"]), 85)


if __name__ == "__main__":
    unittest.main()
