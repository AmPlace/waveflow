from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class PluginProductionTest(unittest.IsolatedAsyncioTestCase):
    async def test_https_download_streams_and_cleans_integrity_failure(self):
        import plugin_production
        download_plugin_artifact = plugin_production.download_plugin_artifact
        payload = b"plugin-payload"
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=transport) as client:
                with mock.patch.object(plugin_production.market, "_validate_fetch_url", mock.AsyncMock(side_effect=lambda url, **_: url)):
                    path = await download_plugin_artifact(
                        "https://plugins.example/artifact", directory,
                        expected_size=len(payload), expected_sha256=hashlib.sha256(payload).hexdigest(), client=client,
                    )
                    self.assertEqual(path.read_bytes(), payload)
                    path.unlink()
                    with self.assertRaises(Exception) as raised:
                        await download_plugin_artifact(
                            "https://plugins.example/artifact?token=secret", directory,
                            expected_size=len(payload), expected_sha256="0" * 64, client=client,
                        )
                    self.assertEqual(raised.exception.code, "ARTIFACT_INTEGRITY_FAILED")
                    self.assertNotIn("secret", str(raised.exception))
                    self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_redirect_is_revalidated_and_http_is_rejected(self):
        import plugin_production
        download_plugin_artifact = plugin_production.download_plugin_artifact
        seen = []
        def handler(request):
            return httpx.Response(302, headers={"location": "https://cdn.example/a"})
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                async def validate(url, **_):
                    seen.append(url)
                    if "cdn.example" in url:
                        raise ValueError("blocked")
                    return url
                with mock.patch.object(plugin_production.market, "_validate_fetch_url", side_effect=validate):
                    with self.assertRaises(Exception):
                        await download_plugin_artifact(
                            "https://plugins.example/a", directory, expected_size=1,
                            expected_sha256="0" * 64, client=client,
                        )
                self.assertEqual(seen, ["https://plugins.example/a", "https://cdn.example/a"])
                with self.assertRaises(Exception) as raised:
                    await download_plugin_artifact("http://plugins.example/a", directory, expected_size=1, expected_sha256="0" * 64)
                self.assertEqual(raised.exception.code, "ARTIFACT_INVALID")

    async def test_dependency_download_reuses_streaming_ssrf_and_integrity_gates(self):
        import plugin_production
        payload = b"wheel-payload"
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(transport=transport) as client:
                with mock.patch.object(plugin_production.market, "_validate_fetch_url", mock.AsyncMock(side_effect=lambda url, **_: url)):
                    path = await plugin_production.download_dependency_artifact(
                        "https://files.pythonhosted.org/dependency.whl", directory,
                        expected_size=len(payload), expected_sha256=hashlib.sha256(payload).hexdigest(), client=client)
                    self.assertEqual(path.read_bytes(), payload)
                    path.unlink()
                    with self.assertRaises(Exception) as integrity:
                        await plugin_production.download_dependency_artifact(
                            "https://files.pythonhosted.org/dependency.whl", directory,
                            expected_size=len(payload), expected_sha256="0" * 64, client=client)
                    self.assertEqual(integrity.exception.code, "DEPENDENCY_ARTIFACT_INTEGRITY_FAILED")

    async def test_persisted_trust_disable_and_rotation(self):
        from plugin_production import ProductionTrustPolicy
        from plugin_runtime.manifest import validate_manifest
        from tests.test_market_plugin_lifecycle import MarketPluginLifecycleTest
        helper = mock.Mock()
        helper.private = Ed25519PrivateKey.generate()
        package = MarketPluginLifecycleTest.package(helper, schemes=("synthetic-production-plugin",))
        manifest = validate_manifest(package["plugin_manifest"])
        payload = Path(package["artifact_references"][0]["local_path"]).read_bytes()
        public = helper.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        row = {"publisher_id": "org.waveflow", "key_id": "fixture-key", "public_key": base64.b64encode(public).decode(), "trust_level": "third_party", "enabled": 1}
        self.assertEqual(ProductionTrustPolicy([row]).verify(manifest, manifest.artifacts[0], payload), "third_party")
        with self.assertRaises(Exception) as disabled:
            ProductionTrustPolicy([{**row, "enabled": 0}]).verify(manifest, manifest.artifacts[0], payload)
        self.assertEqual(disabled.exception.code, "PLUGIN_UNTRUSTED")

    async def test_provider_resolver_defaults_legacy_and_explicit_plugin(self):
        from adapters import _ADAPTER_REGISTRY
        from provider_resolver import ProviderResolver
        legacy = mock.AsyncMock(return_value={"url": "https://legacy.example/a"})
        resolver = ProviderResolver(runtime=None, legacy_resolver=legacy)
        for scheme in _ADAPTER_REGISTRY:
            self.assertEqual(resolver.mode(scheme), "legacy")
        await resolver.resolve("fjtv://one", mock.Mock())
        legacy.assert_awaited_once()
        resolver.set_mode("synthetic-production-plugin", "plugin")
        with self.assertRaises(Exception) as unavailable:
            await resolver.resolve("synthetic-production-plugin://one", mock.Mock())
        self.assertEqual(unavailable.exception.code, "PLUGIN_UNAVAILABLE")

    async def test_synthetic_plugin_resolves_only_after_explicit_cutover(self):
        from plugin_runtime import PluginRuntime, validate_manifest
        from provider_resolver import ProviderResolver
        from tests.test_market_plugin_lifecycle import FIXTURE, MarketPluginLifecycleTest
        helper = mock.Mock()
        helper.private = Ed25519PrivateKey.generate()
        package = MarketPluginLifecycleTest.package(
            helper, schemes=("synthetic-production-plugin",),
        )
        manifest = validate_manifest(package["plugin_manifest"])
        runtime = PluginRuntime()
        instance = runtime.install(manifest, [
            sys.executable, str(FIXTURE), "--identity", manifest.identity,
            "--version", manifest.version, "--scheme", "synthetic-production-plugin", "--tv-only",
        ])
        await runtime.enable(instance)
        legacy = mock.AsyncMock(return_value={"url": "https://legacy.example/a"})
        resolver = ProviderResolver(runtime=runtime, legacy_resolver=legacy)
        try:
            result = await resolver.resolve("synthetic-production-plugin://channel/one", mock.Mock())
            self.assertEqual(result["url"], "https://legacy.example/a")
            resolver.set_mode("synthetic-production-plugin", "plugin")
            result = await resolver.resolve("synthetic-production-plugin://channel/one", mock.Mock())
            self.assertEqual(result["stream_descriptor_version"], "1.0")
            self.assertEqual(result["source_type"], "hls")
            self.assertTrue(result["url"].endswith(".m3u8"))
        finally:
            await runtime.shutdown()

    async def test_capability_gateway_allows_declared_http_and_denies_undeclared(self):
        from plugin_capabilities import CapabilityGateway
        from plugin_runtime.manifest import validate_manifest
        from plugin_runtime.permissions import PermissionGate, PermissionPolicy
        from tests.test_market_plugin_lifecycle import MarketPluginLifecycleTest
        helper = mock.Mock()
        helper.private = Ed25519PrivateKey.generate()
        package = MarketPluginLifecycleTest.package(helper, schemes=("synthetic-production-plugin",))
        package["plugin_manifest"]["permissions"] = {"network": {"allow_private": False}}
        manifest = validate_manifest(package["plugin_manifest"])
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok"))
        async with httpx.AsyncClient(transport=transport) as client:
            gateway = CapabilityGateway(client=client)
            allowed = PermissionGate(manifest, PermissionPolicy(frozenset({"network"})))
            with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
                result = await gateway.managed_http_get(manifest.identity, allowed, "https://api.example/data")
            self.assertEqual(result["body"], b"ok")
            denied = PermissionGate(manifest, PermissionPolicy())
            with self.assertRaises(Exception) as raised:
                await gateway.managed_http_get(manifest.identity, denied, "https://api.example/data")
            self.assertEqual(raised.exception.code, "CAPABILITY_DENIED")


if __name__ == "__main__":
    unittest.main()
