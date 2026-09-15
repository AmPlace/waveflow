from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SOURCE = Path(__file__).parents[1] / "bundled_plugins" / "nowtv" / "plugin.py"
IDENTITY = "org.waveflow/nowtv"
STREAM = "https://media.example/nowtv/live.m3u8?fixture=1"
API_URL = "https://webtvapi.now.com/10/7/getLiveURL"
USER_AGENT = "NNC/6.3.0 (com.now.news; build:2309121224; iOS 17.1.0) Alamofire/5.2.2"


def _clear_modules() -> None:
    for name in ("database", "plugin_market", "plugin_production"):
        sys.modules.pop(name, None)


class NOWTVPluginTest(unittest.IsolatedAsyncioTestCase):
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
        self.artifact = Path(self.tmp.name) / "nowtv-plugin.pyz"
        build_sdk_artifact(SOURCE, self.artifact)
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.requests: list[httpx.Request] = []
        self.mode = "success"

        async def timeout_response(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("fixture timeout", request=request)

        def upstream(request: httpx.Request):
            self.requests.append(request)
            if self.mode == "timeout":
                return timeout_response(request)
            if self.mode == "http_error":
                return httpx.Response(503, text="unavailable")
            if self.mode == "invalid_json":
                return httpx.Response(200, text="not-json")
            values = {
                "business_error": {"responseCode": "FAIL", "asset": [STREAM]},
                "missing_response_code": {"asset": [STREAM]},
                "empty_asset": {"responseCode": "SUCCESS", "asset": []},
                "missing_asset": {"responseCode": "SUCCESS"},
                "malformed_asset": {"responseCode": "SUCCESS", "asset": STREAM},
                "malformed_url": {"responseCode": "SUCCESS", "asset": [123]},
            }
            return httpx.Response(200, json=values.get(self.mode, {"responseCode": "SUCCESS", "asset": [STREAM]}))

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.runtime = PluginRuntime(
            permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)),
        )
        self.store = plugin_market.PluginArtifactStore(Path(self.tmp.name) / "store", allowed_local_roots=[self.artifact.parent])
        self.service = plugin_market.PluginMarketService(
            runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "nowtv-test-key"): public}),
            command_factory=lambda manifest, artifact: (
                sys.executable, str(artifact), "--identity", manifest.identity, "--version", manifest.version,
            ), os_name="linux", arch="x86_64",
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
            "manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "nowtv",
            "display_name": "NOW TV Provider", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": "nowtv", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"],
            "permissions": {"network": {"managed": managed_network, "allowed_hosts": ["webtvapi.now.com"]}},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "python", "entrypoint": "plugin.pyz",
                "sha256": digest, "size_bytes": len(payload), "signature": {"algorithm": "ed25519",
                "key_id": "nowtv-test-key", "value": base64.b64encode(signature).decode()}}],
            "dependencies": [], "state_schema_version": 1,
        }
        return {"schema_version": 1, "id": "official::nowtv-plugin", "original_id": "nowtv-plugin",
            "name": "NOW TV Plugin", "kind": "plugin_package", "package_type": "plugin_package",
            "version": version, "plugin_manifest": manifest,
            "artifact_references": [{"sha256": digest, "local_path": str(self.artifact)}],
            "market_source": {"source_key": "official"}}

    async def install(self, version="1.0.0"):
        return await self.service.install_from_packages([self.package(version)], IDENTITY)

    @staticmethod
    async def legacy_resolver(target: str, client: httpx.AsyncClient):
        from adapters import parse_adapter_url
        from adapters.nowtv import resolve_nowtv
        return await resolve_nowtv(parse_adapter_url(target), client)

    async def test_signed_install_exact_json_post_and_dual_track_golden(self):
        from provider_resolver import ProviderResolver, parse_tv_reference

        installed = await self.install()
        self.assertEqual((installed["active_version"], installed["trust_state"]), ("1.0.0", "fixture_trusted"))
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy_resolver)
        self.assertEqual(resolver.mode("nowtv"), "legacy")
        direct = parse_tv_reference("nowtv://NEWS")
        compat = parse_tv_reference("adapter://nowtv/NEWS")
        self.assertEqual((direct.scheme, direct.resource_id), (compat.scheme, compat.resource_id))
        legacy = await resolver.resolve("nowtv://NEWS", self.client)
        resolver.set_mode("nowtv", "plugin", IDENTITY)
        plugin = await resolver.resolve("adapter://nowtv/NEWS", self.client)
        for key in ("url", "source_type", "headers", "requires_proxy", "ttl", "expires_at", "volatile_url"):
            self.assertEqual(plugin[key], legacy[key], key)
        self.assertFalse(plugin["direct_playable"])
        self.assertEqual(plugin["stream_descriptor_version"], "1.0")
        request = self.requests[-1]
        self.assertEqual((request.method, str(request.url)), ("POST", API_URL))
        self.assertEqual(request.headers["user-agent"], USER_AGENT)
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(json.loads(request.content), {
            "deviceType": "IOS_PHONE", "contentId": "331", "audioCode": "A",
            "deviceId": "8269809F-7702-45CE-9378-D7157A2E6819", "mode": "prod",
            "callerReferenceNo": "20140702122500", "contentType": "Channel",
        })
        resolver.set_mode("nowtv", "legacy")
        self.assertNotIn("stream_descriptor_version", await resolver.resolve("nowtv://NEWS", self.client))

    async def test_alias_numeric_mapping_response_validation_and_stable_errors(self):
        from plugin_runtime import PluginError

        await self.install()
        instance = self.runtime.registry.route("nowtv")
        for resource, content_id in (("NEWS", "331"), ("finance", "332"), ("LIVE", "333"), ("999", "999")):
            result = await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "nowtv", "resource_id": resource})
            self.assertEqual((result["url"], result["requires_proxy"], result["ttl_seconds"]), (STREAM, True, 300))
            self.assertEqual(json.loads(self.requests[-1].content)["contentId"], content_id)
        with self.assertRaises(PluginError) as unknown:
            await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "nowtv", "resource_id": "unknown"})
        self.assertEqual(unknown.exception.code, "RESOURCE_NOT_FOUND")
        for mode in ("business_error", "missing_response_code", "empty_asset", "missing_asset",
                     "malformed_asset", "malformed_url", "invalid_json", "http_error", "timeout"):
            self.mode = mode
            with self.assertRaises(PluginError) as raised:
                await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "nowtv", "resource_id": "NEWS"})
            self.assertIn(raised.exception.code, {"TEMPORARY_UPSTREAM_FAILURE", "PLUGIN_TIMEOUT"}, mode)

    async def test_legacy_and_plugin_failure_semantics_are_retryable_upstream_errors(self):
        from adapters import AdapterResolveError
        from plugin_runtime import PluginError

        await self.install()
        instance = self.runtime.registry.route("nowtv")
        for mode in ("business_error", "missing_response_code", "empty_asset", "missing_asset",
                     "invalid_json", "http_error", "timeout"):
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(AdapterResolveError) as legacy:
                    await self.legacy_resolver("nowtv://NEWS", self.client)
                with self.assertRaises(PluginError) as plugin:
                    await self.runtime.request(instance, "tv.resolve_stream", {"scheme": "nowtv", "resource_id": "NEWS"})
                self.assertTrue(legacy.exception.retryable)
                self.assertEqual(legacy.exception.status_code, 502)
                self.assertTrue(plugin.exception.retryable)
                self.assertIn(plugin.exception.code, {"TEMPORARY_UPSTREAM_FAILURE", "PLUGIN_TIMEOUT"})

    async def test_permission_market_projection_dependency_and_source_purity(self):
        from market import _normalize_package, _package_card
        from plugin_runtime import PluginError

        card = _package_card(_normalize_package(self.package()))
        self.assertEqual(card["plugin"]["publisher_id"], "org.waveflow")
        self.assertEqual(card["plugin"]["owned_schemes"], [{"scheme": "nowtv", "contract": "tv_provider"}])
        self.assertEqual(card["plugin"]["permissions"], ["network.managed"])
        self.assertNotIn("artifact_references", card)
        await self.service.install_from_packages([self.package(managed_network=False)], IDENTITY)
        with self.assertRaises(PluginError) as denied:
            await self.runtime.request(self.runtime.registry.route("nowtv"), "tv.resolve_stream", {"scheme": "nowtv", "resource_id": "NEWS"})
        self.assertEqual(denied.exception.code, "CAPABILITY_DENIED")
        source = SOURCE.read_text(encoding="utf-8")
        for forbidden in ("import backend", "import httpx", "import requests", "os.environ", "FastAPI"):
            self.assertNotIn(forbidden, source)

    async def test_content_dependency_update_failure_and_recovery(self):
        requirement = {"plugin": IDENTITY, "version_range": ">=1.0.0 <2.0.0",
                       "contract": "tv_provider", "required_schemes": ["nowtv"]}
        await self.db.install_market_package_atomic(
            package_id="nowtv-content-fixture", market_url="fixture://market",
            title="NOW TV Content Fixture", subscription_url="market://nowtv-content-fixture",
            channels=[{"name": "NOW 新闻台", "url": "nowtv://NEWS"}], installed_version="1.0.0",
            metadata_json=json.dumps({"requires_plugins": [requirement]}),
        )
        projection = self.service.installed_content_dependency_projection
        self.assertEqual((await projection("nowtv-content-fixture"))["status"], "dependency_missing")
        await self.install("1.0.0")
        self.assertEqual((await projection("nowtv-content-fixture"))["status"], "ready")
        await self.install("1.1.0")
        self.assertEqual(self.runtime.registry.route("nowtv").manifest.version, "1.1.0")
        factory = self.service.command_factory
        self.service.command_factory = lambda manifest, artifact: (
            sys.executable, str(artifact), "--identity", manifest.identity, "--version", "9.9.9")
        with self.assertRaises(Exception):
            await self.service.install_from_packages([self.package("1.2.0")], IDENTITY)
        self.assertEqual(self.runtime.registry.route("nowtv").manifest.version, "1.1.0")
        self.service.command_factory = factory
        with self.assertRaises(Exception):
            await self.service.install_from_packages([self.package("1.2.0", trusted=False)], IDENTITY)
        self.assertEqual(self.runtime.registry.route("nowtv").manifest.version, "1.1.0")

        await self.runtime.shutdown()
        from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
        from plugin_runtime import PermissionPolicy, PluginRuntime
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.service = self.pm.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=self.service.trust_policy, command_factory=factory, os_name="linux", arch="x86_64")
        self.assertEqual(await self.service.recover_enabled(), [{"plugin": IDENTITY, "status": "active"}])
        await self.service.disable(IDENTITY)
        self.assertEqual((await projection("nowtv-content-fixture"))["status"], "provider_unavailable")
        await self.service.enable(IDENTITY)
        # The Content fixture still declares this Plugin: V1 refuses the
        # uninstall and reports the dependent instead of cascading.
        with self.assertRaises(self.pm.PluginError) as blocked:
            await self.service.uninstall(IDENTITY)
        self.assertEqual(blocked.exception.code, "PLUGIN_DEPENDENCY_ACTIVE")
        self.assertEqual(blocked.exception.details["dependents"], ["nowtv-content-fixture"])
        self.assertTrue(await self.service.uninstall(IDENTITY, force=True))
        self.assertEqual((await projection("nowtv-content-fixture"))["status"], "dependency_missing")

    async def test_production_ownership_guard_persistence_and_rollback(self):
        from plugin_capabilities import CapabilityGateway
        from plugin_production import ProductionPluginSubsystem
        from plugin_runtime import PluginError
        from provider_resolver import ProviderResolver

        await self.install()
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy_resolver)
        subsystem = ProductionPluginSubsystem(service=self.service, trust_policy=self.service.trust_policy,
            download_root=Path(self.tmp.name) / "downloads", http_client=self.client,
            provider_resolver=resolver, capability_gateway=CapabilityGateway(client=self.client))
        self.assertEqual(resolver.mode("nowtv"), "legacy")
        await subsystem.set_ownership("nowtv", "plugin", IDENTITY)
        result = await resolver.resolve("nowtv://LIVE", self.client)
        self.assertEqual((result["url"], result["requires_proxy"], result["direct_playable"]), (STREAM, True, False))
        for operation in (subsystem.disable, subsystem.uninstall):
            with self.assertRaises(PluginError) as blocked:
                await operation(IDENTITY)
            self.assertEqual(blocked.exception.code, "SCHEME_CONFLICT")
        rows = await self.db.list_plugin_scheme_ownership()
        self.assertEqual(next(row for row in rows if row["scheme"] == "nowtv")["mode"], "plugin")

        await self.runtime.shutdown()
        from plugin_capabilities import CoreCapabilityDispatcher
        from plugin_runtime import PermissionPolicy, PluginRuntime
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})),
            capability_dispatcher=CoreCapabilityDispatcher(CapabilityGateway(client=self.client)))
        self.service = self.pm.PluginMarketService(runtime=self.runtime, store=self.store,
            trust_policy=self.service.trust_policy, command_factory=self.service.command_factory,
            os_name="linux", arch="x86_64")
        self.assertEqual(await self.service.recover_enabled(), [{"plugin": IDENTITY, "status": "active"}])
        rows = await self.db.list_plugin_scheme_ownership()
        restarted = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy_resolver,
            ownership={row["scheme"]: row["mode"] for row in rows})
        for row in rows:
            restarted.set_mode(row["scheme"], row["mode"], row.get("plugin_identity") or "")
        resolved = await restarted.resolve("nowtv://NEWS", self.client)
        self.assertEqual((resolved["stream_descriptor_version"], resolved["requires_proxy"]), ("1.0", True))
        subsystem = ProductionPluginSubsystem(service=self.service, trust_policy=self.service.trust_policy,
            download_root=Path(self.tmp.name) / "downloads", http_client=self.client,
            provider_resolver=restarted, capability_gateway=CapabilityGateway(client=self.client))
        await subsystem.set_ownership("nowtv", "legacy")
        await subsystem.disable(IDENTITY)
        await self.service.enable(IDENTITY)
        self.assertTrue(await subsystem.uninstall(IDENTITY))

    async def test_production_provider_result_enters_core_media_proxy_boundary(self):
        from provider_resolver import ProviderResolver
        from routers import media_proxy
        from security.dependencies import MediaAccessContext

        await self.install()
        resolver = ProviderResolver(runtime=self.runtime, legacy_resolver=self.legacy_resolver)
        resolver.set_mode("nowtv", "plugin", IDENTITY)
        captured = {}

        async def serve_resolved(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(status_code=200)

        source = {"url": "nowtv://NEWS", "source_type": "adapter", "enabled": True,
                  "source_id": "src_nowtv"}
        fake_main = types.SimpleNamespace(
            _source_type=lambda value: value.get("source_type") or "hls",
            resolve_adapter_source=self.legacy_resolver,
            http_client=self.client,
            AdapterResolveError=__import__("adapters").AdapterResolveError,
            app=types.SimpleNamespace(state=types.SimpleNamespace(provider_resolver=resolver)),
        )
        old_main = sys.modules.get("main")
        old_serve = media_proxy._serve_resolved_source_playlist
        try:
            sys.modules["main"] = fake_main
            media_proxy._serve_resolved_source_playlist = serve_resolved
            response = await media_proxy._serve_iptv_source_playlist(
                source, "NOW 新闻台", MediaAccessContext(source="anonymous"),
            )
        finally:
            media_proxy._serve_resolved_source_playlist = old_serve
            if old_main is None:
                sys.modules.pop("main", None)
            else:
                sys.modules["main"] = old_main
        self.assertEqual(response.status_code, 200)
        self.assertEqual((captured["resolved_url"], captured["resolved_st"], captured["source_id"]),
                         (STREAM, "hls", "src_nowtv"))
        self.assertEqual((captured["custom_ua"], captured["referer"], captured["cookie"]), ("", "", ""))


if __name__ == "__main__":
    unittest.main()
