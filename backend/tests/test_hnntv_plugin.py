from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ARTIFACT = Path(__file__).parents[1] / "bundled_plugins" / "hnntv" / "plugin.py"
IDENTITY = "org.waveflow/hnntv"
LIVE_STREAM = "https://media.example/hnntv/live.m3u8"
REPLAY_STREAM = "https://media.example/hnntv/replay.m3u8"
PLAYSEEK = "20260811120000-20260811123000"


class HNNTVPluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        for name in ("database", "plugin_market", "plugin_permissions", "plugin_production"):
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
            if self.mode == "http_error":
                return httpx.Response(503, text="unavailable")
            if request.url.path.endswith("/livePlayUrl"):
                return httpx.Response(200, json={"url": LIVE_STREAM})
            if request.url.host == "www.hnntv.cn":
                if self.mode == "bad_schedule":
                    return httpx.Response(200, json={"resultSet": "invalid"})
                schedules = [] if self.mode == "no_schedule" else [
                    {"id": "short", "startDatetime": "2026-08-11 12:00:00",
                     "endDatetime": "2026-08-11 12:20:00", "programName": "短节目"},
                    {"id": "long", "startDatetime": "2026-08-11 12:00:00",
                     "endDatetime": "2026-08-11 13:00:00", "programName": "长节目"},
                ]
                return httpx.Response(200, json={"resultSet": [{"schedules": schedules}]})
            if self.mode == "no_replay":
                return httpx.Response(200, json={})
            return httpx.Response(200, text=json.dumps({"url": REPLAY_STREAM}).replace("/", "\\/"))

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.store = plugin_market.PluginArtifactStore(Path(self.tmp.name) / "store", allowed_local_roots=[ARTIFACT.parent])
        self.service = plugin_market.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "hnntv-test-key"): public}),
            command_factory=lambda manifest, artifact: (sys.executable, str(artifact), "--identity", manifest.identity,
                "--version", manifest.version, "--fixture-live-url", LIVE_STREAM), os_name="linux", arch="x86_64")
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

    def package(self, version="1.0.0", *, direct=True):
        payload = ARTIFACT.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        manifest = {"manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "hnntv",
            "display_name": "HNNTV Provider", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "hnntv", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"],
            "permissions": {"network": {"managed": True, **({"direct": True} if direct else {}),
                                          "allowed_hosts": ["www.hnntv.cn", "ps.hnntv.cn"]}},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "python", "entrypoint": "plugin.py",
                "sha256": digest, "size_bytes": len(payload), "signature": {"algorithm": "ed25519",
                    "key_id": "hnntv-test-key", "value": base64.b64encode(self.private.sign(payload)).decode()}}],
            "dependencies": [], "state_schema_version": 1}
        return {"id": "official::hnntv-plugin", "package_type": "plugin_package", "version": version,
            "plugin_manifest": manifest, "artifact_references": [{"sha256": digest, "local_path": str(ARTIFACT)}],
            "market_source": {"source_key": "official"}}

    async def approve_install(self, version="1.0.0"):
        package = self.package(version)
        await self.service.approve_permission(IDENTITY, [package], "network.direct", "test-admin")
        return await self.service.install_from_packages([package], IDENTITY)

    @staticmethod
    async def legacy(target, client):
        from adapters import parse_adapter_url
        from adapters.hnntv import resolve_hnntv
        return await resolve_hnntv(parse_adapter_url(target), client)

    async def test_direct_live_request_and_approval_gate(self):
        spec = importlib.util.spec_from_file_location("hnntv_direct_fixture", ARTIFACT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        captured = {}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def read(self): return json.dumps({"data": {"playUrl": LIVE_STREAM}}).encode()

        def opener(request, **kwargs):
            captured.update(url=request.full_url, headers=dict(request.header_items()), kwargs=kwargs)
            return Response()

        payload, raw = module.direct_live("STHaiNan_channel_lywsgq", opener=opener)
        self.assertEqual(module.extract_url(payload, raw), LIVE_STREAM)
        self.assertEqual(captured["url"],
                         "http://ps.hnntv.cn/ps/livePlayUrl?appCode=&token=&channelCode=STHaiNan_channel_lywsgq")
        self.assertEqual(captured["kwargs"], {"timeout": 10})
        self.assertEqual(captured["headers"]["Referer"], "https://www.hnntv.cn/")
        from plugin_runtime import PluginError
        with self.assertRaises(PluginError) as pending:
            await self.service.install_from_packages([self.package()], IDENTITY)
        self.assertEqual(pending.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        installed = await self.approve_install()
        self.assertEqual(installed["lifecycle_state"], "active")

    async def test_live_and_replay_legacy_plugin_golden(self):
        from provider_resolver import ProviderResolver
        await self.approve_install()
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        legacy_live = await resolver.resolve("hnntv://hnws", self.client)
        resolver.set_mode("hnntv", "plugin", IDENTITY)
        plugin_live = await resolver.resolve("adapter://hnntv/hnws", self.client)
        for key in ("url", "source_type", "headers", "requires_proxy", "ttl", "expires_at", "volatile_url"):
            self.assertEqual(plugin_live[key], legacy_live[key], key)
        resolver.set_mode("hnntv", "legacy")
        legacy_replay = await resolver.resolve(f"hnntv://hnws?playseek={PLAYSEEK}", self.client)
        resolver.set_mode("hnntv", "plugin", IDENTITY)
        plugin_replay = await resolver.resolve(f"adapter://hnntv/hnws?playseek={PLAYSEEK}", self.client)
        for key in ("url", "source_type", "headers", "requires_proxy", "ttl", "expires_at", "volatile_url"):
            self.assertEqual(plugin_replay[key], legacy_replay[key], key)
        descriptor = await self.runtime.request(self.runtime.registry.route("hnntv"), "tv.resolve_stream",
            {"resource_id": "hnws", "query": {"playseek": [PLAYSEEK]}})
        self.assertEqual(descriptor["provider_diagnostics"], {"schedule_id": "long", "program_name": "长节目"})
        schedule, replay = self.requests[-2:]
        self.assertEqual(dict(schedule.url.params), {"channelId": "13"})
        self.assertEqual(dict(replay.url.params)["scheduleId"], "long")
        resolver.set_mode("hnntv", "legacy")

    async def test_errors_update_recovery_ownership_and_source_purity(self):
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_production import ProductionPluginSubsystem
        from plugin_runtime import PermissionPolicy, PluginError, PluginRuntime
        from provider_resolver import ProviderResolver
        await self.approve_install()
        instance = self.runtime.registry.route("hnntv")
        for resource in ("ssws", "xwpd", "sepd"):
            self.assertEqual((await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": resource}))["url"], LIVE_STREAM)
        with self.assertRaises(PluginError) as invalid:
            await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "unknown"})
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")
        with self.assertRaises(PluginError) as bad_seek:
            await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "hnws", "query": {"playseek": ["bad"]}})
        self.assertEqual(bad_seek.exception.code, "RESOURCE_NOT_FOUND")
        for mode in ("bad_schedule", "no_schedule", "no_replay", "http_error"):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(PluginError) as failed:
                await self.runtime.request(instance, "tv.resolve_stream", {"resource_id": "hnws", "query": {"playseek": [PLAYSEEK]}})
            self.assertIn(failed.exception.code, {"TEMPORARY_UPSTREAM_FAILURE", "INVALID_CAPABILITY_REQUEST"})
        self.mode = "success"
        await self.service.install_from_packages([self.package("1.1.0")], IDENTITY)
        self.assertEqual(self.runtime.registry.route("hnntv").manifest.version, "1.1.0")
        factory = self.service.command_factory
        await self.runtime.shutdown()
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.service = self.pm.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=self.service.trust_policy, command_factory=factory, os_name="linux", arch="x86_64")
        self.assertEqual(await self.service.recover_enabled(), [{"plugin": IDENTITY, "status": "active"}])
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        subsystem = ProductionPluginSubsystem(self.service, self.service.trust_policy, Path(self.tmp.name) / "downloads",
            self.client, resolver, CapabilityGateway(client=self.client))
        await subsystem.set_ownership("hnntv", "plugin", IDENTITY)
        self.assertEqual((await resolver.resolve("hnntv://hnws", self.client))["url"], LIVE_STREAM)
        with self.assertRaises(PluginError):
            await subsystem.revoke_permission(IDENTITY, "network.direct", "test-admin")
        await subsystem.set_ownership("hnntv", "legacy")
        self.assertTrue(await subsystem.uninstall(IDENTITY))
        source = ARTIFACT.read_text()
        for forbidden in ("import backend", "import httpx", "import requests", "os.environ", "FastAPI"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
