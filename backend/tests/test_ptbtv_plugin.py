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


SOURCE = Path(__file__).parents[1] / "bundled_plugins" / "ptbtv" / "plugin.py"
DEPENDENCIES = Path(__file__).parent / "fixtures" / "dependencies" / "ptbtv"
LOCK = json.loads((DEPENDENCIES / "dependency-lock.json").read_text())
IDENTITY = "org.waveflow/ptbtv"
STREAM = "https://media.example/ptbtv/live.m3u8?fixture=1"


class PTBTVPluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        # plugin_production keeps a module-level database reference. Reload it
        # whenever this test swaps WAVEFLOW_DB_PATH so ownership checks cannot
        # leak a database module from an earlier test case.
        for name in ("database", "plugin_market", "plugin_permissions", "plugin_production"):
            sys.modules.pop(name, None)
        import database, plugin_market
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_python_runtime import PythonEnvironmentManager
        from plugin_runtime import PermissionPolicy, PluginRuntime
        from waveflow_plugin_cli import build_sdk_artifact
        self.db, self.pm = database, plugin_market; await self.db.initialize()
        self.artifact = Path(self.tmp.name) / "ptbtv-plugin.pyz"
        build_sdk_artifact(SOURCE, self.artifact)
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.requests = []
        self.mode = "success"
        def upstream(request):
            self.requests.append(request)
            if self.mode == "http_error": return httpx.Response(403, text="denied")
            if self.mode == "timeout": raise httpx.ReadTimeout("fixture timeout", request=request)
            if self.mode == "invalid_json": return httpx.Response(200, text="not-json")
            values = {"empty": [], "missing": [{}], "invalid_url": [{"m3u8": "file:///bad"}]}
            return httpx.Response(200, json=values.get(self.mode, [{"m3u8": STREAM}]))
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.store = plugin_market.PluginArtifactStore(Path(self.tmp.name) / "store",
            allowed_local_roots=[self.artifact.parent, DEPENDENCIES])
        self.envs = PythonEnvironmentManager(Path(self.tmp.name) / "store" / "python")
        def runtime_command(manifest, artifact, environment):
            python = str(environment.python) if environment else sys.executable
            return (python, "-I", str(artifact), "--identity", manifest.identity, "--version", manifest.version,
                    "--fixture-direct-unavailable")
        self.service = plugin_market.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "ptbtv-test-key"): public}),
            python_environments=self.envs, runtime_command_factory=runtime_command, os_name="macos", arch="arm64")
        self.safe = mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()); self.safe.start()

    async def asyncTearDown(self):
        self.safe.stop(); await self.runtime.shutdown(); await self.client.aclose()
        if self.old_db is None: os.environ.pop("WAVEFLOW_DB_PATH", None)
        else: os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        self.tmp.cleanup()

    def package(self, version="1.0.0"):
        payload = self.artifact.read_bytes(); digest = hashlib.sha256(payload).hexdigest()
        manifest = {"manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "ptbtv",
            "display_name": "PTBTV Provider", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "ptbtv", "contract": "tv_provider"}], "capabilities": ["tv.resolve_stream"],
            "permissions": {"network": {"managed": True, "direct": True, "allowed_hosts": ["www.ptbtv.com"]}},
            "runtime": {"type": "python", "ipc": "stdio_framed_json_v1", "python_version_range": ">=3.14.0 <3.15.0",
                "entrypoint": "plugin.pyz", "dependency_lock": LOCK},
            "artifacts": [{"os": "macos", "arch": "arm64", "runtime": "python", "entrypoint": "plugin.pyz",
                "sha256": digest, "size_bytes": len(payload), "signature": {"algorithm": "ed25519", "key_id": "ptbtv-test-key",
                    "value": base64.b64encode(self.private.sign(payload)).decode()}}], "dependencies": [], "state_schema_version": 1}
        return {"id": "official::ptbtv-plugin", "package_type": "plugin_package", "version": version,
            "plugin_manifest": manifest, "artifact_references": [{"sha256": digest, "local_path": str(self.artifact)}],
            "dependency_references": [{"sha256": item["sha256"], "local_path": str(DEPENDENCIES / item["filename"])} for item in LOCK["artifacts"]],
            "market_source": {"source_key": "official"}}

    async def approve_install(self, version="1.0.0"):
        package = self.package(version)
        await self.service.approve_permission(IDENTITY, [package], "network.direct", "test-admin")
        return await self.service.install_from_packages([package], IDENTITY)

    async def test_real_direct_request_construction_with_injected_session(self):
        spec = importlib.util.spec_from_file_location("ptbtv_direct_fixture", SOURCE)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        captured = {}
        class Response: status_code = 200; text = json.dumps([{"m3u8": STREAM}])
        class Session:
            def __init__(self, **kwargs): captured["init"] = kwargs
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def get(self, url, **kwargs): captured.update(url=url, request=kwargs); return Response()
        headers = module.build_headers("https://www.ptbtv.com/live/pt1t/", now=lambda: 1700000000)
        result = await module.direct_fetch("4", headers, session_factory=Session)
        self.assertEqual(result[0], 200); self.assertEqual(captured["init"], {"impersonate": "chrome", "timeout": 10})
        self.assertEqual(captured["url"], module.API_URL); self.assertEqual(captured["request"]["params"], {"channel_id": "4"})
        expected = hashlib.md5(f"{module.API_KEY}&{module.API_SECRET}&{module.API_VERSION}&1700000000".encode()).hexdigest()
        self.assertEqual((headers["X-API-TIMESTAMP"], headers["X-API-SIGNATURE"], headers["User-Agent"]),
                         ("1700000000", expected, module.USER_AGENT))
        class FailedSession(Session):
            async def get(self, *_args, **_kwargs): raise RuntimeError("curl failed")
        self.assertIsNone(await module.direct_fetch("4", headers, session_factory=FailedSession))
        class Non200Session(Session):
            async def get(self, *_args, **_kwargs):
                value = Response(); value.status_code = 403; return value
        self.assertEqual((await module.direct_fetch("4", headers, session_factory=Non200Session))[0], 403)

    async def test_market_approval_environment_import_and_managed_fallback(self):
        from plugin_runtime import PluginError
        package = self.package()
        with self.assertRaises(PluginError) as pending: await self.service.install_from_packages([package], IDENTITY)
        self.assertEqual(pending.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        await self.approve_install()
        instance = self.runtime.registry.route("ptbtv")
        result = await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "ptbtv", "resource_id": "pt1"})
        self.assertEqual((result["url"], result["ttl_seconds"], result["volatile_url"], result["requires_proxy"]),
                         (STREAM, 180, True, False))
        origin = result["provider_diagnostics"]["dependency_origin"]
        self.assertIn(str(self.envs.environments_root), origin)
        self.assertEqual(dict(self.requests[-1].url.params), {"channel_id": "4"})
        self.assertEqual(self.requests[-1].headers["origin"], "https://www.ptbtv.com")

    async def test_aliases_response_errors_and_source_purity(self):
        from plugin_runtime import PluginError
        await self.approve_install()
        instance = self.runtime.registry.route("ptbtv")
        for resource, channel_id in (("1", "4"), ("ptbtv-2", "5"), ("xy", "6")):
            result = await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "ptbtv", "resource_id": resource})
            self.assertEqual(result["url"], STREAM)
            self.assertEqual(dict(self.requests[-1].url.params), {"channel_id": channel_id})
        with self.assertRaises(PluginError) as unknown:
            await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "ptbtv", "resource_id": "unknown"})
        self.assertEqual((unknown.exception.code, unknown.exception.retryable), ("RESOURCE_NOT_FOUND", False))
        for mode in ("http_error", "timeout", "invalid_json", "empty", "missing", "invalid_url"):
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(PluginError) as failed:
                    await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "ptbtv", "resource_id": "1"})
                self.assertIn(failed.exception.code, {"TEMPORARY_UPSTREAM_FAILURE", "PLUGIN_TIMEOUT"})
                self.assertTrue(failed.exception.retryable)
        source = SOURCE.read_text(encoding="utf-8")
        for forbidden in ("import backend", "import httpx", "import requests", "os.environ", "FastAPI"):
            self.assertNotIn(forbidden, source)
        self.assertIn("from curl_cffi.requests import AsyncSession", source)

    async def test_legacy_plugin_golden_ownership_and_update_recovery(self):
        from adapters import parse_adapter_url
        from adapters.ptbtv import resolve_ptbtv
        from provider_resolver import ProviderResolver
        async def legacy(target, client): return await resolve_ptbtv(parse_adapter_url(target), client)
        async def no_curl(*_args, **_kwargs): return None
        await self.approve_install()
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=legacy)
        with mock.patch("adapters.ptbtv._fetch_with_curl_cffi", new=no_curl):
            legacy_result = await resolver.resolve("ptbtv://1", self.client)
        resolver.set_mode("ptbtv", "plugin", IDENTITY); plugin = await resolver.resolve("adapter://ptbtv/pt1", self.client)
        for key in ("url", "source_type", "headers", "requires_proxy", "ttl", "expires_at", "volatile_url"):
            self.assertEqual(plugin[key], legacy_result[key], key)
        resolver.set_mode("ptbtv", "legacy")
        await self.service.install_from_packages([self.package("1.1.0")], IDENTITY)
        self.assertEqual(self.runtime.registry.route("ptbtv").manifest.version, "1.1.0")
        await self.runtime.shutdown()

    async def test_production_ownership_revoke_guard_and_restart_permission_gate(self):
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_production import ProductionPluginSubsystem
        from plugin_runtime import PermissionPolicy, PluginError, PluginRuntime
        from provider_resolver import ProviderResolver
        package = self.package()
        await self.approve_install()
        resolver = ProviderResolver(runtime=self.runtime)
        subsystem = ProductionPluginSubsystem(self.service, self.service.trust_policy,
            Path(self.tmp.name) / "downloads", self.client, resolver, CapabilityGateway(client=self.client))
        await subsystem.set_ownership("ptbtv", "plugin", IDENTITY)
        with self.assertRaises(PluginError) as owned:
            await subsystem.revoke_permission(IDENTITY, "network.direct", "test-admin")
        self.assertEqual(owned.exception.code, "SCHEME_CONFLICT")
        await subsystem.set_ownership("ptbtv", "legacy")
        await subsystem.revoke_permission(IDENTITY, "network.direct", "test-admin")
        await self.runtime.shutdown()
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        runtime_command = self.service.runtime_command_factory
        self.service = self.pm.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=self.service.trust_policy, python_environments=self.envs,
            runtime_command_factory=runtime_command, os_name="macos", arch="arm64")
        recovery = await self.service.recover_enabled()
        self.assertEqual(recovery[0]["status"], "unavailable")
        self.assertIn("PERMISSION_APPROVAL_REQUIRED", recovery[0]["error"])
        await self.service.approve_permission(IDENTITY, [package], "network.direct", "test-admin")
        self.assertEqual((await self.service.enable(IDENTITY))["lifecycle_state"], "active")


if __name__ == "__main__": unittest.main()
