from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plugin_runtime import load_manifest
from plugin_runtime.process import PluginProcess
from plugin_python_runtime import PythonEnvironmentManager
from waveflow_plugin_cli import build_project
from waveflow_plugin_sdk import PluginError, ResolveContext, TVReference


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "bundled_plugins" / "huya" / "plugin.py"
PROJECT = SOURCE.parent
DEPENDENCIES = ROOT / "official_plugins" / "dependency_artifacts" / "youtube"
PLAY_URLS = {
    "tx": "https://media.example/huya-tx.flv",
    "hs": "https://media.example/huya-hs.m3u8",
    "al": "https://media.example/huya-al.ts",
}


def _load_module():
    spec = importlib.util.spec_from_file_location("huya_plugin_fixture", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeStream:
    def __init__(self, url: str):
        self.url = url

    def to_url(self):
        return self.url


class FakeStreamlink:
    streams_result: dict[str, FakeStream] = {}
    failure: BaseException | None = None
    calls: list[str] = []

    def __init__(self):
        type(self).calls = []

    def streams(self, url: str):
        type(self).calls.append(url)
        if self.failure:
            raise self.failure
        return self.streams_result


class HuyaPluginContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def setUp(self):
        FakeStreamlink.failure = None
        FakeStreamlink.streams_result = {
            "tx_source": FakeStream(PLAY_URLS["tx"]),
            "hs_source": FakeStream(PLAY_URLS["hs"]),
            "al_source": FakeStream(PLAY_URLS["al"]),
            "best": FakeStream("https://media.example/huya-best.flv"),
        }

    def _resolve(self, resource="31421", query=None):
        with mock.patch.object(self.module, "Streamlink", FakeStreamlink):
            return self.module.Provider().resolve_stream(
                TVReference("huya", resource, query or {}),
                ResolveContext("huya-fixture", 9_999_999_999_999, {}, None),
            )

    def test_reference_and_cdn_selection_match_legacy_policy(self):
        default = self._resolve("/31421/")
        self.assertEqual((default.url, default.transport), (PLAY_URLS["tx"], "http_flv"))
        self.assertEqual(FakeStreamlink.calls, ["https://www.huya.com/31421"])

        selected = self._resolve("31421", {"cdn": ["hs"]})
        self.assertEqual((selected.url, selected.transport), (PLAY_URLS["hs"], "hls"))
        selected = self._resolve("31421", {"cdn": ["al"]})
        self.assertEqual((selected.url, selected.transport), (PLAY_URLS["al"], "mpegts"))

        # Unknown CDN falls through to the same fixed legacy preference list.
        fallback = self._resolve("31421", {"cdn": ["missing"]})
        self.assertEqual(fallback.url, PLAY_URLS["tx"])

    def test_selection_priority_and_descriptor_fields_are_deterministic(self):
        FakeStreamlink.streams_result = {
            "hs_source": FakeStream("https://media.example/hs.flv"),
            "tx_source": FakeStream("https://media.example/tx.flv"),
            "best": FakeStream("https://media.example/best.flv"),
        }
        descriptor = self._resolve("31421", {"cdn": ["hs"]})
        contract = descriptor.as_contract()
        self.assertEqual(
            (descriptor.url, descriptor.transport, descriptor.headers,
             descriptor.ttl_seconds, descriptor.volatile_url,
             descriptor.requires_proxy, descriptor.direct_playable),
            ("https://media.example/hs.flv", "http_flv",
             {"Origin": "https://www.huya.com", "Referer": "https://www.huya.com/"},
             60, False, False, True),
        )
        self.assertEqual(contract["provider_diagnostics"]["stream_name"], "hs_source")
        self.assertEqual(contract["provider_diagnostics"]["available_streams"], ["hs_source", "tx_source", "best"])
        self.assertEqual(contract["provider_diagnostics"]["preferred_cdn"], "hs")

    def test_unavailable_stream_and_streamlink_failures_keep_taxonomy(self):
        FakeStreamlink.streams_result = {}
        with self.assertRaises(PluginError) as not_live:
            self._resolve()
        self.assertEqual(
            (not_live.exception.code, not_live.exception.retryable,
             not_live.exception.details["provider_code"]),
            ("NOT_LIVE", False, "huya_not_live"),
        )

        for failure in (
            self.module.NoPluginError("missing plugin"),
            self.module.StreamError("bad stream"),
            RuntimeError("upstream"),
        ):
            FakeStreamlink.failure = failure
            with self.subTest(failure=type(failure).__name__), self.assertRaises(PluginError) as upstream:
                self._resolve()
            self.assertEqual((upstream.exception.code, upstream.exception.retryable),
                             ("TEMPORARY_UPSTREAM_FAILURE", True))

    def test_invalid_reference_and_source_purity(self):
        with self.assertRaises(PluginError) as invalid:
            self._resolve("")
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")
        source = SOURCE.read_text(encoding="utf-8")
        for forbidden in ("adapters.huya", "backend.adapters.huya", "import httpx", "chromium", "playwright", "selenium"):
            self.assertNotIn(forbidden, source)
        self.assertIn("asyncio.to_thread", source)

    def test_descriptor_matches_legacy_golden_fixture(self):
        legacy = importlib.import_module("adapters.huya")
        from adapters import AdapterRequest

        class LegacyFake(FakeStreamlink):
            streams_result = {"tx_source": FakeStream(PLAY_URLS["tx"]), "hs_source": FakeStream(PLAY_URLS["hs"])}

        with mock.patch.object(legacy, "Streamlink", LegacyFake):
            legacy_result = asyncio.run(legacy.resolve_huya(
                AdapterRequest("huya://31421?cdn=hs", "huya", "31421", {"cdn": ["hs"]}), None,
            ))
        with mock.patch.object(self.module, "Streamlink", LegacyFake):
            descriptor = self.module.Provider().resolve_stream(
                TVReference("huya", "31421", {"cdn": ["hs"]}),
                ResolveContext("huya-fixture", 9_999_999_999_999, {}, None),
            )
        self.assertEqual(
            {"url": descriptor.url, "source_type": descriptor.transport,
             "headers": descriptor.headers, "ttl": descriptor.ttl_seconds,
             "expires_at": descriptor.expires_at, "direct_playable": descriptor.direct_playable,
             "requires_proxy": descriptor.requires_proxy, "volatile_url": descriptor.volatile_url},
            {"url": legacy_result["url"], "source_type": legacy_result["source_type"],
             "headers": legacy_result["headers"], "ttl": legacy_result["ttl"],
             "expires_at": legacy_result["expires_at"], "direct_playable": legacy_result["direct_playable"],
             "requires_proxy": legacy_result["requires_proxy"], "volatile_url": False},
        )

    def test_streamlink_call_boundary_is_explicitly_worker_isolated(self):
        with mock.patch.object(self.module, "_resolve_huya_with_streamlink",
                               return_value={"url": PLAY_URLS["tx"], "transport": "http_flv",
                                             "stream_name": "tx_source", "available_streams": ["tx_source"]}) as worker:
            self._resolve("31421")
        worker.assert_called_once_with("https://www.huya.com/31421", "tx")


class HuyaIsolatedRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_clean_isolated_environment_imports_streamlink_and_plugin_health(self):
        manifest = load_manifest(PROJECT / "manifest.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            for filename in ("plugin.py", "manifest.json", "dependency-lock.json"):
                shutil.copyfile(PROJECT / filename, project / filename)
            build = build_project(project, output=root / "huya.pyz")
            references = {
                item["sha256"]: DEPENDENCIES / item["filename"]
                for item in manifest.runtime["dependency_lock"]["artifacts"]
            }
            manager = PythonEnvironmentManager(root / "plugin-store")
            environment = await manager.prepare(manifest, references)
            probe = await asyncio.create_subprocess_exec(
                str(environment.python), "-I", "-c",
                "import shutil, streamlink, sys; print(streamlink.__version__); print(sys.prefix != sys.base_prefix); print(shutil.which('node') is None)",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
            )
            stdout, stderr = await probe.communicate()
            self.assertEqual(probe.returncode, 0, stderr.decode())
            self.assertEqual(stdout.decode().splitlines(), ["8.4.0", "True", "True"])
            process = PluginProcess(
                (str(environment.python), "-I", str(build["artifact"]), "--identity", manifest.identity,
                 "--version", manifest.version),
                "huya-runtime-fixture", environment={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
            await process.start()
            try:
                hello = await process.call("runtime.hello", {})
                process.negotiate_protocol(hello["protocol_version"])
                health = await process.call("runtime.health", {})
                self.assertEqual(hello["plugin"], manifest.identity)
                self.assertEqual(hello["owned_schemes"], [{"scheme": "huya", "contract": "tv_provider"}])
                self.assertTrue(health["healthy"])
            finally:
                await process.call("runtime.shutdown", {})
                await process.stop()


if __name__ == "__main__":
    unittest.main()
