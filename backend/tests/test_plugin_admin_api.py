from __future__ import annotations

import base64
import importlib
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx


def _clear_modules():
    for name in list(sys.modules):
        if name in {"database", "main", "plugin_production", "plugin_permissions", "routers.plugins"}:
            sys.modules.pop(name, None)


class PluginAdminApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self.tmp.name, "waveflow.db")
        _clear_modules()
        self.db = importlib.import_module("database")
        self.main = importlib.import_module("main")
        await self.db.initialize()
        self.subsystem = SimpleNamespace(
            service=mock.Mock(),
            reload_trust=mock.AsyncMock(),
            set_ownership=mock.AsyncMock(return_value={"scheme": "synthetic", "mode": "legacy", "plugin": ""}),
            disable=mock.AsyncMock(return_value={"plugin": "org.example/fixture", "enabled": False}),
            uninstall=mock.AsyncMock(return_value=True),
            approve_permission=mock.AsyncMock(return_value={"requested": [], "approved": [], "pending": [], "risk": {}}),
            revoke_permission=mock.AsyncMock(return_value={"requested": [], "approved": [], "pending": [], "risk": {}}),
        )
        self.main.app.state.plugin_subsystem = self.subsystem
        self.main.app.state.automation_service = None
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.main.app.dependency_overrides.clear()
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        _clear_modules()
        self.tmp.cleanup()

    async def test_plugin_routes_require_admin(self):
        for method, path in (("GET", "/api/admin/plugins"), ("PUT", "/api/admin/plugins/ownership/synthetic")):
            response = await self.client.request(method, path, json={"mode": "legacy"} if method == "PUT" else None)
            self.assertIn(response.status_code, {401, 403})

    async def test_trust_write_and_projection_hide_public_key(self):
        async def admin():
            return {"id": 1, "role": "admin"}
        self.main.app.dependency_overrides[self.main.require_admin] = admin
        public_key = base64.b64encode(b"x" * 32).decode()
        response = await self.client.put("/api/admin/plugins/trust", json={
            "publisher_id": "org.example", "key_id": "release-key",
            "public_key": public_key, "trust_level": "third_party", "enabled": True,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("public_key", response.json())
        listed = await self.client.get("/api/admin/plugins/trust")
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn(public_key, listed.text)
        official = next(item for item in listed.json()["publishers"] if item["publisher_id"] == "org.waveflow")
        self.assertTrue(official["builtin"])
        self.assertEqual(official["trust_level"], "official")
        self.subsystem.reload_trust.assert_awaited_once()

    async def test_official_publisher_trust_cannot_be_replaced_by_admin(self):
        async def admin():
            return {"id": 1, "role": "admin"}
        self.main.app.dependency_overrides[self.main.require_admin] = admin
        response = await self.client.put("/api/admin/plugins/trust", json={
            "publisher_id": "org.waveflow", "key_id": "test-key",
            "public_key": base64.b64encode(b"x" * 32).decode(),
            "trust_level": "official", "enabled": True,
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "PLUGIN_UNTRUSTED")
        self.subsystem.reload_trust.assert_not_awaited()

    async def test_installed_projection_hides_artifact_and_process_details(self):
        async def admin():
            return {"id": 1, "role": "admin"}
        self.main.app.dependency_overrides[self.main.require_admin] = admin
        await self.db.begin_plugin_candidate(
            publisher_id="org.example", plugin_id="fixture", version="1.0.0",
            trust_state="third_party", source_key="third", source_package_id="third::fixture",
            manifest_json='{"display_name":"Fixture","owned_schemes":[],"provider_contracts":[]}',
            manifest_sha256="1" * 64, artifact_sha256="2" * 64,
            artifact_path="/secret/local/plugin", runtime_type="python", entrypoint="plugin.py",
            platform_os="linux", platform_arch="x86_64",
        )
        response = await self.client.get("/api/admin/plugins")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("/secret/local/plugin", response.text)
        self.assertNotIn("artifact_path", response.text)
        self.assertNotIn("pid", response.text.lower())

    async def test_python_runtime_projection_is_scoped_and_hides_environment_paths(self):
        async def admin():
            return {"id": 1, "role": "admin"}
        self.main.app.dependency_overrides[self.main.require_admin] = admin
        from tests.test_plugin_python_runtime import _manifest, _wheel
        from pathlib import Path
        dependency = _wheel(Path(self.tmp.name), "fixture-admin-dependency", "1.0.0", "ok")
        manifest = _manifest("fixture-python-admin", "1.0.0", [dependency])
        await self.db.begin_plugin_candidate(
            publisher_id="org.waveflow", plugin_id="fixture-python-admin", version="1.0.0",
            trust_state="third_party", source_key="third", source_package_id="third::python",
            manifest_json=json.dumps(manifest.raw), manifest_sha256="1" * 64,
            artifact_sha256="2" * 64, artifact_path="/secret/plugin.py",
            runtime_type="python", entrypoint="plugin.py", platform_os="macos", platform_arch="arm64")
        response = await self.client.get("/api/admin/plugins/org.waveflow/fixture-python-admin")
        self.assertEqual(response.status_code, 200, response.text)
        runtime = response.json()["runtime"]
        self.assertEqual((runtime["type"], runtime["dependency_count"]), ("python", 1))
        self.assertEqual(runtime["dependencies"], [{"name": "fixture-admin-dependency", "version": "1.0.0"}])
        self.assertNotIn("/secret", response.text)

    async def test_disable_and_uninstall_use_production_ownership_guard(self):
        async def admin():
            return {"id": 1, "role": "admin"}
        self.main.app.dependency_overrides[self.main.require_admin] = admin
        disabled = await self.client.post("/api/admin/plugins/org.example/fixture/disable")
        removed = await self.client.delete("/api/admin/plugins/org.example/fixture")
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(removed.json(), {"removed": True})
        self.subsystem.disable.assert_awaited_once_with("org.example/fixture")
        self.subsystem.uninstall.assert_awaited_once_with("org.example/fixture")

    async def test_ownership_preflight_unavailable_is_reported_as_service_blocker(self):
        async def admin():
            return {"id": 1, "role": "admin"}
        self.main.app.dependency_overrides[self.main.require_admin] = admin
        from plugin_runtime import PluginError
        self.subsystem.set_ownership.side_effect = PluginError(
            "PLUGIN_UNAVAILABLE", "Plugin runtime is not healthy", category="routing",
        )

        response = await self.client.put(
            "/api/admin/plugins/ownership/jstv",
            json={"mode": "plugin", "plugin": "org.waveflow/jstv"},
        )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"]["code"], "PLUGIN_UNAVAILABLE")

    async def test_permission_approval_and_revoke_use_production_subsystem(self):
        async def admin():
            return {"id": 1, "role": "admin"}
        self.main.app.dependency_overrides[self.main.require_admin] = admin
        with mock.patch("routers.plugins._packages", new=mock.AsyncMock(return_value=[{"id": "fixture"}])):
            approved = await self.client.post(
                "/api/admin/plugins/org.example/fixture/permissions/approve",
                json={"permission": "network.direct", "package_id": "fixture"})
        revoked = await self.client.post(
            "/api/admin/plugins/org.example/fixture/permissions/revoke",
            json={"permission": "network.direct"})
        self.assertEqual((approved.status_code, revoked.status_code), (200, 200))
        self.subsystem.approve_permission.assert_awaited_once_with(
            "org.example/fixture", [{"id": "fixture"}], "network.direct", "admin_api")
        self.subsystem.revoke_permission.assert_awaited_once_with(
            "org.example/fixture", "network.direct", "admin_api")


if __name__ == "__main__":
    unittest.main()
