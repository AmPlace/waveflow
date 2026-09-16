import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from urllib.parse import parse_qs, urlsplit
from uuid import UUID as StationUUID

from bundled_plugins.hk_sg_radio.plugin import Provider as StaticProvider
from bundled_plugins.radiobrowser.plugin import Provider as BrowserProvider
from plugin_runtime.validation import validate_radio_catalog, validate_stream_descriptor
from waveflow_plugin_sdk import InvalidResource, PluginError, RadioReference, ResolveContext


ROOT = Path(__file__).resolve().parents[1]
UUID = "01234567-89ab-cdef-0123-456789abcdef"


class Capabilities:
    def __init__(self, body, status=200):
        self.body, self.status, self.calls = body, status, []

    def managed_http(self, url, **options):
        self.calls.append((url, options))
        return SimpleNamespace(body=self.body, status=self.status)


def context(capabilities=None):
    return ResolveContext("fixture", 0, {}, capabilities)


class RadioCatalogPluginsTest(unittest.TestCase):
    def test_hk_sg_catalog_is_owned_by_the_plugin_not_radio_core(self):
        core_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("STATIC_STATIONS", core_source)
        self.assertNotIn("_TINGFM_STREAMS", core_source)
        self.assertNotIn("CURRENT_STREAMS", core_source)
        provider = StaticProvider()
        catalog = validate_radio_catalog(provider.catalog({}, context()), owned_schemes={"hksgradio"})
        self.assertEqual(len(catalog["stations"]), 34)
        for row in catalog["stations"]:
            reference = row["station_ref"]
            self.assertEqual(reference["provider_key"], "hksgradio")
            descriptor = provider.resolve_stream(RadioReference(
                "hksgradio", reference["provider_station_id"], row["playback_config"],
            ), context())
            validate_stream_descriptor(descriptor.as_contract())
            self.assertTrue(descriptor.url.startswith(("http://", "https://")))

    def test_static_plugin_rejects_unknown_identity_and_changed_catalog_revision(self):
        provider = StaticProvider()
        for reference in (RadioReference("hksgradio", "unknown"), RadioReference("other", "tf_909"),
                          RadioReference("hksgradio", "tf_909", {"stream_revision": "old"})):
            with self.subTest(reference=reference):
                with self.assertRaises(InvalidResource):
                    provider.resolve_stream(reference, context())

    def test_radiobrowser_projects_explicit_metadata_without_taxonomy(self):
        row = {"stationuuid": UUID, "name": "Sports Music 北京", "url_resolved": "https://media.example/stream",
               "favicon": "https://media.example/icon.png", "countrycode": "DE", "state": "Region X",
               "languagecodes": "deu", "tags": "custom,rock", "hls": 0}
        capabilities = Capabilities([row, row])
        catalog = validate_radio_catalog(BrowserProvider().catalog({}, context(capabilities)), owned_schemes={"radiobrowser"})
        self.assertEqual(len(catalog["stations"]), 1)
        station = catalog["stations"][0]
        self.assertEqual(station["name"], row["name"])
        self.assertEqual(station["country"], "DE")
        self.assertEqual(station["metadata"]["tags"], ["custom", "rock"])
        self.assertNotIn("tag", station["metadata"])
        self.assertNotIn("url_resolved", json.dumps(station))
        self.assertIn("limit=100", capabilities.calls[0][0])
        self.assertIn("offset=0", capabilities.calls[0][0])
        self.assertEqual(capabilities.calls[0][1]["headers"]["User-Agent"], "WaveFlow-RadioBrowser/1.0")

    def test_radiobrowser_projects_extended_explicit_metadata(self):
        row = {"stationuuid": UUID, "name": "Fixture", "url_resolved": "https://media.example/stream",
               "countrycode": "DE", "hls": 0, "bitrate": 128,
               "homepage": "https://radio.example/home", "tags": "news"}
        station = BrowserProvider().catalog({}, context(Capabilities([row])))['stations'][0]
        assert station['metadata']['bitrate'] == 128
        assert station['metadata']['homepage'] == row['homepage']

    def test_radiobrowser_resolves_by_uuid_and_explicit_hls_flag_not_url_suffix(self):
        for hls, suffix, transport in ((0, "live.m3u8", "audio_http"), (1, "opaque", "hls")):
            with self.subTest(hls=hls):
                capabilities = Capabilities([{"stationuuid": UUID, "url_resolved": "https://media.example/" + suffix, "hls": hls}])
                descriptor = BrowserProvider().resolve_stream(RadioReference("radiobrowser", UUID), context(capabilities))
                self.assertEqual(descriptor.transport, transport)
                self.assertTrue(capabilities.calls[0][0].endswith("/byuuid/" + UUID))
                validate_stream_descriptor(descriptor.as_contract())

    def test_radiobrowser_forwards_explicit_filter_search_and_cursor_without_core_inference(self):
        queries = []

        class Pages:
            def managed_http(self, url, **_options):
                query = parse_qs(urlsplit(url).query)
                offset, limit = int(query["offset"][0]), int(query["limit"][0])
                queries.append(query)
                return SimpleNamespace(status=200, body=[{
                    "stationuuid": str(StationUUID(int=i)), "name": f"Fixture {i}",
                    "countrycode": "DE", "url_resolved": "https://media.example/audio", "hls": 0,
                } for i in range(offset, offset + limit)])

        catalog = BrowserProvider().catalog({
            "country": "de", "query": "News", "tag": "public", "language": "deu",
            "cursor": "250", "page_size": 2,
        }, context(Pages()))
        self.assertEqual(len(catalog["stations"]), 2)
        self.assertEqual(catalog["next_cursor"], "252")
        self.assertEqual(queries, [{
            "hidebroken": ["true"], "order": ["votes"], "reverse": ["true"], "limit": ["2"],
            "offset": ["250"], "countrycode": ["DE"], "name": ["News"], "tag": ["public"],
            "language": ["deu"],
        }])
        self.assertEqual(catalog["stations"][-1]["station_ref"]["provider_station_id"], str(StationUUID(int=251)))

    def test_radiobrowser_rejects_invalid_catalog_query(self):
        provider = BrowserProvider()
        for payload in ({"country": "DEU"}, {"cursor": "not-a-number"}, {"page_size": 251}, {"query": 7}):
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidResource):
                    provider.catalog(payload, context(Capabilities([])))

    def test_radiobrowser_rejects_missing_resolved_url_and_transport(self):
        for row in ({"stationuuid": UUID, "url": "https://media.example/unresolved"},
                    {"stationuuid": UUID, "url_resolved": "https://media.example/stream"}):
            with self.subTest(row=row):
                with self.assertRaises(InvalidResource):
                    BrowserProvider().resolve_stream(RadioReference("radiobrowser", UUID), context(Capabilities([row])))

    def test_radiobrowser_failure_is_not_a_successful_empty_catalog(self):
        for body, status in (({}, 200), ([], 503), ([{"error": "unavailable"}], 200)):
            with self.subTest(status=status):
                with self.assertRaises(PluginError):
                    BrowserProvider().catalog({}, context(Capabilities(body, status)))
        self.assertEqual(BrowserProvider().catalog({}, context(Capabilities([]))), {"stations": [], "next_cursor": None})


