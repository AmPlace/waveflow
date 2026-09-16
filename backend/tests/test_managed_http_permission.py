import os
import tempfile
import unittest
from unittest import mock

import httpx

import database
from plugin_capabilities import CapabilityGateway
from plugin_runtime import PermissionGate, PermissionPolicy, PluginError, validate_manifest
from plugin_permissions import MANAGED_HTTP_PERMISSION, requested_permissions
from ssrf_guard import UnsafeTargetError


def _manifest(*, allow_http: bool = False, managed: bool = True) -> object:
    network = {"managed": managed}
    if allow_http:
        network["allow_http"] = True
    return validate_manifest({
        "manifest_version": 1,
        "publisher_id": "org.waveflow",
        "plugin_id": "managed-http-fixture",
        "display_name": "Managed HTTP Fixture",
        "version": "1.0.0",
        "plugin_api_version": "1.0",
        "core_version_range": ">=0.1.0 <1.0.0",
        "provider_contracts": [{
            "contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"],
        }],
        "owned_schemes": [{"scheme": "managed-http-fixture", "contract": "tv_provider"}],
        "capabilities": ["tv.resolve_stream"],
        "permissions": {"network": network},
        "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
        "artifacts": [{
            "os": "macos", "arch": "arm64", "runtime": "python", "entrypoint": "fixture.py",
            "sha256": "0" * 64, "size_bytes": 1,
            "signature": {"algorithm": "ed25519", "key_id": "fixture", "value": "fixture"},
        }],
        "dependencies": [], "state_schema_version": 1,
    })


class ManagedHttpPermissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        self._tmp = tempfile.TemporaryDirectory(prefix="waveflow-managed-http-")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmp.name, "waveflow.db")
        await database.initialize()

    async def asyncTearDown(self):
        if self._old_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self._old_db_path
        self._tmp.cleanup()

    async def _fetch(self, url: str, *, allow_http: bool, transport: httpx.AsyncBaseTransport) -> dict:
        manifest = _manifest(allow_http=allow_http)
        gateway = CapabilityGateway(client=httpx.AsyncClient(transport=transport))
        try:
            return await gateway.managed_http_fetch(
                manifest.identity,
                PermissionGate(manifest, PermissionPolicy(frozenset({"network"}))),
                {"url": url}, timeout=2,
            )
        finally:
            await gateway.client.aclose()

    def test_plain_http_is_explicit_high_risk_permission(self):
        manifest = _manifest(allow_http=True)
        self.assertEqual(
            [item.name for item in requested_permissions(manifest)],
            ["network.managed", "network.managed_http"],
        )
        self.assertEqual(MANAGED_HTTP_PERMISSION, "network.managed_http")

        with self.assertRaises(PluginError):
            validate_manifest({**manifest.raw, "permissions": {"network": {"allow_http": True}}})
        with self.assertRaises(PluginError):
            validate_manifest({**manifest.raw, "permissions": {"network": {"managed": True, "allow_http": "true"}}})

    async def test_http_without_permission_is_denied(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
        with self.assertRaises(PluginError) as raised:
            await self._fetch("http://example.test/plain", allow_http=False, transport=transport)
        self.assertEqual(raised.exception.code, "CAPABILITY_DENIED")

    async def test_https_is_unaffected_and_http_requires_explicit_permission(self):
        async def validate(url, *, allow_private, allow_loopback, allowed_schemes):
            if url.startswith("http://") and "http" not in allowed_schemes:
                raise UnsafeTargetError("HTTP permission required")

        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
        with mock.patch("plugin_capabilities.assert_safe_target_url", new=validate):
            https_result = await self._fetch("https://example.test/secure", allow_http=False, transport=transport)
            self.assertEqual(https_result["body"], "ok")
            http_result = await self._fetch("http://example.test/plain", allow_http=True, transport=transport)
            self.assertEqual(http_result["body"], "ok")

    async def test_redirect_revalidates_http_permission_each_hop(self):
        async def validate(url, *, allow_private, allow_loopback, allowed_schemes):
            if url.startswith("http://") and "http" not in allowed_schemes:
                raise UnsafeTargetError("HTTP permission required")

        def handler(request):
            if str(request.url) == "https://example.test/start":
                return httpx.Response(302, headers={"location": "http://example.test/final"})
            return httpx.Response(200, text="ok")

        with mock.patch("plugin_capabilities.assert_safe_target_url", new=validate):
            with self.assertRaises(PluginError) as denied:
                await self._fetch(
                    "https://example.test/start", allow_http=False, transport=httpx.MockTransport(handler),
                )
            self.assertEqual(denied.exception.code, "CAPABILITY_DENIED")
            allowed = await self._fetch(
                "https://example.test/start", allow_http=True, transport=httpx.MockTransport(handler),
            )
            self.assertEqual(allowed["body"], "ok")

    async def test_http_permission_does_not_allow_private_redirect(self):
        async def validate(url, *, allow_private, allow_loopback, allowed_schemes):
            self.assertIn("http", allowed_schemes)
            self.assertFalse(allow_private)
            if "127.0.0.1" in url:
                raise UnsafeTargetError("private target")

        def handler(request):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/metadata"})

        with mock.patch("plugin_capabilities.assert_safe_target_url", new=validate):
            with self.assertRaises(PluginError) as denied:
                await self._fetch(
                    "http://example.test/start", allow_http=True, transport=httpx.MockTransport(handler),
                )
            self.assertEqual(denied.exception.code, "CAPABILITY_DENIED")


if __name__ == "__main__":
    unittest.main()
