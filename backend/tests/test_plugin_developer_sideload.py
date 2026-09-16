from __future__ import annotations

import importlib
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class DeveloperPackageTest(unittest.TestCase):
    def test_local_package_digest_and_unsigned_marker_are_required(self):
        from plugin_developer import load_developer_package
        from plugin_runtime import PluginError
        from waveflow_plugin_cli import build_project

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "yunting"
            shutil.copytree(Path(__file__).parents[1] / "bundled_plugins" / "yunting", project)
            built = build_project(project)
            package = load_developer_package(built["manifest"])
            self.assertEqual(package["market_source"]["source_key"], "developer_local")
            self.assertTrue(package["_developer_local"])

            artifact = Path(package["artifact_references"][0]["local_path"])
            original = artifact.read_bytes()
            artifact.write_bytes(original + b"tamper")
            with self.assertRaises(PluginError) as raised:
                load_developer_package(built["manifest"])
            self.assertEqual(raised.exception.code, "ARTIFACT_INTEGRITY_FAILED")
            artifact.write_bytes(original)

            manifest_path = Path(built["manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"][0]["signature"]["value"] = "not-unsigned"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(PluginError) as raised:
                load_developer_package(manifest_path)
            self.assertEqual(raised.exception.code, "PLUGIN_UNTRUSTED")


class DeveloperSideloadLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        self.old_bootstrap = os.environ.get("WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "0"
        for name in ("database", "plugin_developer", "plugin_market", "plugin_production"):
            sys.modules.pop(name, None)
        self.db = importlib.import_module("database")
        await self.db.initialize()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        self.subsystems = []

    async def asyncTearDown(self):
        for subsystem in reversed(self.subsystems):
            await subsystem.shutdown()
        await self.http.aclose()
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        if self.old_bootstrap is None:
            os.environ.pop("WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP", None)
        else:
            os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = self.old_bootstrap
        for name in ("database", "plugin_developer", "plugin_market", "plugin_production"):
            sys.modules.pop(name, None)
        self.tmp.cleanup()

    async def _make_subsystem(self):
        from plugin_production import ProductionPluginSubsystem

        subsystem = await ProductionPluginSubsystem.create(
            root=Path(self.tmp.name) / "plugins", http_client=self.http,
        )
        self.subsystems.append(subsystem)
        return subsystem

    def _build_yunting(self) -> str:
        from waveflow_plugin_cli import build_project

        project = Path(self.tmp.name) / "yunting"
        shutil.copytree(Path(__file__).parents[1] / "bundled_plugins" / "yunting", project)
        return str(build_project(project)["manifest"])

    async def test_developer_mode_gate_restart_recovery_and_disable_semantics(self):
        from plugin_runtime import PluginError

        subsystem = await self._make_subsystem()
        manifest_path = self._build_yunting()
        with self.assertRaises(PluginError) as raised:
            await subsystem.install_developer_local(manifest_path)
        self.assertEqual(raised.exception.code, "DEVELOPER_MODE_REQUIRED")

        await subsystem.set_developer_mode(True)
        installed = await subsystem.install_developer_local(manifest_path)
        self.assertEqual(installed["trust_class"], "developer_local")
        row = await self.db.get_plugin_installation("org.waveflow", "yunting")
        self.assertEqual(row["source_key"], "developer_local")
        self.assertTrue(Path(row["artifact_path"]).is_file())

        await subsystem.set_developer_mode(False)
        await subsystem.shutdown()
        self.subsystems.remove(subsystem)
        restarted = await self._make_subsystem()
        recovery = await restarted.startup()
        self.assertEqual(recovery[0]["status"], "active")
        recovered = await self.db.get_plugin_installation("org.waveflow", "yunting")
        self.assertEqual(recovered["trust_class"], "developer_local")
        self.assertTrue(restarted.service.installed_artifact_valid(recovered))
        with self.assertRaises(PluginError) as raised:
            await restarted.install_developer_local(manifest_path)
        self.assertEqual(raised.exception.code, "DEVELOPER_MODE_REQUIRED")

    async def test_local_to_official_uses_normal_upgrade_transaction(self):
        from plugin_production import ProductionTrustPolicy
        from waveflow_plugin_cli import build_project, sign_build

        subsystem = await self._make_subsystem()
        await subsystem.set_developer_mode(True)
        local_manifest = self._build_yunting()
        await subsystem.install_developer_local(local_manifest)

        project = Path(self.tmp.name) / "yunting-official"
        shutil.copytree(Path(__file__).parents[1] / "bundled_plugins" / "yunting", project)
        manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
        manifest["version"] = "1.1.0"
        (project / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        built = build_project(project)
        key = Ed25519PrivateKey.generate()
        key_path = Path(self.tmp.name) / "official-key.raw"
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption(),
        ))
        signed = sign_build(built["manifest"], key_path, key_id="test-official-key")
        package = json.loads(Path(signed["package"]).read_text(encoding="utf-8"))
        source = Path(package["artifact_references"][0]["local_path"])
        target = subsystem.download_root / "official-test-artifact.pyz"
        shutil.copyfile(source, target)
        package["market_source"] = {"source_key": "official", "is_builtin": True}
        package["artifact_references"] = [{"sha256": package["artifact_references"][0]["sha256"], "local_path": str(target)}]
        public_key = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        subsystem.trust_policy.replace([{
            "publisher_id": "org.waveflow", "key_id": "test-official-key",
            "public_key": base64.b64encode(public_key).decode(),
            "trust_level": "official", "enabled": 1, "require_manifest_signature": True,
        }])

        upgraded = await subsystem.service.install_from_packages([package], "org.waveflow/yunting")
        self.assertEqual(upgraded["active_version"], "1.1.0")
        self.assertEqual(upgraded["trust_class"], "official")
        self.assertEqual(upgraded["trust_state"], "official")
        with self.assertRaises(Exception) as raised:
            await subsystem.install_developer_local(local_manifest)
        self.assertEqual(raised.exception.code, "PLUGIN_UNTRUSTED")

    async def test_developer_local_high_risk_permission_uses_local_trust_boundary(self):
        from waveflow_plugin_cli import build_project

        subsystem = await self._make_subsystem()
        await subsystem.set_developer_mode(True)
        project = Path(self.tmp.name) / "direct-yunting"
        shutil.copytree(Path(__file__).parents[1] / "bundled_plugins" / "yunting", project)
        manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
        manifest["permissions"]["network"]["direct"] = True
        (project / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        local_manifest = str(build_project(project)["manifest"])

        projection = await subsystem.approve_developer_local_permission(
            local_manifest, "network.direct", "developer-test",
        )
        self.assertEqual([item["name"] for item in projection["pending"]], [])
        installed = await subsystem.install_developer_local(local_manifest)
        self.assertEqual(installed["trust_class"], "developer_local")
        row = await self.db.get_plugin_installation("org.waveflow", "yunting")
        self.assertEqual(row["lifecycle_state"], "active")

    async def test_developer_local_plain_http_requires_explicit_approval(self):
        from plugin_runtime import PluginError
        from waveflow_plugin_cli import build_project

        subsystem = await self._make_subsystem()
        await subsystem.set_developer_mode(True)
        project = Path(self.tmp.name) / "plain-http-yunting"
        shutil.copytree(Path(__file__).parents[1] / "bundled_plugins" / "yunting", project)
        manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
        manifest["permissions"]["network"]["allow_http"] = True
        (project / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        local_manifest = str(build_project(project)["manifest"])

        with self.assertRaises(PluginError) as pending:
            await subsystem.install_developer_local(local_manifest)
        self.assertEqual(pending.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        projection = await subsystem.approve_developer_local_permission(
            local_manifest, "network.managed_http", "developer-test",
        )
        self.assertEqual([item["name"] for item in projection["pending"]], [])
        installed = await subsystem.install_developer_local(local_manifest)
        self.assertEqual(installed["trust_class"], "developer_local")

    async def test_developer_local_plain_http_revoke_is_fail_closed(self):
        from plugin_runtime import PluginError
        from waveflow_plugin_cli import build_project

        subsystem = await self._make_subsystem()
        await subsystem.set_developer_mode(True)
        project = Path(self.tmp.name) / "revoke-plain-http-yunting"
        shutil.copytree(Path(__file__).parents[1] / "bundled_plugins" / "yunting", project)
        manifest = json.loads((project / "manifest.json").read_text(encoding="utf-8"))
        manifest["permissions"]["network"]["allow_http"] = True
        (project / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        local_manifest = str(build_project(project)["manifest"])

        await subsystem.approve_developer_local_permission(
            local_manifest, "network.managed_http", "developer-test",
        )
        await subsystem.install_developer_local(local_manifest)
        await subsystem.revoke_permission(
            "org.waveflow/yunting", "network.managed_http", "developer-test",
        )
        row = await self.db.get_plugin_installation("org.waveflow", "yunting")
        self.assertEqual((row["enabled"], row["lifecycle_state"]), (1, "unavailable"))
        with self.assertRaises(PluginError) as denied:
            await subsystem.service.enable("org.waveflow/yunting")
        self.assertEqual(denied.exception.code, "PERMISSION_APPROVAL_REQUIRED")

        await subsystem.approve_developer_local_permission(
            local_manifest, "network.managed_http", "developer-test",
        )
        self.assertEqual(
            (await subsystem.service.enable("org.waveflow/yunting"))["lifecycle_state"], "active",
        )


if __name__ == "__main__":
    unittest.main()