class RadioCatalogArtifactTest(unittest.IsolatedAsyncioTestCase):
    async def test_static_archive_contains_catalog_resource_and_resolves(self):
        from waveflow_plugin_cli import build_project, test_build as run_build_test

        with tempfile.TemporaryDirectory() as directory:
            built = build_project(ROOT / "bundled_plugins/hk_sg_radio", output=Path(directory) / "provider.pyz")
            result = await run_build_test(Path(built["manifest"]), method="radio.catalog", payload={})
            self.assertEqual(len(result["result"]["stations"]), 34)
            station = result["result"]["stations"][0]
            resolved = await run_build_test(Path(built["manifest"]), method="radio.resolve_stream", payload={
                "station_ref": station["station_ref"], "playback_config": station["playback_config"],
            })
            self.assertEqual(resolved["result"]["transport"], "hls")
            self.assertTrue(resolved["health"]["healthy"])

    async def test_radiobrowser_archive_uses_managed_http_and_uuid_resolution(self):
        from waveflow_plugin_cli import build_project, test_build as run_build_test

        row = {"stationuuid": UUID, "name": "Fixture", "countrycode": "HK", "hls": 0,
               "url_resolved": "https://media.example/audio"}
        capabilities = {"core.http.fetch": {"status": 200, "headers": {}, "body": [row], "response_mode": "json"}}
        with tempfile.TemporaryDirectory() as directory:
            built = build_project(ROOT / "bundled_plugins/radiobrowser", output=Path(directory) / "provider.pyz")
            result = await run_build_test(Path(built["manifest"]), method="radio.catalog", payload={}, capabilities=capabilities)
            self.assertEqual(len(result["result"]["stations"]), 1)
            resolved = await run_build_test(Path(built["manifest"]), method="radio.resolve_stream", payload={
                "station_ref": result["result"]["stations"][0]["station_ref"],
            }, capabilities=capabilities)
            self.assertEqual(resolved["result"]["transport"], "audio_http")


if __name__ == "__main__":
    unittest.main()
