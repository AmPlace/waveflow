from __future__ import annotations

import json
import unittest

from plugin_runtime import validate_manifest
from plugin_runtime.validation import validate_radio_catalog
from waveflow_plugin_sdk import PluginError, RadioReference, ResolveContext

from bundled_plugins.hitfm.plugin import REFRESH_TTL_SECONDS, Provider


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body


class _Capabilities:
    def __init__(self, body="https://stream.example/live.m3u8", error=None):
        self.body = body
        self.error = error
        self.calls = []

    def managed_http(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return _Response(self.body)


def _context(capabilities):
    return ResolveContext("hitfm-test", 0, {}, capabilities, lambda: False)


class HitFmPluginTest(unittest.TestCase):
    def test_catalog_covers_hitfm_and_pop_with_stable_station_identity(self):
        provider = Provider()
        catalog = provider.catalog({}, _context(_Capabilities()))
        ids = [item["station_ref"]["provider_station_id"] for item in catalog["stations"]]
        self.assertEqual(ids, ["hitfm", "hitfm_taichung", "hitfm_tainan", "hitfm_yilan", "hitfm_huadong", "pop917"])
        self.assertTrue(all(item["station_ref"]["provider_key"] == "hitfm" for item in catalog["stations"]))
        self.assertTrue(all(not item["logo_url"] or item["logo_url"].startswith("https://")
                            for item in catalog["stations"]))
        self.assertEqual(len(validate_radio_catalog(catalog, owned_schemes={"hitfm"})["stations"]), 6)
        with open("backend/bundled_plugins/hitfm/manifest.json", encoding="utf-8") as manifest_file:
            manifest = validate_manifest(json.load(manifest_file))
        self.assertEqual(manifest.identity, "org.waveflow/hitfm")
        self.assertEqual(manifest.version, "1.0.1")

    def test_dynamic_url_and_ttl_descriptor(self):
        capabilities = _Capabilities()
        descriptor = Provider().resolve_stream(
            RadioReference("hitfm", "pop917", {
                "channel_id": "1", "api": "https://www.pop917.com/ajax.aspx",
                "referer": "https://www.pop917.com/liveStream.aspx?id=1", "origin": "https://www.pop917.com",
            }),
            _context(capabilities),
        )
        self.assertEqual(descriptor.url, "https://stream.example/live.m3u8")
        self.assertEqual(descriptor.transport, "hls")
        self.assertEqual(descriptor.ttl_seconds, REFRESH_TTL_SECONDS)
        self.assertTrue(descriptor.volatile_url)
        self.assertFalse(descriptor.requires_proxy)
        self.assertIn("channelID=1&action=getLIVEURL", capabilities.calls[0][1]["text_body"])

    def test_malformed_or_upstream_failure_is_not_success(self):
        with self.assertRaises(PluginError) as raised:
            Provider().resolve_stream(
                RadioReference("hitfm", "hitfm", {"channel_id": "1"}),
                _context(_Capabilities(body="not-a-url")),
            )
        self.assertEqual(raised.exception.code, "TEMPORARY_UPSTREAM_FAILURE")


if __name__ == "__main__":
    unittest.main()
