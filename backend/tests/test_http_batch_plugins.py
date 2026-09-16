from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from plugin_runtime import load_manifest
from plugin_runtime.process import PluginProcess
from plugin_python_runtime import PythonEnvironmentManager
from waveflow_plugin_cli import build_project, validate_project
from waveflow_plugin_sdk import PluginError, ResolveContext, TVReference


ROOT = Path(__file__).parents[1]
PROVIDERS = ("migu", "hbtv", "sdly", "qukan")
IDENTITIES = {name: f"org.waveflow/{name}" for name in PROVIDERS}
STREAMS = {
    "migu": "https://media.example/migu/live.m3u8?puData=abcdef",
    "hbtv": "https://media.example/hbtv/live.m3u8",
    "sdly": "https://media.example/sdly/live.m3u8",
    "qukan": "https://media.example/qukan/live.m3u8",
}


def _load(name: str):
    source = ROOT / "bundled_plugins" / name / "plugin.py"
    spec = importlib.util.spec_from_file_location(f"{name}_batch_fixture", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeCapabilities:
    def __init__(self, body, *, status: int = 200):
        self.body = body
        self.status = status
        self.calls: list[dict] = []

    def managed_http(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.body, BaseException):
            raise self.body
        return SimpleNamespace(status=self.status, headers={}, body=self.body, response_mode="json")


def _context(caps) -> ResolveContext:
    return ResolveContext("batch-fixture", 9_999_999_999_999, {}, caps)


class HttpBatchDeterministicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = {name: _load(name) for name in PROVIDERS}

    def test_migu_signature_rate_and_ddcalcu_semantics(self):
        module = self.modules["migu"]
        caps = FakeCapabilities({
            "body": {
                "urlInfo": {"url": STREAMS["migu"]},
                "content": {"contId": "123456789"},
            }
        })
        with mock.patch.object(module.time, "time", return_value=1_700_000_000), \
             mock.patch.object(module.random, "randint", return_value=123456), \
             mock.patch.object(module, "MIGU_CLIENT_ID", "fixture-client"):
            descriptor = module.Provider().resolve_stream(
                TVReference("migu", "/123456789/", {"rate": ["2"]}), _context(caps)
            )
        call = caps.calls[0]
        self.assertEqual((call["url"], call["timeout"], call["response_mode"]), (
            module.MIGU_PLAYURL_ENDPOINT, 8, "json",
        ))
        self.assertEqual(call["query"]["contId"], "123456789")
        self.assertEqual(call["query"]["rateType"], "2")
        self.assertEqual(call["query"]["timestamp"], "1700000000000")
        self.assertEqual(call["query"]["salt"], "12345625")
        self.assertEqual(call["headers"]["ClientId"], "fixture-client")
        self.assertIn("ddCalcu=", descriptor.url)
        self.assertTrue(descriptor.url.endswith("&sv=10004&ct=android"))
        self.assertEqual(
            (descriptor.transport, descriptor.ttl_seconds, descriptor.volatile_url,
             descriptor.requires_proxy, descriptor.direct_playable),
            ("hls", 1800, False, False, True),
        )

    def test_migu_no_appcode_region_malformed_and_reference_errors(self):
        module = self.modules["migu"]
        caps = FakeCapabilities({"body": {"urlInfo": {"url": STREAMS["migu"]}}})
        with mock.patch.object(module.time, "time", return_value=1_700_000_000), \
             mock.patch.object(module.random, "randint", return_value=1):
            module.Provider().resolve_stream(TVReference("migu", "641886683"), _context(caps))
        self.assertNotIn("appCode", caps.calls[0]["headers"])
        for body, expected in (
            ({"region": "restricted"}, "migu_region_restricted"),
            ({"body": {}}, "migu_no_play_url"),
            ("not-json", "migu_upstream_failed"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(PluginError) as raised:
                    module.Provider().resolve_stream(TVReference("migu", "123456789"), _context(FakeCapabilities(body)))
                self.assertEqual(raised.exception.details["provider_code"], expected)
        with self.assertRaises(PluginError) as invalid:
            module.Provider().resolve_stream(TVReference("migu", "bad"), _context(caps))
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")
        with self.assertRaises(PluginError) as quality:
            module.Provider().resolve_stream(
                TVReference("migu", "123456789", {"rate": ["1"]}), _context(caps)
            )
        self.assertEqual(quality.exception.details["provider_code"], "unsupported_quality")

    def test_hbtv_channels_numeric_mapping_signature_and_failures(self):
        module = self.modules["hbtv"]
        body = {"returnData": {"news": [{
            "id": 10524916, "title": "河北卫视",
            "liveVideo": [{"formats": [{"url": "https://media.example/hb/base.m3u8"}]}],
            "appCustomParams": {"movie": {"liveUri": "uri", "liveKey": "key"}},
        }]}}
        caps = FakeCapabilities(body)
        with mock.patch.object(module.time, "time", return_value=1_700_000_000):
            descriptor = module.Provider().resolve_stream(TVReference("hbtv", "hbws"), _context(caps))
        self.assertEqual(caps.calls[0]["query"], {"catalogId": "32557", "siteId": "1"})
        self.assertEqual(descriptor.url.split("?")[0], "https://media.example/hb/base.m3u8")
        self.assertEqual(descriptor.ttl_seconds, 3600)
        self.assertTrue(descriptor.volatile_url)
        caps = FakeCapabilities(body)
        module.Provider().resolve_stream(TVReference("hbtv", "10524916"), _context(caps))
        self.assertEqual(caps.calls[0]["url"], module.HBTv_LIST_URL)
        for body, expected in (({}, "hbtv_parse_failed"), ({"returnData": {"news": []}}, "hbtv_channel_not_found")):
            with self.assertRaises(PluginError) as raised:
                module.Provider().resolve_stream(TVReference("hbtv", "hbws"), _context(FakeCapabilities(body)))
            self.assertEqual(raised.exception.details["provider_code"], expected)
        with self.assertRaises(PluginError) as invalid:
            module.Provider().resolve_stream(TVReference("hbtv", "unknown"), _context(caps))
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")

    def test_sdly_full_mapping_normalization_and_multiple_region_fixtures(self):
        module = self.modules["sdly"]
        self.assertGreaterEqual(len(module.SDLY_CHANNELS), 200)
        cases = (("jncqxw", 171, 2), ("dygg", 537, 3), ("whhy", 157, 12))
        for resource, orgid, channel_id in cases:
            with self.subTest(resource=resource):
                caps = FakeCapabilities({"data": [{"id": channel_id, "stream": STREAMS["sdly"]}]})
                descriptor = module.Provider().resolve_stream(TVReference("sdly", f"/{resource}/"), _context(caps))
                self.assertEqual(descriptor.url, STREAMS["sdly"])
                self.assertEqual(caps.calls[0]["query"], {"_orgid_": str(orgid)})
        caps = FakeCapabilities({"data": [{"id": 20, "stream": STREAMS["sdly"]}]})
        module.Provider().resolve_stream(TVReference("sdly", "171:20"), _context(caps))
        self.assertEqual(caps.calls[0]["query"], {"_orgid_": "171"})
        for body, expected in (({"data": []}, "sdly_no_stream"), ("bad", "sdly_request_failed")):
            with self.assertRaises(PluginError) as raised:
                module.Provider().resolve_stream(TVReference("sdly", "jncqxw"), _context(FakeCapabilities(body)))
            self.assertEqual(raised.exception.details["provider_code"], expected)

    def test_qukan_form_body_headers_and_error_taxonomy(self):
        module = self.modules["qukan"]
        caps = FakeCapabilities({"code": 0, "value": {"url": STREAMS["qukan"]}})
        descriptor = module.Provider().resolve_stream(TVReference("qukan", "/jimei/"), _context(caps))
        call = caps.calls[0]
        self.assertEqual((call["method"], call["response_mode"]), ("POST", "json"))
        self.assertIn("liveId=1778569138673121", call["text_body"])
        self.assertIn("sign=18d34b7bfe26df9912192850e3eb36c3", call["text_body"])
        self.assertIn("/1778569138673121", call["headers"]["Referer"])
        self.assertEqual((descriptor.transport, descriptor.ttl_seconds, descriptor.volatile_url), ("hls", 1800, True))
        for body, expected in (({"code": 1}, "qukan_business_error"),
                               ({"code": 0, "value": {}}, "qukan_no_stream"),
                               ("bad", "qukan_parse_failed")):
            with self.assertRaises(PluginError) as raised:
                module.Provider().resolve_stream(TVReference("qukan", "jimei"), _context(FakeCapabilities(body)))
            self.assertEqual(raised.exception.details["provider_code"], expected)
        with self.assertRaises(PluginError) as invalid:
            module.Provider().resolve_stream(TVReference("qukan", "unknown"), _context(caps))
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")

    def test_manifests_and_source_independence(self):
        for name in PROVIDERS:
            project = ROOT / "bundled_plugins" / name
            manifest = load_manifest(project / "manifest.json")
            self.assertEqual(manifest.identity, IDENTITIES[name])
            self.assertEqual(manifest.permissions["network"]["managed"], True)
            self.assertEqual(manifest.runtime["dependency_lock"]["artifacts"], [])
            source = (project / "plugin.py").read_text()
            for forbidden in ("backend.adapters", "adapters.", "import httpx", "import requests", "FastAPI"):
                self.assertNotIn(forbidden, source)


class HttpBatchRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_four_plugins_run_in_clean_python_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in PROVIDERS:
                project = ROOT / "bundled_plugins" / name
                manifest = load_manifest(project / "manifest.json")
                self.assertEqual(validate_project(project)["plugin"], IDENTITIES[name])
                isolated_project = root / name
                isolated_project.mkdir()
                shutil.copyfile(project / "plugin.py", isolated_project / "plugin.py")
                shutil.copyfile(project / "manifest.json", isolated_project / "manifest.json")
                artifact = build_project(isolated_project, output=root / f"{name}.pyz")
                manager = PythonEnvironmentManager(root / "plugin-store" / name)
                environment = await manager.prepare(manifest, {})
                process = PluginProcess(
                    (str(environment.python), "-I", str(artifact["artifact"]),
                     "--identity", manifest.identity, "--version", manifest.version),
                    f"{name}-batch-runtime", environment={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
                )
                await process.start()
                try:
                    hello = await process.call("runtime.hello", {})
                    process.negotiate_protocol(hello["protocol_version"])
                    health = await process.call("runtime.health", {})
                    self.assertEqual(hello["plugin"], IDENTITIES[name])
                    self.assertEqual(hello["owned_schemes"], [{"scheme": name, "contract": "tv_provider"}])
                    self.assertTrue(health["healthy"])
                finally:
                    await process.call("runtime.shutdown", {})
                    await process.stop()


if __name__ == "__main__":
    unittest.main()
