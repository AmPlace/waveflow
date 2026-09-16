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


ARTIFACT = Path(__file__).parents[1] / "bundled_plugins" / "jstv" / "plugin.py"
IDENTITY = "org.waveflow/jstv"
FIXED_TIME = 1_700_000_000


class JSTVPluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        for name in ("database", "plugin_market", "plugin_production"):
            sys.modules.pop(name, None)
        import database, plugin_market
        from plugin_runtime import PluginRuntime
        self.db, self.pm = database, plugin_market
        await self.db.initialize()
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.runtime = PluginRuntime()
        self.store = plugin_market.PluginArtifactStore(Path(self.tmp.name) / "store", allowed_local_roots=[ARTIFACT.parent])
        self.service = plugin_market.PluginMarketService(
            runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "jstv-test-key"): public}),
            command_factory=lambda manifest, artifact: (
                sys.executable, str(artifact), "--identity", manifest.identity, "--version", manifest.version,
                "--fixed-time", str(FIXED_TIME)), os_name="linux", arch="x86_64")
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    async def asyncTearDown(self):
        await self.runtime.shutdown(); await self.client.aclose()
        if self.old_db is None: os.environ.pop("WAVEFLOW_DB_PATH", None)
        else: os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        self.tmp.cleanup()

    def package(self, version="1.0.0", *, trusted=True):
        payload = ARTIFACT.read_bytes(); digest = hashlib.sha256(payload).hexdigest()
        signature = self.private.sign(payload) if trusted else b"invalid"
        manifest = {"manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "jstv",
            "display_name": "JSTV Provider", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "jstv", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"], "permissions": {},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "python", "entrypoint": "plugin.py",
                "sha256": digest, "size_bytes": len(payload), "signature": {"algorithm": "ed25519",
                    "key_id": "jstv-test-key", "value": base64.b64encode(signature).decode()}}],
            "dependencies": [], "state_schema_version": 1}
        return {"id": "official::jstv-plugin", "package_type": "plugin_package", "version": version,
            "plugin_manifest": manifest, "artifact_references": [{"sha256": digest, "local_path": str(ARTIFACT)}],
            "market_source": {"source_key": "official"}}

    async def install(self, version="1.0.0"):
        return await self.service.install_from_packages([self.package(version)], IDENTITY)

    @staticmethod
    async def legacy(target, client):
        from adapters import parse_adapter_url
        from adapters.jstv import resolve_jstv
        with mock.patch("adapters.jstv.time.time", return_value=FIXED_TIME):
            return await resolve_jstv(parse_adapter_url(target), client)

    async def test_signed_install_local_signing_and_dual_track_golden(self):
        from provider_resolver import ProviderResolver
        installed = await self.install()
        self.assertEqual((installed["active_version"], installed["trust_state"]), ("1.0.0", "fixture_trusted"))
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        self.assertEqual(resolver.mode("jstv"), "legacy")
        legacy = await resolver.resolve("jstv://jsws", self.client)
        resolver.set_mode("jstv", "plugin", IDENTITY)
        plugin = await resolver.resolve("adapter://jstv/jsws", self.client)
        for key in ("url", "source_type", "headers", "requires_proxy", "ttl", "expires_at", "volatile_url"):
            self.assertEqual(plugin[key], legacy[key], key)
        self.assertTrue(plugin["direct_playable"])
        expected_time = format(FIXED_TIME + 180, "x")
        expected_sign = hashlib.md5(f"tJanAHkyGtaifaQG4dWejswspro{expected_time}".encode()).hexdigest()
        self.assertIn(f"txSecret={expected_sign}&txTime={expected_time}", plugin["url"])
        resolver.set_mode("jstv", "legacy")

    async def test_channels_direct_url_errors_and_source_purity(self):
        from plugin_runtime import PluginError
        await self.install(); instance = self.runtime.registry.route("jstv")
        for resource in ("jsws4k", "nanjing", "siyang"):
            result = await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": resource})
            self.assertEqual((result["transport"], result["ttl_seconds"], result["headers"]),
                             ("hls", 180, {"Referer": "https://live.jstv.com/"}))
        direct = await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "https://media.example/live.m3u8"})
        self.assertEqual(direct["url"], "https://media.example/live.m3u8")
        with self.assertRaises(PluginError) as error:
            await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "unknown"})
        self.assertEqual(error.exception.code, "RESOURCE_NOT_FOUND")
        source = ARTIFACT.read_text()
        for forbidden in ("import backend", "import httpx", "import requests", "os.environ", "FastAPI"):
            self.assertNotIn(forbidden, source)

    async def test_update_failure_recovery_disable_enable_uninstall(self):
        await self.install(); await self.install("1.1.0")
        self.assertEqual(self.runtime.registry.route("jstv").manifest.version, "1.1.0")
        factory = self.service.command_factory
        self.service.command_factory = lambda manifest, artifact: (
            sys.executable, str(artifact), "--identity", manifest.identity, "--version", "9.9.9")
        with self.assertRaises(Exception): await self.service.install_from_packages([self.package("1.2.0")], IDENTITY)
        self.assertEqual(self.runtime.registry.route("jstv").manifest.version, "1.1.0")
        self.service.command_factory = factory
        with self.assertRaises(Exception): await self.service.install_from_packages([self.package("1.2.0", trusted=False)], IDENTITY)
        await self.runtime.shutdown()
        from plugin_runtime import PluginRuntime
        self.runtime = PluginRuntime()
        self.service = self.pm.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=self.service.trust_policy, command_factory=factory, os_name="linux", arch="x86_64")
        self.assertEqual(await self.service.recover_enabled(), [{"plugin": IDENTITY, "status": "active"}])
        await self.service.disable(IDENTITY); await self.service.enable(IDENTITY)
        self.assertTrue(await self.service.uninstall(IDENTITY))

    async def test_production_ownership_guard_and_rollback(self):
        from plugin_capabilities import CapabilityGateway
        from plugin_production import ProductionPluginSubsystem
        from plugin_runtime import PluginError
        from provider_resolver import ProviderResolver
        await self.install()
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        subsystem = ProductionPluginSubsystem(self.service, self.service.trust_policy, Path(self.tmp.name) / "downloads",
            self.client, resolver, CapabilityGateway(client=self.client))
        await subsystem.set_ownership("jstv", "plugin", IDENTITY)
        result = await resolver.resolve("jstv://jscs", self.client)
        self.assertEqual(result["stream_descriptor_version"], "1.0")
        for operation in (subsystem.disable, subsystem.uninstall):
            with self.assertRaises(PluginError) as blocked: await operation(IDENTITY)
            self.assertEqual(blocked.exception.code, "SCHEME_CONFLICT")
        await subsystem.set_ownership("jstv", "legacy")
        self.assertNotIn("stream_descriptor_version", await resolver.resolve("jstv://jscs", self.client))


if __name__ == "__main__": unittest.main()
