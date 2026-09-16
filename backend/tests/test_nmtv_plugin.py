from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
import xxtea
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging import tags


SOURCE = Path(__file__).parents[1] / "bundled_plugins" / "nmtv" / "plugin.py"
WHEEL = next((Path(__file__).parent / "fixtures" / "dependencies").glob("xxtea-5.0.0-*.whl"))
IDENTITY = "org.waveflow/nmtv"
STREAM = "https://media.example/nmtv/live.m3u8?fixture=1"
API_URL = "https://api-bt.nmtv.cn/broadcast/list"
KEY = b"5b28bae827e651b3"


def _clear_modules():
    for name in ("database", "plugin_market", "plugin_production"):
        sys.modules.pop(name, None)


class NMTVPluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        _clear_modules()
        import database
        import plugin_market
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_python_runtime import PythonEnvironmentManager
        from plugin_runtime import PermissionPolicy, PluginRuntime
        from waveflow_plugin_cli import build_sdk_artifact

        self.db, self.pm = database, plugin_market
        await self.db.initialize()
        self.artifact = Path(self.tmp.name) / "nmtv-plugin.pyz"
        build_sdk_artifact(SOURCE, self.artifact)
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.requests = []
        self.mode = "success"

        def upstream(request: httpx.Request):
            self.requests.append(request)
            if self.mode == "http_error":
                return httpx.Response(503, text="unavailable")
            if self.mode == "malformed":
                return httpx.Response(200, text="not-base64")
            rows = [] if self.mode == "missing" else [{"data": {"id": 262, "name": "内蒙古卫视",
                                                                   "streamUrls": [] if self.mode == "empty" else [STREAM]}}]
            raw = json.dumps({"data": rows}, separators=(",", ":")).encode()
            raw += b" " * (-len(raw) % 4)
            encrypted = xxtea.encrypt(raw, KEY, padding=False)
            return httpx.Response(200, text=json.dumps(base64.b64encode(encrypted).decode()))

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.store = plugin_market.PluginArtifactStore(Path(self.tmp.name) / "store", allowed_local_roots=[self.artifact.parent, WHEEL.parent])
        self.envs = PythonEnvironmentManager(Path(self.tmp.name) / "store" / "python")
        self.service = plugin_market.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "nmtv-test-key"): public}),
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
        _clear_modules()
        self.tmp.cleanup()

    def package(self, version="1.0.0", *, trusted=True):
        payload = self.artifact.read_bytes(); artifact_digest = hashlib.sha256(payload).hexdigest()
        wheel = WHEEL.read_bytes(); wheel_digest = hashlib.sha256(wheel).hexdigest()
        wheel_tag = next(tag for tag in tags.sys_tags() if str(tag) == "cp314-cp314-macosx_11_0_arm64")
        lock_item = {"name": "xxtea", "version": "5.0.0", "filename": WHEEL.name,
                     "url": "https://files.pythonhosted.org/packages/7c/76/1e4ced8038904aada6716ec729ddffbce509c12784c6b3f659cd8206e544/" + WHEEL.name,
                     "sha256": wheel_digest, "size_bytes": len(wheel), "python_tag": wheel_tag.interpreter,
                     "abi_tag": wheel_tag.abi, "platform_tag": wheel_tag.platform}
        signature = self.private.sign(payload) if trusted else b"invalid"
        manifest = {
            "manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "nmtv",
            "display_name": "NMTV Provider", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "nmtv", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"],
            "permissions": {"network": {"managed": True, "allowed_hosts": ["api-bt.nmtv.cn"]}},
            "runtime": {"type": "python", "ipc": "stdio_framed_json_v1",
                        "python_version_range": ">=3.14.0 <3.15.0", "entrypoint": "plugin.pyz",
                        "dependency_lock": {"lock_version": 1, "artifacts": [lock_item]}},
            "artifacts": [{"os": "macos", "arch": "arm64", "runtime": "python", "entrypoint": "plugin.pyz",
                           "sha256": artifact_digest, "size_bytes": len(payload),
                           "signature": {"algorithm": "ed25519", "key_id": "nmtv-test-key",
                                         "value": base64.b64encode(signature).decode()}}],
            "dependencies": [], "state_schema_version": 1,
        }
        return {"schema_version": 1, "id": "official::nmtv-plugin", "original_id": "nmtv-plugin",
                "name": "NMTV Plugin", "kind": "plugin_package", "package_type": "plugin_package",
                "version": version, "plugin_manifest": manifest,
                "artifact_references": [{"sha256": artifact_digest, "local_path": str(self.artifact)}],
                "dependency_references": [{"sha256": wheel_digest, "local_path": str(WHEEL)}],
                "market_source": {"source_key": "official"}}

    async def install(self, version="1.0.0"):
        return await self.service.install_from_packages([self.package(version)], IDENTITY)

    @staticmethod
    async def legacy(target, client):
        from adapters import parse_adapter_url
        from adapters.nmtv import resolve_nmtv
        return await resolve_nmtv(parse_adapter_url(target), client)

    async def test_real_dependency_environment_legacy_plugin_golden_and_request(self):
        from provider_resolver import ProviderResolver
        installed = await self.install()
        self.assertEqual(installed["runtime"]["dependencies"], [{"name": "xxtea", "version": "5.0.0"}])
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        self.assertEqual(resolver.mode("nmtv"), "legacy")
        legacy = await resolver.resolve("nmtv://nmws", self.client)
        resolver.set_mode("nmtv", "plugin", IDENTITY)
        plugin = await resolver.resolve("adapter://nmtv/nmws", self.client)
        for key in ("url", "source_type", "headers", "requires_proxy", "ttl", "expires_at", "volatile_url"):
            self.assertEqual(plugin[key], legacy[key], key)
        request = self.requests[-1]
        self.assertEqual((request.method, str(request.url.copy_with(query=None))), ("GET", API_URL))
        self.assertEqual(dict(request.url.params), {"size": "100", "type": "1"})
        self.assertEqual(request.headers["referer"], "https://www.nmtv.cn/")
        instance = self.runtime.registry.route("nmtv")
        descriptor = await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "nmtv", "resource_id": "nmws"})
        origin = descriptor["provider_diagnostics"]["dependency_origin"]
        self.assertTrue(str(self.envs.environments_root) in origin, origin)
        self.assertNotIn(str(Path(sys.prefix) / "lib"), origin)
        resolver.set_mode("nmtv", "legacy")

    async def test_post_commit_cancellation_keeps_committed_python_environment(self):
        await self.install("1.0.0")
        original_activate = self.db.activate_plugin_candidate
        committed = asyncio.Event()
        release = asyncio.Event()

        async def commit_then_pause(**kwargs):
            result = await original_activate(**kwargs)
            committed.set()
            await release.wait()
            return result

        with mock.patch.object(self.db, "activate_plugin_candidate", new=commit_then_pause):
            update = asyncio.create_task(self.install("1.1.0"))
            await committed.wait()
            update.cancel()
            await asyncio.sleep(0)
            self.assertFalse(update.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await update

        row = await self.db.get_plugin_installation("org.waveflow", "nmtv")
        public = self.service._public(row)
        active = self.runtime.registry.route("nmtv")
        environment = self.envs.environment_path(active.manifest, public["runtime"]["lock_digest"])
        self.assertEqual((row["active_version"], active.manifest.version), ("1.1.0", "1.1.0"))
        self.assertTrue(Path(row["artifact_path"]).is_file())
        self.assertTrue((environment / "waveflow-environment.json").is_file())

    async def test_errors_numeric_update_recovery_and_environment_damage_isolation(self):
        from plugin_runtime import PluginError, PermissionPolicy, PluginRuntime
        await self.install()
        instance = self.runtime.registry.route("nmtv")
        result = await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "nmtv", "resource_id": "262"})
        self.assertEqual(result["url"], STREAM)
        with self.assertRaises(PluginError) as invalid:
            await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "nmtv", "resource_id": "unknown"})
        self.assertEqual(invalid.exception.code, "RESOURCE_NOT_FOUND")
        for mode in ("http_error", "malformed", "missing", "empty"):
            self.mode = mode
            with self.assertRaises(PluginError) as failed:
                await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "nmtv", "resource_id": "nmws"})
            self.assertEqual(failed.exception.code, "TEMPORARY_UPSTREAM_FAILURE")
        self.mode = "success"
        await self.install("1.1.0")
        self.assertEqual(self.runtime.registry.route("nmtv").manifest.version, "1.1.0")
        row = await self.db.get_plugin_installation("org.waveflow", "nmtv")
        env = self.envs.environment_path(self.runtime.registry.route("nmtv").manifest,
                                         self.service._public(row)["runtime"]["lock_digest"])
        await self.runtime.shutdown()
        (env / "waveflow-environment.json").unlink()
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.service = self.pm.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=self.service.trust_policy, python_environments=self.envs, os_name="macos", arch="arm64")
        self.assertEqual(await self.service.recover_enabled(), [{"plugin": IDENTITY, "status": "active"}])
        self.assertTrue((env / "waveflow-environment.json").is_file())
        self.envs.remove_environment(env)
        cache = self.envs.cache_objects()[0]
        cache.write_bytes(b"corrupt")
        await self.runtime.shutdown()
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        broken = self.pm.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=self.service.trust_policy, python_environments=self.envs, os_name="macos", arch="arm64")
        self.service = broken
        recovery = await broken.recover_enabled()
        self.assertEqual(recovery[0]["status"], "unavailable")
        legacy = await self.legacy("nmtv://nmws", self.client)
        self.assertEqual(legacy["url"], STREAM)
        from tests.test_plugin_capability_bridge import FIXTURE
        from plugin_runtime import validate_manifest
        peer_data = json.loads(json.dumps(self.package()["plugin_manifest"]))
        peer_data.update(plugin_id="dependency-isolation-peer",
                         runtime={"type": "subprocess", "ipc": "stdio_framed_json_v1"})
        peer_data["owned_schemes"] = [{"scheme": "dependency-isolation-peer", "contract": "tv_provider"}]
        peer_data["permissions"] = {}
        peer_data["artifacts"][0].update(runtime="native", entrypoint="fixture")
        peer = self.runtime.install(validate_manifest(peer_data), [
            sys.executable, str(FIXTURE), "--scheme", "dependency-isolation-peer", "--tv-only",
            "--identity", "org.waveflow/dependency-isolation-peer", "--version", "1.0.0"])
        await self.runtime.enable(peer)
        self.assertTrue((await self.runtime.request(peer, "tv.resolve_stream", {"scheme": "nmtv", "resource_id": "one"}))["url"].endswith(".m3u8"))

    async def test_legacy_and_plugin_errors_remain_retryable_upstream_failures(self):
        from adapters import AdapterResolveError
        from plugin_runtime import PluginError
        await self.install()
        instance = self.runtime.registry.route("nmtv")
        for mode in ("http_error", "malformed", "missing", "empty"):
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(AdapterResolveError) as legacy:
                    await self.legacy("nmtv://nmws", self.client)
                with self.assertRaises(PluginError) as plugin:
                    await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "nmtv", "resource_id": "nmws"})
                self.assertEqual((legacy.exception.status_code, legacy.exception.retryable), (502, True))
                self.assertEqual((plugin.exception.code, plugin.exception.retryable),
                                 ("TEMPORARY_UPSTREAM_FAILURE", True))

    async def test_market_ownership_guard_and_uninstall_cleanup(self):
        from plugin_capabilities import CapabilityGateway
        from plugin_production import ProductionPluginSubsystem
        from plugin_runtime import PluginError
        from provider_resolver import ProviderResolver
        await self.install()
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy)
        subsystem = ProductionPluginSubsystem(self.service, self.service.trust_policy,
            Path(self.tmp.name) / "downloads", self.client, resolver, CapabilityGateway(client=self.client))
        self.assertEqual(resolver.mode("nmtv"), "legacy")
        await subsystem.set_ownership("nmtv", "plugin", IDENTITY)
        self.assertEqual((await resolver.resolve("nmtv://nmws", self.client))["url"], STREAM)
        with self.assertRaises(PluginError):
            await subsystem.uninstall(IDENTITY)
        await subsystem.set_ownership("nmtv", "legacy")
        manifest = self.runtime.registry.route("nmtv").manifest
        runtime = self.service._public(await self.db.get_plugin_installation("org.waveflow", "nmtv"))["runtime"]
        env = self.envs.environment_path(manifest, runtime["lock_digest"])
        self.assertTrue(await subsystem.uninstall(IDENTITY))
        self.assertFalse(env.exists())
        self.assertEqual(len(self.envs.cache_objects()), 1)

    async def test_same_version_changed_dependency_lock_is_not_idempotent(self):
        await self.install()
        changed = self.package()
        changed["plugin_manifest"]["runtime"]["dependency_lock"]["artifacts"][0]["url"] += "?immutable-conflict=1"
        with self.assertRaises(Exception) as conflict:
            await self.service.install_from_packages([changed], IDENTITY)
        self.assertEqual(conflict.exception.code, "PLUGIN_INCOMPATIBLE")

    async def test_artifact_source_has_no_core_or_network_dependency_import(self):
        source = SOURCE.read_text()
        for forbidden in ("import backend", "import httpx", "import requests", "os.environ", "FastAPI"):
            self.assertNotIn(forbidden, source)
        with self.assertRaises(Exception) as untrusted:
            await self.service.install_from_packages([self.package(trusted=False)], IDENTITY)
        self.assertEqual(untrusted.exception.code, "AUTH_FAILED")


if __name__ == "__main__":
    unittest.main()
