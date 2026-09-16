from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plugin_runtime import load_manifest
from plugin_runtime.process import PluginProcess
from plugin_python_runtime import PythonEnvironmentManager
from waveflow_plugin_cli import build_project, validate_project
from waveflow_plugin_sdk import PluginError, ResolveContext, TVReference


ROOT = Path(__file__).parents[1]
PROJECT = ROOT / "bundled_plugins" / "kuaishou"
SOURCE = PROJECT / "plugin.py"
PTBTV_DEPENDENCIES = ROOT / "tests" / "fixtures" / "dependencies" / "ptbtv"
PTBTV_LOCK = json.loads((PTBTV_DEPENDENCIES / "dependency-lock.json").read_text())
IDENTITY = "org.waveflow/kuaishou"
ROOM = "fixture-room"
STREAM_H264 = "https://live.example/kuaishou/h264.flv"
STREAM_FALLBACK = "https://live.example/kuaishou/fallback.flv"


def _load_module():
    spec = importlib.util.spec_from_file_location("kuaishou_plugin_fixture", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _page(*, live_stream: object, author: object | None = None, game_info: object | None = None) -> str:
    payload = {"liveStream": live_stream, "author": author or {"name": "Fixture Anchor"},
               "gameInfo": game_info or {}}
    return "<script>window.__INITIAL_STATE__=" + json.dumps(payload, separators=(",", ":")) + ";(function(){var s;"


class _Response:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _Session:
    response = _Response(200, _page(live_stream={}))
    init_kwargs: dict[str, object] = {}
    calls: list[tuple[str, dict[str, object]]] = []

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, **kwargs):
        type(self).calls.append((url, kwargs))
        return type(self).response


class KuaishouPluginContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def _context(self):
        return ResolveContext("kuaishou-fixture", 9_999_999_999_999, {}, None)

    def _resolve(self, page: str):
        with mock.patch.object(self.module, "_resolve_direct", return_value=(200, page)):
            return self.module.Provider().resolve_stream(TVReference("kuaishou", ROOM), self._context())

    def test_h264_priority_and_descriptor_semantics(self):
        page = _page(
            live_stream={
                "playUrls": {
                    "h264": {"adaptationSet": {"representation": [{"url": STREAM_H264}]}},
                    "other": {"adaptationSet": {"representation": [{"url": "https://live.example/other.flv"}]}},
                }
            }
        )
        descriptor = self._resolve(page)
        self.assertEqual(
            (descriptor.transport, descriptor.url, descriptor.ttl_seconds,
             descriptor.volatile_url, descriptor.requires_proxy, descriptor.direct_playable),
            ("http_flv", STREAM_H264, 1800, False, False, True),
        )
        self.assertEqual(descriptor.provider_diagnostics["anchor_name"], "Fixture Anchor")

    def test_list_play_urls_use_first_representation_when_h264_is_absent(self):
        descriptor = self._resolve(_page(live_stream={
            "playUrls": [
                {"adaptationSet": {"representation": [{"url": STREAM_FALLBACK}]}},
                {"adaptationSet": {"representation": [{"url": "https://live.example/second.flv"}]}},
            ]
        }))
        self.assertEqual(descriptor.url, STREAM_FALLBACK)

    def test_not_live_is_non_retryable_and_has_legacy_provider_code(self):
        with self.assertRaises(PluginError) as raised:
            self._resolve(_page(live_stream={}))
        self.assertEqual((raised.exception.code, raised.exception.retryable,
                          raised.exception.details["provider_code"]),
                         ("NOT_LIVE", False, "kuaishou_not_live"))

    def test_missing_marker_and_malformed_playlist_preserve_taxonomy(self):
        with mock.patch.object(self.module, "_resolve_direct", return_value=(200, "<html>no marker</html>")):
            with self.assertRaises(PluginError) as missing:
                self.module.Provider().resolve_stream(TVReference("kuaishou", ROOM), self._context())
        self.assertEqual(missing.exception.details["provider_code"], "kuaishou_parse_failed")

        malformed = "<script>window.__INITIAL_STATE__={\"liveStream\":not-json,\"gameInfo\":{};" \
            "(function(){var s;"
        with mock.patch.object(self.module, "_resolve_direct", return_value=(200, malformed)):
            with self.assertRaises(PluginError) as invalid:
                self.module.Provider().resolve_stream(TVReference("kuaishou", ROOM), self._context())
        self.assertEqual(invalid.exception.details["provider_code"], "kuaishou_resolve_failed")

    def test_http_and_transport_failures_are_temporary_upstream(self):
        with mock.patch.object(self.module, "_resolve_direct", return_value=(403, "denied")):
            with self.assertRaises(PluginError) as http_error:
                self.module.Provider().resolve_stream(TVReference("kuaishou", ROOM), self._context())
        self.assertEqual((http_error.exception.details["provider_code"], http_error.exception.details["status"]),
                         ("kuaishou_http_error", 403))

        with mock.patch.object(self.module, "_resolve_direct", side_effect=TimeoutError("fixture timeout")):
            with self.assertRaises(PluginError) as failed:
                self.module.Provider().resolve_stream(TVReference("kuaishou", ROOM), self._context())
        self.assertEqual(failed.exception.details["provider_code"], "kuaishou_resolve_failed")

    def test_direct_fetch_uses_legacy_curl_profile_without_core_headers(self):
        _Session.calls = []
        _Session.response = _Response(200, "fixture")
        result = asyncio.run(self.module.direct_fetch("https://live.kuaishou.com/u/room", session_factory=_Session))
        self.assertEqual(result, (200, "fixture"))
        self.assertEqual(_Session.init_kwargs, {"impersonate": "chrome"})
        self.assertEqual(_Session.calls, [("https://live.kuaishou.com/u/room", {})])

    def test_empty_resource_and_missing_play_url_are_rejected(self):
        with self.assertRaises(PluginError) as empty:
            self.module.Provider().resolve_stream(TVReference("kuaishou", "///"), self._context())
        self.assertEqual(empty.exception.code, "RESOURCE_NOT_FOUND")

        with self.assertRaises(PluginError) as no_url:
            self._resolve(_page(live_stream={"playUrls": []}))
        self.assertEqual(no_url.exception.details["provider_code"], "kuaishou_no_play_url")

    def test_manifest_lock_identity_permissions_and_source_purity(self):
        manifest = load_manifest(PROJECT / "manifest.json")
        self.assertEqual(manifest.identity, IDENTITY)
        self.assertEqual(manifest.permissions["network"]["direct"], True)
        self.assertEqual(manifest.permissions["network"].get("managed", False), False)
        self.assertEqual(manifest.runtime["dependency_lock"], PTBTV_LOCK)
        source = SOURCE.read_text(encoding="utf-8")
        for forbidden in ("adapters.kuaishou", "backend.adapters.kuaishou", "import httpx",
                          "managed_http", "CapabilityGateway"):
            self.assertNotIn(forbidden, source)


class KuaishouIsolatedRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_clean_curl_cffi_environment_and_plugin_health(self):
        manifest = load_manifest(PROJECT / "manifest.json")
        self.assertEqual(validate_project(PROJECT)["plugin"], IDENTITY)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            shutil.copyfile(SOURCE, project / "plugin.py")
            shutil.copyfile(PROJECT / "manifest.json", project / "manifest.json")
            build = build_project(project, output=root / "kuaishou.pyz")
            references = {
                item["sha256"]: PTBTV_DEPENDENCIES / item["filename"]
                for item in PTBTV_LOCK["artifacts"]
            }
            manager = PythonEnvironmentManager(root / "plugin-store")
            environment = await manager.prepare(manifest, references)
            probe = await asyncio.create_subprocess_exec(
                str(environment.python), "-I", "-c",
                "import curl_cffi, shutil, sys; print(curl_cffi.__file__); print(sys.prefix != sys.base_prefix); print(shutil.which('node') is None)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
            )
            stdout, stderr = await probe.communicate()
            self.assertEqual(probe.returncode, 0, stderr.decode())
            lines = stdout.decode().splitlines()
            self.assertIn(str(environment.path), lines[0])
            self.assertEqual(lines[1:], ["True", "True"])

            process = PluginProcess(
                (str(environment.python), "-I", str(build["artifact"]),
                 "--identity", manifest.identity, "--version", manifest.version),
                "kuaishou-runtime-fixture", environment={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
            await process.start()
            try:
                hello = await process.call("runtime.hello", {})
                process.negotiate_protocol(hello["protocol_version"])
                health = await process.call("runtime.health", {})
                self.assertEqual(hello["plugin"], IDENTITY)
                self.assertEqual(hello["owned_schemes"], [{"scheme": "kuaishou", "contract": "tv_provider"}])
                self.assertTrue(health["healthy"])
            finally:
                await process.call("runtime.shutdown", {})
                await process.stop()


if __name__ == "__main__":
    unittest.main()
