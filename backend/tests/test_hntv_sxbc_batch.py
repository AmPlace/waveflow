from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from plugin_runtime import load_manifest
from plugin_runtime.process import PluginProcess
from plugin_python_runtime import PythonEnvironmentManager
from waveflow_plugin_cli import build_project, validate_project
from waveflow_plugin_sdk import PluginError, ResolveContext, TVReference


ROOT = Path(__file__).parents[1]
DEPENDENCIES = ROOT / "tests" / "fixtures" / "dependencies"
PROVIDERS = ("hntv", "sxbc")


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


class SequenceCapabilities(FakeCapabilities):
    def __init__(self, values):
        super().__init__(None)
        self.values = list(values)

    def managed_http(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(status=200, headers={}, body=value, response_mode="text")


def _context(capabilities) -> ResolveContext:
    return ResolveContext("hntv-sxbc-fixture", 9_999_999_999_999, {}, capabilities)


def _zero_pad(value: bytes) -> bytes:
    return value + b"\x00" * (-len(value) % 16)


def _encrypt_zero_padded(key: bytes, iv: bytes, value: bytes) -> str:
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(_zero_pad(value)) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode()


def _stream_source(module, tv_payload: dict, radio_payload: dict, *, key: bytes, iv: bytes) -> str:
    tv = key.decode() + _encrypt_zero_padded(key, iv, json.dumps(tv_payload, separators=(",", ":")).encode())
    radio = iv.decode() + _encrypt_zero_padded(key, iv, json.dumps(radio_payload, separators=(",", ":")).encode())
    return f"var sTV='{tv}'; var sRadio=\"{radio}\";"


class HNTVDeterministicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load("hntv")

    def test_channel_map_hls_signature_and_descriptor(self):
        body = [{"id": 145, "name": "河南卫视", "video_streams": ["https://media.example/hntv/live.m3u8"]}]
        caps = FakeCapabilities(body)
        with mock.patch.object(self.module.time, "time", return_value=1_700_000_000):
            descriptor = self.module.Provider().resolve_stream(TVReference("hntv", "/hnws/"), _context(caps))
        expected_sign = self.module.hashlib.sha256(
            (self.module._SIGN_SALT + "1700000000").encode()
        ).hexdigest()
        self.assertEqual(caps.calls[0]["url"], f"{self.module.HNTV_API}/145")
        self.assertEqual(caps.calls[0]["headers"], {"timestamp": "1700000000", "sign": expected_sign})
        self.assertEqual((descriptor.transport, descriptor.url, descriptor.ttl_seconds,
                          descriptor.volatile_url, descriptor.requires_proxy),
                         ("hls", "https://media.example/hntv/live.m3u8", 1800, True, False))

    def test_multiple_channels_and_legacy_rtmp_boundary(self):
        self.assertGreaterEqual(len(self.module.HNTV_CHANNELS), 35)
        for resource, channel_id in (("hnds", 141), ("145", 145)):
            caps = FakeCapabilities([{"id": channel_id, "streams": [f"https://media.example/hntv/{channel_id}.m3u8"]}])
            descriptor = self.module.Provider().resolve_stream(TVReference("hntv", resource), _context(caps))
            self.assertEqual((descriptor.transport, descriptor.url, descriptor.ttl_seconds),
                             ("hls", f"https://media.example/hntv/{channel_id}.m3u8", 1800))
        with self.assertRaises(PluginError) as rtmp:
            self.module.Provider().resolve_stream(
                TVReference("hntv", "hnws"),
                _context(FakeCapabilities([{"id": 145, "streams": ["rtmp://media.example/hntv/live"]}])),
            )
        self.assertEqual(rtmp.exception.details["provider_code"], "hntv_rtmp_unsupported")

    def test_hntv_not_found_malformed_no_stream_and_upstream_errors(self):
        for body, expected in (("not-json", "hntv_parse_failed"), ([], "hntv_channel_not_found"), ({}, "hntv_channel_not_found"),
                               ([{"id": 145}], "hntv_no_stream")):
            with self.subTest(expected=expected), self.assertRaises(PluginError) as raised:
                self.module.Provider().resolve_stream(TVReference("hntv", "hnws"), _context(FakeCapabilities(body)))
            self.assertEqual(raised.exception.details["provider_code"], expected)
        with self.assertRaises(PluginError) as invalid:
            self.module.Provider().resolve_stream(TVReference("hntv", "unknown"), _context(FakeCapabilities([])))
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")


class SXBCDeterministicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load("sxbc")
        cls.key = b"0123456789abcdef"
        cls.iv = b"fedcba9876543210"
        cls.tv_payload = {
            "sxbc": {
                f"tv{i:02d}": {"name": f"TV {i}", "m3u8": f"https://media.example/sxbc/{i}.m3u8"}
                for i in range(1, 12)
            }
        }
        cls.radio_payload = {
            "sxbc": {
                f"radio{i:02d}": {"name": f"Radio {i}", "m3u8": f"https://media.example/radio/{i}.m3u8"}
                for i in range(1, 9)
            }
        }
        cls.source = _stream_source(cls.module, cls.tv_payload, cls.radio_payload, key=cls.key, iv=cls.iv)

    def test_crypto_golden_vector_matches_openssl_zero_padding(self):
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl reference executable is unavailable")
        raw = b'{"sxbc":{"tv01":{"m3u8":"https://media.example/vector.m3u8"}}}'
        plaintext = raw + b"\x00" * (-len(raw) % 16)
        result = subprocess.run(
            [openssl, "enc", "-aes-128-cbc", "-e", "-K", self.key.hex(), "-iv", self.iv.hex(), "-nopad"],
            input=plaintext, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        encoded = base64.b64encode(result.stdout).decode()
        self.assertEqual(self.module._decrypt_zero_padded(self.key, self.iv, encoded), raw)

    def test_tv_mapping_has_eleven_channels_and_radio_boundary_is_explicit(self):
        caps = FakeCapabilities(self.source)
        descriptor = self.module.Provider().resolve_stream(TVReference("sxbc", "tv07"), _context(caps))
        self.assertEqual((descriptor.transport, descriptor.url, descriptor.ttl_seconds,
                          descriptor.headers["Referer"], descriptor.volatile_url),
                         ("hls", "https://media.example/sxbc/7.m3u8", 600,
                          "http://live.snrtv.com/", True))
        with self.assertRaises(PluginError) as radio:
            self.module.Provider().resolve_stream(
                TVReference("sxbc", "radio01", {"type": ["radio"]}), _context(caps)
            )
        self.assertEqual(radio.exception.details["provider_code"], "sxbc_radio_requires_radio_plugin")

    def test_fallback_source_decrypt_failure_malformed_and_blocked_stream(self):
        caps = SequenceCapabilities([
            PluginError("TEMPORARY_UPSTREAM_FAILURE", "first source unavailable", retryable=True),
            self.source,
        ])
        descriptor = self.module.Provider().resolve_stream(TVReference("sxbc", "tv01"), _context(caps))
        self.assertEqual(len(caps.calls), 2)
        self.assertEqual(descriptor.url, "https://media.example/sxbc/1.m3u8")

        bad = "var sTV='0123456789abcdefnot-base64'; var sRadio='fedcba9876543210not-base64';"
        with self.assertRaises(PluginError) as decrypt:
            self.module.Provider().resolve_stream(TVReference("sxbc", "tv01"), _context(FakeCapabilities(bad)))
        self.assertEqual(decrypt.exception.details["provider_code"], "sxbc_decrypt_failed")

        blocked = _stream_source(
            self.module,
            {"sxbc": {"tv01": {"m3u8": "http://alzbl.snrtv.com/live/sxtv.m3u8"}}},
            self.radio_payload, key=self.key, iv=self.iv,
        )
        with self.assertRaises(PluginError) as no_stream:
            self.module.Provider().resolve_stream(TVReference("sxbc", "tv01"), _context(FakeCapabilities(blocked)))
        self.assertEqual(no_stream.exception.details["provider_code"], "sxbc_no_play_url")

    def test_manifest_reuses_sdtv_cryptography_lock_and_source_has_no_openssl(self):
        manifest = load_manifest(ROOT / "bundled_plugins" / "sxbc" / "manifest.json")
        sdtv = load_manifest(ROOT / "bundled_plugins" / "sdtv" / "manifest.json")
        self.assertEqual(manifest.runtime["dependency_lock"], sdtv.runtime["dependency_lock"])
        self.assertEqual(manifest.version, "1.0.1")
        self.assertTrue(manifest.permissions["network"]["managed"])
        self.assertTrue(manifest.permissions["network"]["allow_http"])
        source = (ROOT / "bundled_plugins" / "sxbc" / "plugin.py").read_text()
        for forbidden in ("openssl", "subprocess", "create_subprocess_exec", "backend.adapters", "import httpx"):
            self.assertNotIn(forbidden, source)


class HNTVSXBCIsolatedRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_hntv_and_sxbc_health_without_system_openssl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("hntv", "sxbc"):
                project = ROOT / "bundled_plugins" / name
                manifest = load_manifest(project / "manifest.json")
                self.assertEqual(validate_project(project)["plugin"], f"org.waveflow/{name}")
                isolated = root / name
                isolated.mkdir()
                shutil.copyfile(project / "plugin.py", isolated / "plugin.py")
                shutil.copyfile(project / "manifest.json", isolated / "manifest.json")
                built = build_project(isolated, output=root / f"{name}.pyz")
                references = {
                    item["sha256"]: DEPENDENCIES / item["filename"]
                    for item in manifest.runtime["dependency_lock"]["artifacts"]
                }
                manager = PythonEnvironmentManager(root / "plugin-store" / name)
                environment = await manager.prepare(manifest, references)
                if name == "sxbc":
                    probe = await asyncio.create_subprocess_exec(
                        str(environment.python), "-I", "-c",
                        "import cryptography,sys; print(cryptography.__file__); print(sys.prefix != sys.base_prefix)",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        env={"PATH": "/nonexistent", "PYTHONNOUSERSITE": "1"},
                    )
                    stdout, stderr = await probe.communicate()
                    self.assertEqual(probe.returncode, 0, stderr.decode())
                    self.assertIn(str(environment.path), stdout.decode().splitlines()[0])
                    self.assertEqual(stdout.decode().splitlines()[1], "True")
                process = PluginProcess(
                    (str(environment.python), "-I", str(built["artifact"]),
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
