from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from plugin_runtime import load_manifest
from plugin_runtime.process import PluginProcess
from plugin_python_runtime import PythonEnvironmentManager
from waveflow_plugin_cli import build_project, validate_project
from waveflow_plugin_sdk import PluginError, ResolveContext, TVReference


ROOT = Path(__file__).parents[1]
DEPENDENCIES = ROOT / "tests" / "fixtures" / "dependencies"
PROVIDERS = ("xjtv", "woniu")


def _load(name: str):
    source = ROOT / "bundled_plugins" / name / "plugin.py"
    spec = importlib.util.spec_from_file_location(f"{name}_batch_fixture", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeCapabilities:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def managed_http(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(status=200, headers={}, body=response, response_mode=kwargs.get("response_mode", "json"))


def _context(capabilities) -> ResolveContext:
    return ResolveContext("xjtv-woniu-fixture", 9_999_999_999_999, {}, capabilities)


class XJTVDeterministicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load("xjtv")
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    def _responses(self, *, channel_body=None):
        return [
            {"success": True, "data": {"token": "fixture-token", "publicKey": self.public_key}},
            {"success": True, "data": "1700000123"},
            channel_body or {"success": True, "data": [{
                "Id": "1", "SimpleName": "xjtv-1", "ChineseName": "新疆卫视",
                "PlayStreamUrl": "https://media.example/xjtv/live.m3u8?auth_key=1700005000-fixture",
            }]},
        ]

    def test_rsa_signature_channel_mapping_and_descriptor(self):
        caps = FakeCapabilities(self._responses())
        with mock.patch.object(self.module.time, "time", return_value=1_700_000_000):
            descriptor = self.module.Provider().resolve_stream(
                TVReference("xjtv", "/xjtv1/"), _context(caps)
            )
        self.assertEqual([call["url"] for call in caps.calls], [
            f"{self.module.XJTV_API_BASE}{self.module.XJTV_GEN_TOKEN_PATH}",
            f"{self.module.XJTV_API_BASE}{self.module.XJTV_TIMESTAMP_PATH}",
            f"{self.module.XJTV_API_BASE}{self.module.XJTV_CHANNEL_LIST_PATH}",
        ])
        signed = caps.calls[2]["query"]["sign"]
        decrypted = self.private_key.decrypt(
            base64.b64decode(signed[32:]), padding.PKCS1v15(),
        ).decode()
        self.assertEqual(decrypted, "fixture-token")
        self.assertEqual(signed[:32], self.module.hashlib.md5(
            b"fixture-token1700000123api/TVLiveV100/TVChannelList"
        ).hexdigest())
        self.assertEqual(
            (descriptor.transport, descriptor.ttl_seconds, descriptor.expires_at,
             descriptor.volatile_url, descriptor.requires_proxy),
            ("hls", 4700, 1700005000, True, False),
        )
        self.assertEqual(descriptor.provider_diagnostics["channel_id"], "1")

    def test_multiple_aliases_and_error_taxonomy(self):
        self.assertEqual(len(self.module.XJTV_CHANNELS), 7)
        for resource in ("3", "xjtv-2", "xjtv2"):
            caps = FakeCapabilities(self._responses(channel_body={"success": True, "data": [{
                "id": "3", "simpleName": "xjtv-2", "playStreamUrl": "https://media.example/xjtv/3.m3u8",
            }]}))
            descriptor = self.module.Provider().resolve_stream(TVReference("xjtv", resource), _context(caps))
            self.assertEqual(descriptor.url, "https://media.example/xjtv/3.m3u8")
        for body, expected in (
            ({"success": True, "data": "bad"}, "xjtv_channel_list_parse_failed"),
            ({"success": True, "data": [{"Id": "1", "IsForbidden": True}]}, "xjtv_channel_forbidden"),
            ({"success": True, "data": [{"Id": "1"}]}, "xjtv_no_play_url"),
        ):
            with self.subTest(expected=expected), self.assertRaises(PluginError) as raised:
                self.module.Provider().resolve_stream(
                    TVReference("xjtv", "1"), _context(FakeCapabilities(self._responses(channel_body=body)))
                )
            self.assertEqual(raised.exception.details["provider_code"], expected)
        with self.assertRaises(PluginError) as denied:
            self.module.Provider().resolve_stream(TVReference("xjtv", "1"), _context(FakeCapabilities([
                PluginError("AUTH_FAILED", "fixture 403", category="auth"),
            ])))
        self.assertEqual(denied.exception.code, "AUTH_FAILED")

    def test_xjtv_source_is_independent(self):
        source = (ROOT / "bundled_plugins" / "xjtv" / "plugin.py").read_text()
        for forbidden in ("backend.adapters", "adapters.xjtv", "import httpx", "subprocess"):
            self.assertNotIn(forbidden, source)


class WoniuDeterministicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load("woniu")

    def test_mapping_auth_query_signature_and_descriptor(self):
        self.assertEqual(len(self.module.WONIU_CHANNELS), 84)
        auth = {"Cookie": "fixture-cookie", "Authorization": "fixture-auth", "X-Device-Id": "fixture-device",
                "Open-token": "fixture-open", "userId": "fixture-user", "userMac": "fixture-mac"}
        url = "https://media.example/woniu/live.m3u8?timestamp=1700003600"
        with mock.patch.object(self.module, "WONIU_AUTH_HEADERS", auth), \
             mock.patch.object(self.module, "WONIU_ATTEMPTS", 1), \
             mock.patch.object(self.module.time, "time", return_value=1_700_000_000), \
             mock.patch.object(self.module, "_fetch_play_urls", return_value={"ret_data": [{"live_url": url}]}):
            descriptor = self.module.Provider().resolve_stream(TVReference("woniu", "/cctv1/"), _context(None))
            self.assertEqual((descriptor.transport, descriptor.url, descriptor.ttl_seconds,
                          descriptor.expires_at, descriptor.volatile_url, descriptor.requires_proxy),
                         ("hls", url, 3000, 1700003600, True, False))
            self.assertEqual(self.module._build_headers()["Authorization"], "fixture-auth")
        params = self.module._build_query_params("5104", now=1700000000)
        self.assertEqual(dict(params)["channel"], "5104")
        signed_path = self.module._build_signed_path(self.module.WONIU_PATH_GET_PLAY_URLS, params)
        self.assertRegex(signed_path, r"[?&]vf=[0-9a-f]{32}$")

    def test_retry_placeholder_and_unavailable_taxonomy(self):
        placeholder = {"ret_data": [{"live_url": "?project=WNTV"}]}
        valid = {"ret_data": [{"live_url": "https://media.example/woniu/retry.m3u8"}]}
        with mock.patch.object(self.module, "WONIU_ATTEMPTS", 2), \
             mock.patch.object(self.module, "WONIU_RETRY_SLEEP_SECONDS", 0), \
             mock.patch.object(self.module, "_fetch_play_urls", side_effect=[placeholder, valid]):
            descriptor = self.module.Provider().resolve_stream(TVReference("woniu", "5216"), _context(None))
        self.assertEqual(descriptor.url, valid["ret_data"][0]["live_url"])
        with mock.patch.object(self.module, "WONIU_ATTEMPTS", 1), \
             mock.patch.object(self.module, "_fetch_play_urls", side_effect=TimeoutError("fixture unavailable")):
            with self.assertRaises(PluginError) as unavailable:
                self.module.Provider().resolve_stream(TVReference("woniu", "cctv1"), _context(None))
        self.assertEqual(unavailable.exception.details["provider_code"], "woniu_request_failed")
        with mock.patch.object(self.module, "WONIU_ATTEMPTS", 1), \
             mock.patch.object(self.module, "_fetch_play_urls", return_value={"ret_data": "bad"}):
            with self.assertRaises(PluginError) as malformed:
                self.module.Provider().resolve_stream(TVReference("woniu", "cctv1"), _context(None))
        self.assertEqual(malformed.exception.details["provider_code"], "woniu_no_stream")
        with self.assertRaises(PluginError) as invalid:
            self.module.Provider().resolve_stream(TVReference("woniu", "unknown"), _context(None))
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")

    def test_direct_boundary_and_source_is_independent(self):
        source = (ROOT / "bundled_plugins" / "woniu" / "plugin.py").read_text()
        for forbidden in ("backend.adapters", "adapters.woniu", "import httpx", "subprocess", "openssl"):
            self.assertNotIn(forbidden, source)


class XJTVWoniuRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_clean_python_runtime_health_and_dependency_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in PROVIDERS:
                project = ROOT / "bundled_plugins" / name
                manifest = load_manifest(project / "manifest.json")
                self.assertEqual(validate_project(project)["plugin"], f"org.waveflow/{name}")
                copied = root / name
                copied.mkdir()
                shutil.copyfile(project / "plugin.py", copied / "plugin.py")
                shutil.copyfile(project / "manifest.json", copied / "manifest.json")
                artifact = build_project(copied, output=root / f"{name}.pyz")
                references = {}
                for item in manifest.runtime["dependency_lock"]["artifacts"]:
                    candidate = DEPENDENCIES / item["filename"]
                    if candidate.is_file():
                        references[item["sha256"]] = candidate
                manager = PythonEnvironmentManager(root / "plugin-store" / name)
                environment = await manager.prepare(manifest, references)
                process = PluginProcess(
                    (str(environment.python), "-I", str(artifact["artifact"]),
                     "--identity", manifest.identity, "--version", manifest.version),
                    f"{name}-runtime-fixture", environment={"PATH": "/nonexistent", "LANG": "C"},
                )
                await process.start()
                try:
                    hello = await process.call("runtime.hello", {})
                    process.negotiate_protocol(hello["protocol_version"])
                    health = await process.call("runtime.health", {})
                    self.assertEqual(hello["plugin"], manifest.identity)
                    self.assertTrue(health["healthy"])
                finally:
                    await process.call("runtime.shutdown", {})
                    await process.stop()


if __name__ == "__main__":
    unittest.main()
