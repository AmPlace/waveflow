from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from plugin_capabilities import CapabilityGateway, CoreCapabilityDispatcher
from plugin_dependencies import DependencyArtifact, DependencyArtifactCache, PluginEnvironmentPlan, PythonRuntimeSpec
from plugin_runtime import LifecycleState, PermissionPolicy, PluginError, PluginRuntime, validate_manifest
from plugin_runtime.permissions import PermissionGate
from ssrf_guard import UnsafeTargetError

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_plugin.py"


def manifest_data(*, scheme="bridge", permissions=None):
    return {
        "manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "synthetic",
        "display_name": "Synthetic", "version": "1.0.0", "plugin_api_version": "1.0",
        "core_version_range": ">=0.1.0 <1.0.0",
        "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
        "owned_schemes": [{"scheme": scheme, "contract": "tv_provider"}],
        "capabilities": ["tv.resolve_stream"], "permissions": permissions or {},
        "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
        "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "native", "entrypoint": "synthetic",
                       "sha256": "0" * 64, "size_bytes": 1,
                       "signature": {"algorithm": "ed25519", "key_id": "fixture", "value": "fixture"}}],
        "dependencies": [], "state_schema_version": 1,
    }


def command(mode: str, scheme: str = "bridge", permissions: str = "network,cache") -> list[str]:
    return [sys.executable, str(FIXTURE), "--mode", mode, "--scheme", scheme,
            "--permissions", permissions, "--tv-only"]


class CapabilityBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"url": "https://cdn.example/live.m3u8"})
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.gateway = CapabilityGateway(client=self.client)
        self.dispatcher = CoreCapabilityDispatcher(self.gateway)
        self.runtime = PluginRuntime(
            permission_policy=PermissionPolicy(frozenset({"network", "cache"})),
            capability_dispatcher=self.dispatcher,
        )

    async def asyncTearDown(self):
        await self.runtime.shutdown()
        await self.client.aclose()

    async def active(self, mode="nested_http", permissions=None):
        manifest = validate_manifest(manifest_data(
            scheme="bridge", permissions=permissions or {"network": {"managed": True}, "cache": {}}
        ))
        instance = self.runtime.install(
            manifest, command(mode, permissions=",".join((permissions or {"network": {}, "cache": {}}).keys())),
            instance_id="bridge-instance",
        )
        await self.runtime.enable(instance)
        return instance

    async def test_nested_provider_http_returns_descriptor(self):
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
            instance = await self.active()
            result = await self.runtime.request(instance, "tv.resolve_stream", {"fixture_url": "https://api.example/resolve"})
        self.assertEqual(result["url"], "https://cdn.example/live.m3u8")
        self.assertEqual(instance.state, LifecycleState.HEALTHY_ACTIVE)

    async def test_concurrent_nested_provider_requests_are_correlated(self):
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
            instance = await self.active()
            results = await asyncio.gather(*(
                self.runtime.request(instance, "tv.resolve_stream", {"fixture_url": "https://api.example/resolve"})
                for _ in range(4)
            ))
        self.assertEqual({item["url"] for item in results}, {"https://cdn.example/live.m3u8"})

    async def test_managed_http_redirect_and_response_limit_are_enforced(self):
        responses = [
            httpx.Response(302, headers={"location": "https://api.example/next"}),
            httpx.Response(200, text="ok"),
        ]
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: responses.pop(0)))
        gateway = CapabilityGateway(client=client, max_response_bytes=2)
        manifest = validate_manifest(manifest_data(permissions={"network": {"managed": True}}))
        gate = PermissionGate(manifest, PermissionPolicy(frozenset({"network"})))
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
            result = await gateway.managed_http_fetch(
                manifest.identity, gate, {"method": "GET", "url": "https://api.example/start"}, timeout=1
            )
        self.assertEqual(result["body"], "ok")
        too_large = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="long")))
        gateway.client = too_large
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
            with self.assertRaises(PluginError) as raised:
                await gateway.managed_http_fetch(
                    manifest.identity, gate, {"method": "GET", "url": "https://api.example/start"}, timeout=1
                )
        self.assertEqual(raised.exception.code, "INVALID_CAPABILITY_REQUEST")
        await client.aclose(); await too_large.aclose()

    async def test_managed_http_preserves_url_query_when_no_query_override_is_supplied(self):
        seen = []
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: (seen.append(request), httpx.Response(200, text="ok"))[1]
        ))
        gateway = CapabilityGateway(client=client)
        manifest = validate_manifest(manifest_data(permissions={"network": {"managed": True}}))
        gate = PermissionGate(manifest, PermissionPolicy(frozenset({"network"})))
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
            await gateway.managed_http_fetch(
                manifest.identity, gate,
                {"method": "GET", "url": "https://api.example/data?channel_id=123"}, timeout=1,
            )
        self.assertEqual(seen[0].url.params["channel_id"], "123")
        await client.aclose()

    async def test_managed_http_host_redirect_ssrf_timeout_and_malformed_request(self):
        manifest = validate_manifest(manifest_data(permissions={
            "network": {"managed": True, "allowed_hosts": ["api.example"]}
        }))
        gate = PermissionGate(manifest, PermissionPolicy(frozenset({"network"})))
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
            with self.assertRaises(PluginError) as denied:
                await self.gateway.managed_http_fetch(
                    manifest.identity, gate, {"url": "https://other.example/data"}, timeout=1
                )
            with self.assertRaises(PluginError) as malformed:
                await self.gateway.managed_http_fetch(
                    manifest.identity, gate, {"method": "DELETE", "url": "https://api.example/data"}, timeout=1
                )
        self.assertEqual(denied.exception.code, "CAPABILITY_DENIED")
        self.assertEqual(malformed.exception.code, "INVALID_CAPABILITY_REQUEST")

        redirect = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "https://metadata.invalid/secret"})
        ))
        self.gateway.client = redirect
        safety = mock.AsyncMock(side_effect=[None, UnsafeTargetError("blocked")])
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=safety):
            with self.assertRaises(PluginError) as ssrf:
                await self.gateway.managed_http_fetch(
                    manifest.identity, gate, {"url": "https://api.example/start"}, timeout=1
                )
        self.assertEqual(ssrf.exception.code, "CAPABILITY_DENIED")
        await redirect.aclose()

        timeout_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("timeout", request=request))
        ))
        self.gateway.client = timeout_client
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
            with self.assertRaises(PluginError) as timeout:
                await self.gateway.managed_http_fetch(
                    manifest.identity, gate, {"url": "https://api.example/start"}, timeout=0.01
                )
        self.assertEqual(timeout.exception.code, "PLUGIN_TIMEOUT")
        await timeout_client.aclose()

    async def test_capability_permission_denied_is_stable(self):
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
            instance = await self.active(permissions={"cache": {}})
            with self.assertRaises(PluginError) as raised:
                await self.runtime.request(instance, "tv.resolve_stream", {})
        self.assertEqual(raised.exception.code, "CAPABILITY_DENIED")

    async def test_plugin_crash_cancels_nested_capability_and_runtime_stays_usable(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return httpx.Response(200, json={"url": "https://cdn.example/live.m3u8"})

        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        self.gateway.client = self.client
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
            instance = await self.active("nested_crash")
            request = asyncio.create_task(self.runtime.request(instance, "tv.resolve_stream", {}, timeout=2))
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(instance.process.process.wait(), 1)
            with self.assertRaises(PluginError) as raised:
                await request
        self.assertEqual(raised.exception.code, "PLUGIN_CRASHED")
        self.assertEqual(self.dispatcher._all[instance.manifest.identity]._value, 8)
        release.set()

    async def test_shutdown_cancels_nested_capability(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return httpx.Response(200, json={"url": "https://cdn.example/live.m3u8"})

        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        self.gateway.client = self.client
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock()):
            instance = await self.active()
            request = asyncio.create_task(self.runtime.request(instance, "tv.resolve_stream", {}, timeout=2))
            await asyncio.wait_for(started.wait(), 1)
            await self.runtime.shutdown()
            with self.assertRaises(PluginError) as raised:
                await request
        self.assertIn(raised.exception.code, {"PLUGIN_CRASHED", "PLUGIN_UNAVAILABLE"})
        self.assertFalse(instance.process._capability_tasks)
        release.set()

    async def test_cache_namespace_ttl_and_config(self):
        first = validate_manifest(manifest_data(permissions={"cache": {}}))
        second_data = manifest_data(permissions={"cache": {}}); second_data["plugin_id"] = "other"
        second = validate_manifest(second_data)
        a, b = PermissionGate(first, PermissionPolicy(frozenset({"cache"}))), PermissionGate(second, PermissionPolicy(frozenset({"cache"})))
        self.gateway.cache_set(first.identity, a, "key", "value", ttl_seconds=30)
        self.assertEqual(self.gateway.cache_get(first.identity, a, "key")["value"], "value")
        self.assertFalse(self.gateway.cache_get(second.identity, b, "key")["found"])
        self.gateway.set_scoped_config(first.identity, {"region": "cn"})
        self.assertEqual(self.gateway.config_get(first.identity, "region"), {"found": True, "value": "cn"})
        self.assertEqual(self.gateway.config_get(second.identity, "region"), {"found": False, "value": None})


class DependencyFoundationTest(unittest.TestCase):
    def test_artifact_cache_is_content_addressed_and_environment_isolated(self):
        data = b"wheel-fixture"
        digest = hashlib.sha256(data).hexdigest()
        artifact = DependencyArtifact("curl-cffi", "0.7.0", "linux-x86_64", "cp312", digest, len(data))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.whl"; source.write_bytes(data)
            cache = DependencyArtifactCache(Path(tmp) / "cache")
            first = cache.put_verified(source, artifact)
            second = cache.put_verified(source, artifact)
            self.assertEqual(first, second)
            self.assertTrue(first.is_file())
            plan = PluginEnvironmentPlan.create("org.waveflow/fjtv", "a" * 64, Path(tmp) / "envs")
            self.assertIn("org.waveflow.fjtv", str(plan.environment_root))
            other_version = DependencyArtifact("curl-cffi", "0.8.0", "linux-x86_64", "cp312", digest, len(data))
            self.assertNotEqual(artifact.cache_key, other_version.cache_key)
            self.assertEqual(cache.path_for(artifact), cache.path_for(other_version))

    def test_python_runtime_metadata_rejects_arbitrary_installer(self):
        with self.assertRaises(PluginError):
            PythonRuntimeSpec.parse({
                "python_version": ">=3.12", "entrypoint": "run.sh", "lock_sha256": "a" * 64,
                "artifacts": [], "install_script": "curl | sh",
            })
