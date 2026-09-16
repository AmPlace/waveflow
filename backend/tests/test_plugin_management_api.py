from __future__ import annotations

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
        if name in {"database", "main", "market", "plugin_permissions", "routers.plugins"}:
            sys.modules.pop(name, None)


def plugin_package(*, version: str = "1.1.0", direct: bool = False, allow_http: bool = False) -> dict:
    permissions = {"network": {
        "managed": True,
        **({"direct": True} if direct else {}),
        **({"allow_http": True} if allow_http else {}),
    }}
    return {
        "id": "official::fixture-plugin",
        "name": "Fixture Plugin",
        "kind": "plugin_package",
        "package_type": "plugin_package",
        "version": version,
        "plugin_installable": True,
        "supported_in_v1": False,
        "previewable": False,
        "importable": False,
        "plugin_manifest": {
            "manifest_version": 1,
            "publisher_id": "org.waveflow",
            "plugin_id": "fixture",
            "display_name": "Fixture Plugin",
            "version": version,
            "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{
                "contract": "tv_provider", "contract_version": "1.0",
                "features": ["resolve_stream"],
            }],
            "owned_schemes": [{"scheme": "fixture", "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"],
            "permissions": permissions,
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{
                "os": "linux", "arch": "x86_64", "runtime": "python",
                "entrypoint": "plugin.py", "sha256": "2" * 64, "size_bytes": 1,
                "signature": {
                    "algorithm": "ed25519", "key_id": "fixture-key", "value": "fixture",
                },
            }],
            "dependencies": [],
            "state_schema_version": 1,
        },
        "artifact_references": [],
        "market_source": {"source_key": "official", "name": "Official"},
    }


class PluginManagementApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self.tmp.name, "waveflow.db")
        _clear_modules()
        self.db = importlib.import_module("database")
        self.market = importlib.import_module("market")
        self.main = importlib.import_module("main")
        await self.db.initialize()

        async def admin():
            return {"id": 1, "role": "admin"}

        self.main.app.dependency_overrides[self.main.require_admin] = admin
        self.service = SimpleNamespace(
            dependency_projection=mock.AsyncMock(return_value={"status": "ready", "dependencies": []}),
            enable=mock.AsyncMock(return_value={"plugin": "org.waveflow/fixture", "enabled": True}),
        )
        self.subsystem = SimpleNamespace(
            service=self.service,
            install=mock.AsyncMock(return_value={
                "plugin": "org.waveflow/fixture", "active_version": "1.1.0", "enabled": True,
            }),
            uninstall=mock.AsyncMock(return_value=True),
        )
        self.main.app.state.plugin_subsystem = self.subsystem
        self.main.app.state.automation_service = None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.main.app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.main.app.dependency_overrides.clear()
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        _clear_modules()
        self.tmp.cleanup()

    def set_packages(self, *packages: dict) -> None:
        self.market._market_cache.update({
            "market": {"schema_version": 1, "packages": list(packages)},
            "packages": list(packages),
            "sources": [],
        })

    async def test_market_projects_plugin_install_and_update_state(self):
        package = plugin_package()
        self.set_packages(package)
        await self.db.begin_plugin_candidate(
            publisher_id="org.waveflow", plugin_id="fixture", version="1.0.0",
            trust_state="official", source_key="official",
            source_package_id="official::fixture-plugin",
            manifest_json=json.dumps(package["plugin_manifest"]), manifest_sha256="1" * 64,
            artifact_sha256="2" * 64, artifact_path="/private/plugin",
            runtime_type="subprocess", entrypoint="plugin", platform_os="linux", platform_arch="x86_64",
        )
        await self.db.activate_plugin_candidate(
            publisher_id="org.waveflow", plugin_id="fixture", candidate_version="1.0.0",
            trust_state="official", source_key="official", source_package_id="official::fixture-plugin",
            manifest_json=json.dumps({**package["plugin_manifest"], "version": "1.0.0"}),
            manifest_sha256="3" * 64, artifact_sha256="2" * 64,
            artifact_path="/private/plugin", runtime_type="subprocess", entrypoint="plugin",
            platform_os="linux", platform_arch="x86_64",
        )

        response = await self.client.get("/api/admin/market/packages")
        self.assertEqual(response.status_code, 200, response.text)
        projected = response.json()["packages"][0]
        self.assertTrue(projected["installed"])
        self.assertEqual(projected["installed_version"], "1.0.0")
        self.assertTrue(projected["update_available"])
        self.assertTrue(projected["plugin_installable"])

    def test_plugin_card_flattens_network_permissions(self):
        card = self.market._package_card(self.market._normalize_package(plugin_package(direct=True)))
        self.assertEqual(card["plugin"]["permissions"], ["network.direct", "network.managed"])
        http_card = self.market._package_card(
            self.market._normalize_package(plugin_package(allow_http=True)),
        )
        self.assertEqual(http_card["plugin"]["permissions"], ["network.managed", "network.managed_http"])

    async def test_market_routes_plugin_install_update_and_uninstall(self):
        package = plugin_package()
        self.set_packages(package)
        self.main.app.state.automation_service = SimpleNamespace()

        with mock.patch("plugin_tasks.reconcile_plugin_update_task", new=mock.AsyncMock()) as reconcile:
            installed = await self.client.post(
                "/api/admin/market/packages/official%3A%3Afixture-plugin/import", json={}
            )
            updated = await self.client.post(
                "/api/admin/market/packages/official%3A%3Afixture-plugin/update"
            )
            removed = await self.client.delete(
                "/api/admin/market/packages/official%3A%3Afixture-plugin/install"
            )

        self.assertEqual((installed.status_code, updated.status_code, removed.status_code), (200, 200, 200))
        self.assertEqual(installed.json()["package_type"], "plugin_package")
        self.assertTrue(removed.json()["uninstalled"])
        self.assertEqual(self.subsystem.install.await_count, 2)
        self.subsystem.uninstall.assert_awaited_once_with("org.waveflow/fixture")
        self.assertEqual(reconcile.await_count, 3)

    async def test_content_install_ensures_plugin_dependencies_before_import(self):
        content = {
            "id": "official::content", "name": "Content", "package_type": "content_package",
            "requires_plugins": [{
                "plugin": "org.waveflow/fixture", "version_range": ">=1.0.0 <2.0.0",
                "contract": "tv_provider", "required_schemes": ["fixture"],
            }],
        }
        self.set_packages(content, plugin_package())
        self.service.dependency_projection.return_value = {
            "status": "dependency_missing", "dependencies": [],
        }
        with mock.patch.object(self.market, "import_package", new=mock.AsyncMock(return_value={
            "ok": True, "subscription_id": 7, "channel_count": 2,
        })) as importer:
            response = await self.client.post(
                "/api/admin/market/packages/official%3A%3Acontent/import", json={}
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["installed_plugins"], ["org.waveflow/fixture"])
        self.subsystem.install.assert_awaited_once()
        importer.assert_awaited_once()

    async def test_content_install_does_not_restart_unavailable_plugin_implicitly(self):
        content = {
            "id": "official::content", "name": "Content", "package_type": "content_package",
            "requires_plugins": [{
                "plugin": "org.waveflow/fixture", "version_range": ">=1.0.0 <2.0.0",
                "contract": "tv_provider", "required_schemes": ["fixture"],
            }],
        }
        self.set_packages(content, plugin_package())
        self.service.dependency_projection.return_value = {
            "status": "provider_unavailable",
            "dependencies": [{"plugin": "org.waveflow/fixture", "status": "provider_unavailable"}],
        }

        response = await self.client.post(
            "/api/admin/market/packages/official%3A%3Acontent/import", json={}
        )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"]["code"], "PLUGIN_UNAVAILABLE")
        self.assertEqual(response.json()["detail"]["details"]["plugin"], "org.waveflow/fixture")
        self.subsystem.install.assert_not_awaited()

    async def test_content_install_reports_incompatible_plugin_without_reinstall(self):
        content = {
            "id": "official::content", "name": "Content", "package_type": "content_package",
            "requires_plugins": [{
                "plugin": "org.waveflow/fixture", "version_range": ">=2.0.0 <3.0.0",
                "contract": "tv_provider", "required_schemes": ["fixture"],
            }],
        }
        self.set_packages(content, plugin_package())
        self.service.dependency_projection.return_value = {
            "status": "plugin_incompatible",
            "dependencies": [{"plugin": "org.waveflow/fixture", "status": "plugin_incompatible"}],
        }

        response = await self.client.post(
            "/api/admin/market/packages/official%3A%3Acontent/import", json={}
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["code"], "PLUGIN_INCOMPATIBLE")
        self.subsystem.install.assert_not_awaited()

    async def test_plugin_admin_projects_ownership_permissions_and_market_link(self):
        package = plugin_package()
        self.set_packages(package)
        await self.db.begin_plugin_candidate(
            publisher_id="org.waveflow", plugin_id="fixture", version="1.0.0",
            trust_state="official", source_key="official", source_package_id=package["id"],
            manifest_json=json.dumps({**package["plugin_manifest"], "version": "1.0.0"}),
            manifest_sha256="1" * 64, artifact_sha256="2" * 64, artifact_path="/private/plugin",
            runtime_type="subprocess", entrypoint="plugin", platform_os="linux", platform_arch="x86_64",
        )
        await self.db.set_plugin_scheme_ownership("fixture", "legacy", "")

        response = await self.client.get("/api/admin/plugins/org.waveflow/fixture")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["ownership"], [{"scheme": "fixture", "mode": "legacy", "plugin": ""}])
        self.assertEqual(payload["market"]["package_id"], package["id"])
        self.assertTrue(payload["market"]["update_available"])
        self.assertEqual(payload["permissions"]["requested"][0]["name"], "network.managed")


if __name__ == "__main__":
    unittest.main()
