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

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SOURCE = Path(__file__).parents[1] / "bundled_plugins" / "fjtv" / "plugin.py"
IDENTITY = "org.waveflow/fjtv"
STREAM = "https://live.example/fjtv/live.m3u8?token=fixture"


def _clear_modules() -> None:
    for name in ("database", "plugin_market", "plugin_production"):
        sys.modules.pop(name, None)


class FJTVPluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        _clear_modules()
        import database
        import plugin_market
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_runtime import PermissionPolicy, PluginRuntime
        from waveflow_plugin_cli import build_sdk_artifact

        self.db, self.pm = database, plugin_market
        await self.db.initialize()
        self.artifact = Path(self.tmp.name) / "fjtv-plugin.pyz"
        build_sdk_artifact(SOURCE, self.artifact)
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.requests: list[httpx.Request] = []
        self.mode = "success"

        def upstream(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.mode == "timeout":
                raise httpx.ReadTimeout("fixture timeout", request=request)
            if self.mode == "error":
                return httpx.Response(503, text="unavailable")
            if self.mode == "malformed_json":
                return httpx.Response(200, text="not json")
            if self.mode == "business_error":
                return httpx.Response(200, json={"error_code": 1})
            if self.mode == "not_live":
                return httpx.Response(200, json=[{"m3u8": ""}])
            host = request.url.host
            if host == "live.fjtv.net":
                return httpx.Response(200, json=[{"m3u8": STREAM}])
            if host == "mapi-plus.fjtv.net":
                return httpx.Response(200, json=[{"topic_camera": [{"streams": [{"hls": STREAM}]}]} for _ in range(10)])
            if host == "mapi1.kxm.xmtv.cn":
                return httpx.Response(200, json=[{"m3u8": STREAM} for _ in range(4)])
            return httpx.Response(200, json={"topic_camera": [{"streams": [{"hls": STREAM}]}]})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        gateway = CapabilityGateway(client=self.client)
        self.runtime = PluginRuntime(
            permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(gateway),
        )
        self.store = plugin_market.PluginArtifactStore(
            Path(self.tmp.name) / "store", allowed_local_roots=[self.artifact.parent]
        )
        self.service = plugin_market.PluginMarketService(
            runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "fjtv-test-key"): public}),
            command_factory=lambda manifest, artifact: (
                sys.executable, str(artifact), "--identity", manifest.identity, "--version", manifest.version,
            ),
            os_name="linux", arch="x86_64",
        )
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

    def package(self, version="1.0.0", *, trusted=True, managed_network=True) -> dict:
        payload = self.artifact.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        signature = self.private.sign(payload) if trusted else b"invalid"
        manifest = {
            "manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "fjtv",
            "display_name": "FJTV Provider", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "fjtv", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"],
            "permissions": {"network": {"managed": managed_network, "allowed_hosts": [
                "live.fjtv.net", "mapi-plus.fjtv.net", "mapi1.kxm.xmtv.cn",
                "mapi.ijjnews.com", "mapi-new.chinashishi.net",
            ]}},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "python",
                           "entrypoint": "plugin.pyz", "sha256": digest, "size_bytes": len(payload),
                           "signature": {"algorithm": "ed25519", "key_id": "fjtv-test-key",
                                         "value": base64.b64encode(signature).decode()}}],
            "dependencies": [], "state_schema_version": 1,
        }
        return {"schema_version": 1, "id": "official::fjtv-plugin", "original_id": "fjtv-plugin",
                "name": "FJTV Plugin", "kind": "plugin_package", "package_type": "plugin_package",
                "version": version, "plugin_manifest": manifest,
                "artifact_references": [{"sha256": digest, "local_path": str(self.artifact)}],
                "market_source": {"source_key": "official"}}

    async def install(self, version="1.0.0"):
        return await self.service.install_from_packages([self.package(version)], IDENTITY)

    @staticmethod
    async def legacy_resolver(target: str, client: httpx.AsyncClient):
        from adapters import parse_adapter_url
        from adapters.fjtv import resolve_fjtv
        return await resolve_fjtv(parse_adapter_url(target), client)

    async def test_manifest_artifact_trust_install_and_explicit_dual_track_cutover(self):
        from provider_resolver import ProviderResolver, parse_tv_reference
        installed = await self.install()
        self.assertEqual((installed["active_version"], installed["trust_state"]), ("1.0.0", "fixture_trusted"))
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy_resolver)
        self.assertEqual(resolver.mode("fjtv"), "legacy")
        direct = parse_tv_reference("fjtv://fjzhpd?quality=best")
        compat = parse_tv_reference("adapter://fjtv/fjzhpd?quality=best")
        self.assertEqual((direct.scheme, direct.resource_id, direct.query),
                         (compat.scheme, compat.resource_id, compat.query))

        legacy = await resolver.resolve("fjtv://fjzhpd", self.client)
        resolver.set_mode("fjtv", "plugin", IDENTITY)
        plugin = await resolver.resolve("adapter://fjtv/fjzhpd", self.client)
        for key in ("url", "source_type", "headers", "requires_proxy", "ttl", "expires_at", "volatile_url"):
            self.assertEqual(plugin[key], legacy[key], key)
        self.assertEqual(plugin["stream_descriptor_version"], "1.0")
        request = next(request for request in reversed(self.requests) if request.url.host == "live.fjtv.net")
        self.assertEqual((request.method, request.headers["user-agent"]), ("GET", "okhttp/3.10.0.7"))
        self.assertEqual(request.url.params["channel_id"], "665248990102917120")

        resolver.set_mode("fjtv", "legacy")
        rolled_back = await resolver.resolve("fjtv://fjzh", self.client)
        self.assertNotIn("stream_descriptor_version", rolled_back)

        from market import _normalize_package, _package_card
        card = _package_card(_normalize_package(self.package()))
        self.assertEqual(card["plugin"]["publisher_id"], "org.waveflow")
        self.assertEqual(card["plugin"]["owned_schemes"], [{"scheme": "fjtv", "contract": "tv_provider"}])
        self.assertEqual(card["plugin"]["permissions"], ["network.managed"])
        self.assertNotIn("artifact_references", card)

    async def test_all_provider_shapes_referers_and_stable_errors(self):
        from plugin_runtime import PluginError
        try:
            instance = self.runtime.registry.route("fjtv")
        except PluginError:
            await self.install(); instance = self.runtime.registry.route("fjtv")
        expected = {"xmws": "https://www.fjtv.net/", "xmtv-1": "https://www.xmtv.cn/",
                    "jjtv": "https://www.ijjnews.com/", "sstv": "https://www.chinashishi.net/"}
        for resource, referer in expected.items():
            result = await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "fjtv", "resource_id": resource})
            self.assertEqual((result["url"], result["headers"]["Referer"]), (STREAM, referer))
        for mode, codes in (("not_live", {"NOT_LIVE"}), ("error", {"TEMPORARY_UPSTREAM_FAILURE"}),
                           ("timeout", {"TEMPORARY_UPSTREAM_FAILURE", "PLUGIN_TIMEOUT"}),
                           ("malformed_json", "TEMPORARY_UPSTREAM_FAILURE"),
                           ("business_error", "TEMPORARY_UPSTREAM_FAILURE")):
            self.mode = mode
            with self.assertRaises(PluginError) as raised:
                await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "fjtv", "resource_id": "fjzh"})
            expected_codes = codes if isinstance(codes, set) else {codes}
            self.assertIn(raised.exception.code, expected_codes)
        self.mode = "success"
        with self.assertRaises(PluginError) as missing:
            await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "fjtv", "resource_id": "unknown"})
        self.assertEqual(missing.exception.code, "RESOURCE_NOT_FOUND")

    async def test_managed_network_permission_is_required(self):
        from plugin_runtime import PluginError
        await self.service.install_from_packages([self.package(managed_network=False)], IDENTITY)
        instance = self.runtime.registry.route("fjtv")
        with self.assertRaises(PluginError) as denied:
            await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "fjtv", "resource_id": "fjzh"})
        self.assertEqual(denied.exception.code, "CAPABILITY_DENIED")

    async def test_dependency_update_failure_recovery_disable_and_uninstall(self):
        requirement = {"plugin": IDENTITY, "version_range": ">=1.0.0 <2.0.0",
                       "contract": "tv_provider", "required_schemes": ["fjtv"]}
        self.assertEqual((await self.service.dependency_projection([requirement]))["status"], "dependency_missing")
        await self.install("1.0.0")
        self.assertEqual((await self.service.dependency_projection([requirement]))["status"], "ready")
        await self.install("1.1.0")
        self.assertEqual(self.runtime.registry.route("fjtv").manifest.version, "1.1.0")
        original_factory = self.service.command_factory
        self.service.command_factory = lambda manifest, artifact: (
            sys.executable, str(artifact), "--identity", manifest.identity, "--version", "9.9.9",
        )
        with self.assertRaises(Exception) as bad_candidate:
            await self.service.install_from_packages([self.package("1.2.0")], IDENTITY)
        self.assertIn(bad_candidate.exception.code, {"PLUGIN_INCOMPATIBLE", "INVALID_PLUGIN_RESPONSE"})
        self.assertEqual(self.runtime.registry.route("fjtv").manifest.version, "1.1.0")
        self.service.command_factory = original_factory
        untrusted = self.package("1.2.0", trusted=False)
        with self.assertRaises(Exception):
            await self.service.install_from_packages([untrusted], IDENTITY)
        self.assertEqual(self.runtime.registry.route("fjtv").manifest.version, "1.1.0")

        await self.runtime.shutdown()
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_runtime import PermissionPolicy, PluginRuntime
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
                                     capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        recovered = self.pm.PluginMarketService(
            runtime=self.runtime, store=self.store, trust_policy=self.service.trust_policy,
            command_factory=self.service.command_factory, os_name="linux", arch="x86_64")
        self.service = recovered
        self.assertEqual(await recovered.recover_enabled(), [{"plugin": IDENTITY, "status": "active"}])
        self.assertEqual(self.runtime.registry.route("fjtv").manifest.version, "1.1.0")
        await recovered.disable(IDENTITY)
        self.assertEqual((await recovered.dependency_projection([requirement]))["status"], "provider_unavailable")
        await recovered.enable(IDENTITY)
        self.assertTrue(await recovered.uninstall(IDENTITY))
        self.assertEqual((await recovered.dependency_projection([requirement]))["status"], "dependency_missing")

    async def test_production_ownership_guards_and_persisted_restart_routing(self):
        from plugin_capabilities import CapabilityGateway
        from plugin_production import ProductionPluginSubsystem
        from plugin_runtime import PluginError
        from provider_resolver import ProviderResolver

        await self.install()
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy_resolver)
        subsystem = ProductionPluginSubsystem(
            service=self.service, trust_policy=self.service.trust_policy,
            download_root=Path(self.tmp.name) / "downloads", http_client=self.client,
            provider_resolver=resolver, capability_gateway=CapabilityGateway(client=self.client),
        )
        self.assertEqual(resolver.mode("fjtv"), "legacy")
        switched = await subsystem.set_ownership("fjtv", "plugin", IDENTITY)
        self.assertEqual(switched, {"scheme": "fjtv", "mode": "plugin", "plugin": IDENTITY})
        result = await resolver.resolve("fjtv://fjdnws", self.client)
        self.assertEqual((result["url"], result["stream_descriptor_version"]), (STREAM, "1.0"))
        for operation in (subsystem.disable, subsystem.uninstall):
            with self.assertRaises(PluginError) as blocked:
                await operation(IDENTITY)
            self.assertEqual(blocked.exception.code, "SCHEME_CONFLICT")

        await self.runtime.shutdown()
        from plugin_capabilities import CoreCapabilityDispatcher
        from plugin_runtime import PermissionPolicy, PluginRuntime
        self.runtime = PluginRuntime(
            permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)),
        )
        recovered = self.pm.PluginMarketService(
            runtime=self.runtime, store=self.store, trust_policy=self.service.trust_policy,
            command_factory=self.service.command_factory, os_name="linux", arch="x86_64",
        )
        self.service = recovered
        self.assertEqual(await recovered.recover_enabled(), [{"plugin": IDENTITY, "status": "active"}])
        ownership = await self.db.list_plugin_scheme_ownership()
        restarted_resolver = ProviderResolver(
            runtime=self.runtime, legacy_resolver=self.legacy_resolver,
            ownership={row["scheme"]: row["mode"] for row in ownership},
        )
        for row in ownership:
            restarted_resolver.set_mode(row["scheme"], row["mode"], row.get("plugin_identity") or "")
        restarted = await restarted_resolver.resolve("fjtv://fjdn", self.client)
        self.assertEqual(restarted["stream_descriptor_version"], "1.0")

        subsystem = ProductionPluginSubsystem(
            service=recovered, trust_policy=recovered.trust_policy,
            download_root=Path(self.tmp.name) / "downloads", http_client=self.client,
            provider_resolver=restarted_resolver, capability_gateway=CapabilityGateway(client=self.client),
        )
        await subsystem.set_ownership("fjtv", "legacy")
        await subsystem.disable(IDENTITY)
        await recovered.enable(IDENTITY)
        self.assertTrue(await subsystem.uninstall(IDENTITY))

    async def test_content_dependency_fixture_and_plugin_crash_isolation(self):
        from plugin_runtime import PluginError, validate_manifest
        from tests.test_plugin_capability_bridge import FIXTURE

        requirement = {"plugin": IDENTITY, "version_range": ">=1.0.0 <2.0.0",
                       "contract": "tv_provider", "required_schemes": ["fjtv"]}
        await self.db.install_market_package_atomic(
            package_id="fjtv-content-fixture", market_url="fixture://market",
            title="FJTV Content Fixture", subscription_url="market://fjtv-content-fixture",
            channels=[{"name": "福建综合", "url": "fjtv://fjzh"}], installed_version="1.0.0",
            metadata_json=json.dumps({"requires_plugins": [requirement]}),
        )
        projection = self.service.installed_content_dependency_projection
        self.assertEqual((await projection("fjtv-content-fixture"))["status"], "dependency_missing")
        await self.install()
        self.assertEqual((await projection("fjtv-content-fixture"))["status"], "ready")
        other_manifest = validate_manifest({
            "manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "fjtv-isolation-peer",
            "display_name": "Isolation Peer", "version": "1.0.0", "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "fjtv-isolation-peer", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"], "permissions": {},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "native", "entrypoint": "fixture",
                           "sha256": "0" * 64, "size_bytes": 1,
                           "signature": {"algorithm": "ed25519", "key_id": "fixture", "value": "fixture"}}],
            "dependencies": [], "state_schema_version": 1,
        })
        peer = self.runtime.install(other_manifest, [
            sys.executable, str(FIXTURE), "--scheme", "fjtv-isolation-peer", "--tv-only",
            "--identity", other_manifest.identity, "--version", other_manifest.version,
        ])
        await self.runtime.enable(peer)
        instance = self.runtime.registry.route("fjtv")
        assert instance.process and instance.process.process
        instance.process.process.kill()
        await instance.process.process.wait()
        with self.assertRaises(PluginError) as crashed:
            await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "fjtv", "resource_id": "fjzh"})
        self.assertIn(crashed.exception.code, {"PLUGIN_CRASHED", "PLUGIN_UNAVAILABLE"})
        peer_result = await self.runtime.request(
            peer, "tv.resolve_stream", {"scheme": "fjtv", "resource_id": "channel/one"},
        )
        self.assertTrue(peer_result["url"].endswith(".m3u8"))
        legacy = await self.legacy_resolver("fjtv://fjzh", self.client)
        self.assertEqual(legacy["url"], STREAM)

    async def test_untrusted_package_is_rejected_and_source_has_no_core_imports(self):
        with self.assertRaises(Exception) as raised:
            await self.service.install_from_packages([self.package(trusted=False)], IDENTITY)
        self.assertEqual(raised.exception.code, "AUTH_FAILED")
        source = SOURCE.read_text(encoding="utf-8")
        for forbidden in ("import backend", "import httpx", "import requests", "os.environ", "FastAPI"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
