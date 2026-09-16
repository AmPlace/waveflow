from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


ARTIFACT = Path(__file__).parents[1] / "bundled_plugins" / "sdtv" / "plugin.py"
DEPENDENCIES = Path(__file__).parent / "fixtures" / "dependencies"
IDENTITY = "org.waveflow/sdtv"
FIXED_TIME = 1_700_000_000.123
TIMESTAMP = int(FIXED_TIME * 1000)
PAGE_URL = "https://v.iqilu.com/live/sdtv/"
EXCHANGE_URL = "https://feiying.litenews.cn/api/v1/auth/exchange"
SALT = "fixture-salt"
AES_KEY = "fixture-aes-key!"
STREAM = "https://media.example/sdtv/live.m3u8?fixture=1"
WHEEL_SPECS = (
    ("cryptography", "48.0.1", "cryptography-48.0.1-cp311-abi3-macosx_10_9_universal2.whl", "cp311", "abi3", "macosx_10_9_universal2"),
    ("cffi", "2.0.0", "cffi-2.0.0-cp314-cp314-macosx_11_0_arm64.whl", "cp314", "cp314", "macosx_11_0_arm64"),
    ("pycparser", "3.0", "pycparser-3.0-py3-none-any.whl", "py3", "none", "any"),
)


def crypt(value: bytes, key: str, *, encrypt: bool) -> bytes:
    key_bytes = key.encode()[:16].ljust(16, b"0")
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(b"0" * 16))
    if encrypt:
        padder = padding.PKCS7(128).padder()
        value = padder.update(value) + padder.finalize()
        worker = cipher.encryptor()
        return worker.update(value) + worker.finalize()
    worker = cipher.decryptor()
    value = worker.update(value) + worker.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(value) + unpadder.finalize()


class SDTVPluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        for name in ("database", "plugin_market", "plugin_production"):
            sys.modules.pop(name, None)
        import database, plugin_market
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_python_runtime import PythonEnvironmentManager
        from plugin_runtime import PermissionPolicy, PluginRuntime
        self.db, self.pm = database, plugin_market
        await database.initialize()
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.mode = "success"
        self.requests: list[httpx.Request] = []

        def upstream(request: httpx.Request):
            self.requests.append(request)
            if self.mode == "http_error":
                return httpx.Response(503, text="unavailable")
            if request.url.host == "v.iqilu.com":
                if self.mode == "bad_page":
                    return httpx.Response(200, text="no provider keys")
                return httpx.Response(200, text=f"mxpx = '{SALT}'; aly = '{AES_KEY}';")
            if self.mode == "bad_ciphertext":
                return httpx.Response(200, text="not-base64")
            result = {} if self.mode == "missing_url" else {"data": STREAM}
            encrypted = crypt(json.dumps(result).encode(), AES_KEY, encrypt=True)
            return httpx.Response(200, text=base64.b64encode(encrypted).decode())

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        roots = [ARTIFACT.parent, DEPENDENCIES]
        self.store = plugin_market.PluginArtifactStore(Path(self.tmp.name) / "store", allowed_local_roots=roots)
        self.envs = PythonEnvironmentManager(Path(self.tmp.name) / "store" / "python")
        self.service = plugin_market.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "sdtv-test-key"): public}),
            python_environments=self.envs, os_name="macos", arch="arm64")
        self.safe = mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock())
        self.safe.start()

    async def asyncTearDown(self):
        self.safe.stop()
        await self.runtime.shutdown()
        await self.client.aclose()
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        self.tmp.cleanup()

    def package(self, version="1.0.0", *, trusted=True):
        payload = ARTIFACT.read_bytes()
        artifact_digest = hashlib.sha256(payload).hexdigest()
        lock, references = [], []
        for name, dependency_version, filename, python_tag, abi_tag, platform_tag in WHEEL_SPECS:
            path = DEPENDENCIES / filename
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lock.append({"name": name, "version": dependency_version, "filename": filename,
                         "url": f"https://files.pythonhosted.org/packages/waveflow-fixture/{filename}",
                         "sha256": digest, "size_bytes": path.stat().st_size,
                         "python_tag": python_tag, "abi_tag": abi_tag, "platform_tag": platform_tag})
            references.append({"sha256": digest, "local_path": str(path)})
        signature = self.private.sign(payload) if trusted else b"invalid"
        manifest = {"manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "sdtv",
            "display_name": "SDTV Provider", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "sdtv", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"],
            "permissions": {"network": {"managed": True, "allowed_hosts": ["v.iqilu.com", "feiying.litenews.cn"]}},
            "runtime": {"type": "python", "ipc": "stdio_framed_json_v1",
                        "python_version_range": ">=3.14.0 <3.15.0", "entrypoint": "plugin.py",
                        "dependency_lock": {"lock_version": 1, "artifacts": lock}},
            "artifacts": [{"os": "macos", "arch": "arm64", "runtime": "python", "entrypoint": "plugin.py",
                "sha256": artifact_digest, "size_bytes": len(payload), "signature": {"algorithm": "ed25519",
                    "key_id": "sdtv-test-key", "value": base64.b64encode(signature).decode()}}],
            "dependencies": [], "state_schema_version": 1}
        return {"id": "official::sdtv-plugin", "package_type": "plugin_package", "version": version,
            "plugin_manifest": manifest,
            "artifact_references": [{"sha256": artifact_digest, "local_path": str(ARTIFACT)}],
            "dependency_references": references, "market_source": {"source_key": "official"}}

    async def install(self, version="1.0.0"):
        return await self.service.install_from_packages([self.package(version)], IDENTITY)

    @staticmethod
    async def legacy(target, client):
        from adapters import parse_adapter_url
        from adapters.sdtv import resolve_sdtv
        with mock.patch("adapters.sdtv.time.time", return_value=FIXED_TIME):
            return await resolve_sdtv(parse_adapter_url(target), client)

    async def test_isolated_crypto_dependency_request_and_legacy_plugin_golden(self):
        from provider_resolver import ProviderResolver
        installed = await self.install()
        self.assertEqual(installed["runtime"]["dependencies"],
                         [{"name": name, "version": version} for name, version, *_ in WHEEL_SPECS])
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        legacy = await resolver.resolve("sdtv://sdws", self.client)
        resolver.set_mode("sdtv", "plugin", IDENTITY)
        plugin = await resolver.resolve("adapter://sdtv/sdws", self.client)
        for key in ("url", "source_type", "headers", "requires_proxy", "ttl", "expires_at", "volatile_url"):
            self.assertEqual(plugin[key], legacy[key], key)
        page, exchange = self.requests[-2:]
        self.assertEqual((page.method, str(page.url)), ("GET", PAGE_URL))
        exchange_query = parse_qs(exchange.url.query.decode())
        plugin_timestamp = int(exchange_query["t"][0])
        expected_signature = hashlib.md5(f"24581{plugin_timestamp}{SALT}".encode()).hexdigest()
        self.assertEqual(str(exchange.url.copy_with(query=None)), EXCHANGE_URL)
        self.assertEqual(exchange_query["s"], [expected_signature])
        cleartext = crypt(base64.b64decode(exchange.content), AES_KEY, encrypt=False)
        self.assertEqual(json.loads(cleartext), {"channelMark": "24581"})
        descriptor = await self.runtime.request(self.runtime.registry.route("sdtv"), "tv.resolve_stream", {"resource_id": "sdws"})
        origin = descriptor["provider_diagnostics"]["dependency_origin"]
        self.assertIn(str(self.envs.environments_root), origin)
        self.assertNotIn(str(Path(sys.prefix) / "lib"), origin)
        resolver.set_mode("sdtv", "legacy")

    async def test_channels_errors_update_recovery_and_ownership(self):
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_production import ProductionPluginSubsystem
        from plugin_runtime import PermissionPolicy, PluginError, PluginRuntime
        from provider_resolver import ProviderResolver
        await self.install()
        instance = self.runtime.registry.route("sdtv")
        self.assertEqual((await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "24584"}))["url"], STREAM)
        with self.assertRaises(PluginError) as invalid:
            await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "unknown"})
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")
        for mode in ("bad_page", "bad_ciphertext", "missing_url", "http_error"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(PluginError) as failed:
                await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "sdws"})
            self.assertIn(failed.exception.code, {"TEMPORARY_UPSTREAM_FAILURE", "INVALID_CAPABILITY_REQUEST"})
        self.mode = "success"
        await self.install("1.1.0")
        self.assertEqual(self.runtime.registry.route("sdtv").manifest.version, "1.1.0")
        await self.runtime.shutdown()
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.service = self.pm.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=self.service.trust_policy, python_environments=self.envs, os_name="macos", arch="arm64")
        self.assertEqual(await self.service.recover_enabled(), [{"plugin": IDENTITY, "status": "active"}])
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        subsystem = ProductionPluginSubsystem(self.service, self.service.trust_policy, Path(self.tmp.name) / "downloads",
            self.client, resolver, CapabilityGateway(client=self.client))
        await subsystem.set_ownership("sdtv", "plugin", IDENTITY)
        self.assertEqual((await resolver.resolve("sdtv://sdws", self.client))["url"], STREAM)
        with self.assertRaises(PluginError):
            await subsystem.uninstall(IDENTITY)
        await subsystem.set_ownership("sdtv", "legacy")
        self.assertTrue(await subsystem.uninstall(IDENTITY))
        self.assertEqual(len(self.envs.cache_objects()), 3)

    async def test_lock_is_complete_and_source_has_no_core_or_http_import(self):
        package = self.package()
        self.assertEqual({item["name"] for item in package["plugin_manifest"]["runtime"]["dependency_lock"]["artifacts"]},
                         {"cryptography", "cffi", "pycparser"})
        source = ARTIFACT.read_text()
        for forbidden in ("import backend", "import httpx", "import requests", "os.environ", "FastAPI", "core.crypto"):
            self.assertNotIn(forbidden, source)
        with self.assertRaises(Exception) as untrusted:
            await self.service.install_from_packages([self.package(trusted=False)], IDENTITY)
        self.assertEqual(untrusted.exception.code, "AUTH_FAILED")


if __name__ == "__main__":
    unittest.main()
