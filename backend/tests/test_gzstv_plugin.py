from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ARTIFACT = Path(__file__).parents[1] / "bundled_plugins" / "gzstv" / "plugin.py"
IDENTITY = "org.waveflow/gzstv"
FIXED_TIME = 1_700_000_000
EXPIRES_AT = FIXED_TIME + 600
STREAM = f"https://media.example/gzstv/live.m3u8?txTime={EXPIRES_AT:x}&txSecret=fixture"
API_URL = "https://api.gzstv.com/v1/tv/ch01/?fields=description%2Ctitle%2Cstream_url%2Cimage%2Cauthor"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class GZSTVPluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        for name in ("database", "plugin_market", "plugin_production"):
            sys.modules.pop(name, None)
        import database, plugin_market
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_runtime import PermissionPolicy, PluginRuntime
        self.pm = plugin_market
        await database.initialize()
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.mode = "success"
        self.requests: list[httpx.Request] = []

        def upstream(request: httpx.Request):
            self.requests.append(request)
            if self.mode == "timeout":
                raise httpx.ReadTimeout("fixture timeout", request=request)
            if self.mode == "http_error":
                return httpx.Response(503, text="unavailable")
            if self.mode == "invalid_json":
                return httpx.Response(200, text="not-json")
            if self.mode == "not_object":
                return httpx.Response(200, json=[])
            if self.mode == "missing_url":
                return httpx.Response(200, json={"title": "贵州卫视"})
            if self.mode == "invalid_url":
                return httpx.Response(200, json={"stream_url": "file:///bad"})
            if self.mode == "no_expiry":
                return httpx.Response(200, json={"stream_url": "https://media.example/gzstv/fallback.m3u8"})
            return httpx.Response(200, json={"title": "贵州卫视", "stream_url": STREAM})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.runtime = PluginRuntime(
            permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.store = plugin_market.PluginArtifactStore(Path(self.tmp.name) / "store", allowed_local_roots=[ARTIFACT.parent])
        self.service = plugin_market.PluginMarketService(
            runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "gzstv-test-key"): public}),
            command_factory=lambda manifest, artifact: (
                sys.executable, str(artifact), "--identity", manifest.identity, "--version", manifest.version,
                "--fixed-time", str(FIXED_TIME)), os_name="linux", arch="x86_64")
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
        digest = hashlib.sha256(payload).hexdigest()
        signature = self.private.sign(payload) if trusted else b"invalid"
        manifest = {"manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "gzstv",
            "display_name": "GZSTV Provider", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "gzstv", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"],
            "permissions": {"network": {"managed": True, "allowed_hosts": ["api.gzstv.com"]}},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "python", "entrypoint": "plugin.py",
                "sha256": digest, "size_bytes": len(payload), "signature": {"algorithm": "ed25519",
                    "key_id": "gzstv-test-key", "value": base64.b64encode(signature).decode()}}],
            "dependencies": [], "state_schema_version": 1}
        return {"id": "official::gzstv-plugin", "package_type": "plugin_package", "version": version,
            "plugin_manifest": manifest, "artifact_references": [{"sha256": digest, "local_path": str(ARTIFACT)}],
            "market_source": {"source_key": "official"}}

    async def install(self, version="1.0.0"):
        return await self.service.install_from_packages([self.package(version)], IDENTITY)

    @staticmethod
    async def legacy(target, client):
        from adapters import parse_adapter_url
        from adapters.gzstv import resolve_gzstv
        with mock.patch("adapters.gzstv.time.time", return_value=FIXED_TIME):
            return await resolve_gzstv(parse_adapter_url(target), client)

    async def test_get_expiry_descriptor_and_dual_track_golden(self):
        from provider_resolver import ProviderResolver
        await self.install()
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        legacy = await resolver.resolve("gzstv://1", self.client)
        resolver.set_mode("gzstv", "plugin", IDENTITY)
        plugin = await resolver.resolve("adapter://gzstv/ch01", self.client)
        for key in ("url", "source_type", "headers", "requires_proxy", "ttl", "expires_at", "volatile_url"):
            self.assertEqual(plugin[key], legacy[key], key)
        self.assertEqual((plugin["ttl"], plugin["expires_at"], plugin["direct_playable"]), (540, EXPIRES_AT, True))
        request = self.requests[-1]
        self.assertEqual((request.method, str(request.url)), ("GET", API_URL))
        self.assertEqual(request.headers["referer"], "https://www.gzstv.com/tv/ch01")
        self.assertEqual(request.headers["origin"], "https://www.gzstv.com")
        self.assertEqual(request.headers["user-agent"], UA)
        resolver.set_mode("gzstv", "legacy")

    async def test_channels_fallback_ttl_errors_and_source_purity(self):
        from plugin_runtime import PluginError
        await self.install()
        instance = self.runtime.registry.route("gzstv")
        for resource in ("2", "ch03", "13"):
            result = await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": resource})
            self.assertEqual((result["transport"], result["ttl_seconds"], result["expires_at"]),
                             ("hls", 540, EXPIRES_AT))
        self.mode = "no_expiry"
        fallback = await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "ch01"})
        self.assertEqual((fallback["ttl_seconds"], fallback["expires_at"]), (300, None))
        for mode in ("missing_url", "invalid_url", "not_object", "invalid_json", "http_error", "timeout"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(PluginError) as raised:
                await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "ch01"})
            self.assertIn(raised.exception.code, {"TEMPORARY_UPSTREAM_FAILURE", "PLUGIN_TIMEOUT", "INVALID_CAPABILITY_REQUEST"})
        with self.assertRaises(PluginError) as invalid:
            await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "ch99"})
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")
        source = ARTIFACT.read_text()
        for forbidden in ("import backend", "import httpx", "import requests", "os.environ", "FastAPI"):
            self.assertNotIn(forbidden, source)

    async def test_update_recovery_ownership_guard_and_rollback(self):
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_production import ProductionPluginSubsystem
        from plugin_runtime import PermissionPolicy, PluginError, PluginRuntime
        from provider_resolver import ProviderResolver
        await self.install()
        await self.install("1.1.0")
        self.assertEqual(self.runtime.registry.route("gzstv").manifest.version, "1.1.0")
        factory = self.service.command_factory
        self.service.command_factory = lambda manifest, artifact: (
            sys.executable, str(artifact), "--identity", manifest.identity, "--version", "9.9.9",
            "--fixed-time", str(FIXED_TIME))
        with self.assertRaises(Exception):
            await self.service.install_from_packages([self.package("1.2.0")], IDENTITY)
        self.assertEqual(self.runtime.registry.route("gzstv").manifest.version, "1.1.0")
        self.service.command_factory = factory
        with self.assertRaises(Exception):
            await self.service.install_from_packages([self.package("1.2.0", trusted=False)], IDENTITY)
        await self.runtime.shutdown()
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.service = self.pm.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=self.service.trust_policy, command_factory=factory, os_name="linux", arch="x86_64")
        self.assertEqual(await self.service.recover_enabled(), [{"plugin": IDENTITY, "status": "active"}])
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        subsystem = ProductionPluginSubsystem(self.service, self.service.trust_policy, Path(self.tmp.name) / "downloads",
            self.client, resolver, CapabilityGateway(client=self.client))
        await subsystem.set_ownership("gzstv", "plugin", IDENTITY)
        self.assertEqual((await resolver.resolve("gzstv://ch01", self.client))["url"], STREAM)
        for operation in (subsystem.disable, subsystem.uninstall):
            with self.assertRaises(PluginError) as blocked:
                await operation(IDENTITY)
            self.assertEqual(blocked.exception.code, "SCHEME_CONFLICT")
        await subsystem.set_ownership("gzstv", "legacy")
        await subsystem.disable(IDENTITY)
        await self.service.enable(IDENTITY)
        self.assertTrue(await subsystem.uninstall(IDENTITY))


if __name__ == "__main__":
    unittest.main()
