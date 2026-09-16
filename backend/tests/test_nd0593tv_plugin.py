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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ARTIFACT = Path(__file__).parents[1] / "bundled_plugins" / "nd0593tv" / "plugin.py"
IDENTITY = "org.waveflow/nd0593tv"
API_URL = "https://app.0593tv.cn/jhxtapi/jhxt/Live/detail"
STREAM = "https://media.example/nd0593tv/live.m3u8"
UA = "QZWireless/20241122 CFNetwork/3860.500.112 Darwin/25.4.0"


class ND0593TVPluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        for name in ("database", "plugin_market", "plugin_production"):
            sys.modules.pop(name, None)
        import database, plugin_market
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_runtime import PermissionPolicy, PluginRuntime
        self.db, self.pm = database, plugin_market
        await self.db.initialize()
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
            if self.mode == "business_error":
                return httpx.Response(200, json={"code": 500, "data": {}})
            if self.mode == "missing_data":
                return httpx.Response(200, json={"code": 200})
            if self.mode == "invalid_url":
                return httpx.Response(200, json={"code": 200, "data": {"link": "file:///bad"}})
            return httpx.Response(200, json={"code": 200, "data": {"link": STREAM}})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.runtime = PluginRuntime(
            permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)),
        )
        self.store = plugin_market.PluginArtifactStore(Path(self.tmp.name) / "store", allowed_local_roots=[ARTIFACT.parent])
        self.service = plugin_market.PluginMarketService(
            runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "nd0593tv-test-key"): public}),
            command_factory=lambda manifest, artifact: (
                sys.executable, str(artifact), "--identity", manifest.identity, "--version", manifest.version),
            os_name="linux", arch="x86_64")
        self.safe = mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock())
        self.safe.start()

    async def asyncTearDown(self):
        self.safe.stop()
        await self.runtime.shutdown(); await self.client.aclose()
        if self.old_db is None: os.environ.pop("WAVEFLOW_DB_PATH", None)
        else: os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        self.tmp.cleanup()

    def package(self, version="1.0.0", *, trusted=True):
        payload = ARTIFACT.read_bytes(); digest = hashlib.sha256(payload).hexdigest()
        signature = self.private.sign(payload) if trusted else b"invalid"
        manifest = {"manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "nd0593tv",
            "display_name": "ND0593TV Provider", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "nd0593tv", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"], "permissions": {"network": {"managed": True, "allowed_hosts": ["app.0593tv.cn"]}},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "python", "entrypoint": "plugin.py",
                "sha256": digest, "size_bytes": len(payload), "signature": {"algorithm": "ed25519",
                    "key_id": "nd0593tv-test-key", "value": base64.b64encode(signature).decode()}}],
            "dependencies": [], "state_schema_version": 1}
        return {"id": "official::nd0593tv-plugin", "package_type": "plugin_package", "version": version,
            "plugin_manifest": manifest, "artifact_references": [{"sha256": digest, "local_path": str(ARTIFACT)}],
            "market_source": {"source_key": "official"}}

    async def install(self, version="1.0.0"):
        return await self.service.install_from_packages([self.package(version)], IDENTITY)

    @staticmethod
    async def legacy(target, client):
        from adapters import parse_adapter_url
        from adapters.nd0593tv import resolve_nd0593tv
        return await resolve_nd0593tv(parse_adapter_url(target), client)

    async def test_post_alias_body_and_golden(self):
        from provider_resolver import ProviderResolver
        await self.install()
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        legacy = await resolver.resolve("nd0593tv://news", self.client)
        resolver.set_mode("nd0593tv", "plugin", IDENTITY)
        plugin = await resolver.resolve("adapter://nd0593tv/21", self.client)
        for key in ("url", "source_type", "headers", "requires_proxy", "ttl", "expires_at", "volatile_url"):
            self.assertEqual(plugin[key], legacy[key], key)
        request = self.requests[-1]
        self.assertEqual((request.method, str(request.url)), ("POST", API_URL))
        self.assertEqual(request.headers["user-agent"], UA)
        self.assertEqual(request.headers["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(parse_qs(request.content.decode(), keep_blank_values=True), {"uid": ["0"], "device": [""], "nid": [""], "lid": ["21"], "siteid": ["1"]})

    async def test_business_malformed_and_transport_errors(self):
        from plugin_runtime import PluginError
        await self.install()
        instance = self.runtime.registry.route("nd0593tv")
        for resource in ("20", "culture", "nd-news"):
            result = await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": resource})
            self.assertEqual((result["transport"], result["ttl_seconds"], result["volatile_url"]), ("hls", 1800, True))
        for mode in ("business_error", "missing_data", "invalid_url", "invalid_json", "http_error", "timeout"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(PluginError) as raised:
                await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "news"})
            self.assertIn(raised.exception.code, {"TEMPORARY_UPSTREAM_FAILURE", "PLUGIN_TIMEOUT"})
        with self.assertRaises(PluginError) as invalid:
            await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "missing"})
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")
        source = ARTIFACT.read_text()
        for forbidden in ("import backend", "import httpx", "import requests", "os.environ", "FastAPI"):
            self.assertNotIn(forbidden, source)

    async def test_update_recovery_and_ownership_rollback(self):
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_production import ProductionPluginSubsystem
        from plugin_runtime import PluginError, PluginRuntime
        from provider_resolver import ProviderResolver
        await self.install(); await self.install("1.1.0")
        self.assertEqual(self.runtime.registry.route("nd0593tv").manifest.version, "1.1.0")
        await self.runtime.shutdown(); self.runtime = PluginRuntime(permission_policy=__import__("plugin_runtime").PermissionPolicy(frozenset({"network"})), capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.service = self.pm.PluginMarketService(runtime=self.runtime, store=self.store, trust_policy=self.service.trust_policy, command_factory=lambda manifest, artifact: (sys.executable, str(artifact), "--identity", manifest.identity, "--version", manifest.version), os_name="linux", arch="x86_64")
        self.assertEqual(await self.service.recover_enabled(), [{"plugin": IDENTITY, "status": "active"}])
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        subsystem = ProductionPluginSubsystem(self.service, self.service.trust_policy, Path(self.tmp.name) / "downloads", self.client, resolver, CapabilityGateway(client=self.client))
        await subsystem.set_ownership("nd0593tv", "plugin", IDENTITY)
        self.assertEqual((await resolver.resolve("nd0593tv://news", self.client))["url"], STREAM)
        with self.assertRaises(PluginError): await subsystem.disable(IDENTITY)
        await subsystem.set_ownership("nd0593tv", "legacy")
        await subsystem.disable(IDENTITY); await self.service.enable(IDENTITY)
        await subsystem.set_ownership("nd0593tv", "legacy")
        self.assertTrue(await subsystem.uninstall(IDENTITY))


if __name__ == "__main__": unittest.main()
