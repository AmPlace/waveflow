"""Uninstall must leave no recoverable, grantable or routable residue.

The contract these tests pin:

* ``uninstall`` removes the installation, artifact, environment, runtime and
  workspace for exactly one Plugin.
* It also removes the durable routing claims that still name that Plugin, and
  re-projects each affected scheme so it reads as unowned.  Rows owned by other
  Plugins, and ``migration_test`` rows, are untouched.
* It removes the Plugin's permission approvals, because an approval row is keyed
  by a fingerprint that only encodes the permission name: a survivor would
  silently re-authorise ``network.direct`` for a later reinstall of the same id.
* Deleting a leaf directory prunes the now-empty parents above it, and only
  those: a sibling version, another Plugin, or a shared publisher namespace is
  never removed.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_plugin.py"
IDENTITY = "org.waveflow/fixture-multi-provider"
SCHEME = "uninstall-fixture"


class _FixtureMixin:
    """A temp database, a signed synthetic Plugin and a production subsystem."""

    async def _set_up_fixture(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        for name in ("database", "plugin_market", "plugin_permissions", "plugin_production",
                     "plugin_tasks", "plugin_python_runtime", "routers.plugins"):
            sys.modules.pop(name, None)

        import database
        import plugin_market
        from plugin_runtime import PermissionPolicy, PluginRuntime

        self.db, self.pm = database, plugin_market
        await self.db.initialize()
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})))
        self.store = plugin_market.PluginArtifactStore(
            Path(self.tmp.name) / "store", allowed_local_roots=[FIXTURE.parent])
        self.service = plugin_market.PluginMarketService(
            runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "fixture-key"): public}),
            command_factory=lambda manifest, artifact: (
                sys.executable, str(artifact),
                "--identity", manifest.identity, "--version", manifest.version,
                "--schemes", ",".join(scheme for scheme, _contract in manifest.owned_schemes),
                "--tv-only", "--permissions", "network",
            ),
            os_name="linux", arch="x86_64")

    async def _tear_down_fixture(self) -> None:
        if getattr(self, "runtime", None) is not None:
            await self.runtime.shutdown()
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        self.tmp.cleanup()

    def package(self, version: str = "1.0.0", *, direct: bool = True):
        payload = FIXTURE.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        manifest = {
            "manifest_version": 1, "publisher_id": "org.waveflow",
            "plugin_id": "fixture-multi-provider", "display_name": "Uninstall Fixture",
            "version": version, "plugin_api_version": "1.0", "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0",
                                    "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": SCHEME, "contract": "tv_provider"}],
            "capabilities": ["tv.resolve_stream"],
            "permissions": {"network": {"managed": True, **({"direct": True} if direct else {})}},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "python",
                           "entrypoint": "fixture.py", "sha256": digest, "size_bytes": len(payload),
                           "signature": {"algorithm": "ed25519", "key_id": "fixture-key",
                                         "value": base64.b64encode(self.private.sign(payload)).decode()}}],
            "dependencies": [], "state_schema_version": 1,
        }
        return {"id": "official::uninstall-fixture", "package_type": "plugin_package",
                "version": version, "plugin_manifest": manifest,
                "artifact_references": [{"sha256": digest, "local_path": str(FIXTURE)}],
                "market_source": {"source_key": "official"}}

    async def _install(self, version: str = "1.0.0"):
        package = self.package(version)
        await self.service.approve_permission(IDENTITY, [package], "network.direct", "test-admin")
        return await self.service.install_from_packages([package], IDENTITY)

    def _subsystem(self):
        from plugin_production import ProductionPluginSubsystem
        from provider_resolver import ProviderResolver

        return ProductionPluginSubsystem(
            self.service, self.service.trust_policy, Path(self.tmp.name) / "downloads", None,
            ProviderResolver(runtime=self.runtime), object(),
        )


class UninstallResidueContractTest(_FixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self._set_up_fixture()

    async def asyncTearDown(self):
        await self._tear_down_fixture()

    async def test_uninstall_removes_installation_artifact_runtime_and_workspace(self):
        installed = await self._install()
        self.assertEqual(installed["lifecycle_state"], "active")
        self.assertIsNotNone(await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider"))
        artifact = next(self.store.installed_root.glob("org.waveflow/fixture-multi-provider/**/artifact"))
        workspace = self.service._working_directory(self.runtime.registry.route(SCHEME).manifest)
        self.assertTrue(workspace.is_dir())

        subsystem = self._subsystem()
        self.assertTrue(await subsystem.uninstall(IDENTITY))

        self.assertIsNone(await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider"))
        self.assertEqual(await self.db.list_plugin_artifacts("org.waveflow", "fixture-multi-provider"), [])
        self.assertFalse(artifact.exists())
        self.assertFalse(workspace.exists())
        self.assertNotIn(IDENTITY, self.service._active)
        # A second uninstall is refused rather than silently succeeding: the
        # installation row is gone, so there is nothing left to remove.
        from plugin_runtime import PluginError

        with self.assertRaises(PluginError) as missing:
            await subsystem.uninstall(IDENTITY)
        self.assertEqual(missing.exception.code, "RESOURCE_NOT_FOUND")

    async def test_uninstall_clears_only_the_rows_that_name_this_plugin(self):
        await self._install()
        # A released-but-still-named row is what uninstall has to clean up: a row
        # that actively owns the scheme is already refused by the lifecycle guard.
        await self.db.set_plugin_scheme_ownership(SCHEME, "legacy", IDENTITY)
        await self.db.set_plugin_scheme_ownership("other-plugin-scheme", "legacy", "org.waveflow/other")
        await self.db.set_plugin_scheme_ownership("migration-scheme", "migration_test", "org.waveflow/mt")

        subsystem = self._subsystem()
        self.assertTrue(await subsystem.uninstall(IDENTITY))

        remaining = {row["scheme"]: row for row in await self.db.list_plugin_scheme_ownership()}
        self.assertNotIn(SCHEME, remaining)
        self.assertEqual(remaining["other-plugin-scheme"]["plugin_identity"], "org.waveflow/other")
        self.assertEqual(remaining["migration-scheme"]["mode"], "migration_test")
        self.assertEqual(remaining["migration-scheme"]["plugin_identity"], "org.waveflow/mt")

    async def test_cleared_scheme_projects_unowned_and_resolves_fail_closed(self):
        from plugin_runtime import PluginError
        from provider_resolver import UNOWNED_MODE, ROUTABLE_MODES

        await self._install()
        await self.db.set_plugin_scheme_ownership(SCHEME, "legacy", IDENTITY)
        subsystem = self._subsystem()
        self.assertTrue(await subsystem.uninstall(IDENTITY))

        resolver = subsystem.provider_resolver
        self.assertEqual(resolver.mode(SCHEME), UNOWNED_MODE)
        self.assertNotIn(resolver.mode(SCHEME), ROUTABLE_MODES)
        with self.assertRaises(PluginError) as raised:
            await resolver.resolve(f"{SCHEME}://room-1", mock.Mock())
        self.assertEqual(raised.exception.code, "PLUGIN_UNAVAILABLE")

    async def test_uninstall_clears_approvals_so_a_reinstall_is_re_approved(self):
        from plugin_runtime import PluginError

        # Without approval the install gate refuses, and it records a pending row.
        with self.assertRaises(PluginError) as pending:
            await self.service.install_from_packages([self.package()], IDENTITY)
        self.assertEqual(pending.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        self.assertTrue(await self.db.list_plugin_permission_approvals("org.waveflow", "fixture-multi-provider"))

        await self._install()
        approvals = await self.db.list_plugin_permission_approvals("org.waveflow", "fixture-multi-provider")
        self.assertEqual({row["permission_name"] for row in approvals if row["approved"]}, {"network.direct"})

        subsystem = self._subsystem()
        self.assertTrue(await subsystem.uninstall(IDENTITY))
        self.assertEqual(
            await self.db.list_plugin_permission_approvals("org.waveflow", "fixture-multi-provider"), [],
            "uninstall must not leave an approval that a reinstall would inherit",
        )

        # The fingerprint only encodes the permission name, so without this the
        # reinstall would have been silently pre-authorised.
        with self.assertRaises(PluginError) as again:
            await self.service.install_from_packages([self.package()], IDENTITY)
        self.assertEqual(again.exception.code, "PERMISSION_APPROVAL_REQUIRED")

    async def test_a_plugin_can_be_reinstalled_after_uninstall(self):
        await self._install()
        subsystem = self._subsystem()
        self.assertTrue(await subsystem.uninstall(IDENTITY))

        # Re-approving is all a reinstall needs: uninstall removed the durable
        # claims but not the Plugin's ability to come back cleanly.
        package = self.package("1.1.0")
        await self.service.approve_permission(IDENTITY, [package], "network.direct", "test-admin")
        reinstalled = await self.service.install_from_packages([package], IDENTITY)

        self.assertEqual(reinstalled["lifecycle_state"], "active")
        self.assertEqual(reinstalled["active_version"], "1.1.0")
        self.assertIsNotNone(await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider"))
        artifact = next(self.store.installed_root.glob("org.waveflow/fixture-multi-provider/**/artifact"))
        self.assertTrue(artifact.is_file(), "the artifact namespace is recreated")
        self.assertTrue(
            self.service._working_directory(self.runtime.registry.route(SCHEME).manifest).is_dir())


class PurgeResidueContractTest(_FixtureMixin, unittest.IsolatedAsyncioTestCase):
    """The operator path for residue that predates the uninstall contract."""

    async def asyncSetUp(self):
        await self._set_up_fixture()
        self.relative = Path("org.waveflow") / "fixture-multi-provider"

    async def asyncTearDown(self):
        await self._tear_down_fixture()

    def _roots(self):
        return (self.store.staged_root, self.store.installed_root,
                self.service.workspaces_root,
                self.service.python_environments.environments_root)

    async def test_purge_removes_residue_left_by_an_older_uninstall(self):
        await self.db.set_plugin_scheme_ownership(SCHEME, "legacy", IDENTITY)
        await self.db.set_plugin_permission_approval(
            "org.waveflow", "fixture-multi-provider", "network.direct", "stale-fingerprint",
            approved=True, actor="old-build", manifest_version="1.0.0")
        for root in self._roots():
            leftover = root / self.relative / "1.0.0" / "rt" / "digest"
            leftover.mkdir(parents=True)
            (leftover / "marker").write_text("x")

        subsystem = self._subsystem()
        result = await subsystem.purge_uninstalled(IDENTITY)

        self.assertEqual(result["schemes"], [SCHEME])
        self.assertEqual(result["approvals"], 1)
        self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])
        self.assertEqual(
            await self.db.list_plugin_permission_approvals("org.waveflow", "fixture-multi-provider"), [])
        for root in self._roots():
            self.assertFalse((root / self.relative).exists(), str(root))
            self.assertTrue(root.is_dir(), "the shared root itself must survive")

    async def test_purge_refuses_while_the_plugin_is_still_installed(self):
        from plugin_runtime import PluginError

        await self._install()
        subsystem = self._subsystem()
        with self.assertRaises(PluginError) as still_installed:
            await subsystem.purge_uninstalled(IDENTITY)
        self.assertEqual(still_installed.exception.code, "SCHEME_CONFLICT")
        self.assertIsNotNone(
            await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider"))

    async def test_purge_rejects_a_malformed_identity(self):
        from plugin_runtime import PluginError

        subsystem = self._subsystem()
        with self.assertRaises(PluginError) as invalid:
            await subsystem.purge_uninstalled("no-publisher")
        self.assertEqual(invalid.exception.code, "INVALID_PLUGIN_RESPONSE")


class OwnershipClearContractTest(_FixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self._set_up_fixture()

    async def asyncTearDown(self):
        await self._tear_down_fixture()

    async def test_clear_ownership_deletes_the_row_instead_of_writing_a_tombstone(self):
        from provider_resolver import UNOWNED_MODE

        # A released row whose owner is already gone is exactly the woniu shape.
        await self.db.set_plugin_scheme_ownership(SCHEME, "legacy", "")
        subsystem = self._subsystem()

        result = await subsystem.clear_ownership(SCHEME)
        self.assertEqual(result, {"scheme": SCHEME, "mode": UNOWNED_MODE, "plugin": ""})
        self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])
        self.assertEqual(subsystem.provider_resolver.mode(SCHEME), UNOWNED_MODE)

    async def test_clear_ownership_leaves_every_other_row_alone(self):
        await self.db.set_plugin_scheme_ownership(SCHEME, "legacy", "")
        await self.db.set_plugin_scheme_ownership("other-plugin-scheme", "legacy", "org.waveflow/other")
        await self.db.set_plugin_scheme_ownership("migration-scheme", "migration_test", "org.waveflow/mt")
        subsystem = self._subsystem()

        await subsystem.clear_ownership(SCHEME)

        remaining = {row["scheme"]: row for row in await self.db.list_plugin_scheme_ownership()}
        self.assertEqual(set(remaining), {"other-plugin-scheme", "migration-scheme"})
        self.assertEqual(remaining["other-plugin-scheme"]["mode"], "legacy")
        self.assertEqual(remaining["migration-scheme"]["mode"], "migration_test")

    async def test_clear_ownership_is_idempotent_and_rejects_an_empty_scheme(self):
        from plugin_runtime import PluginError

        subsystem = self._subsystem()
        self.assertEqual((await subsystem.clear_ownership(SCHEME))["mode"], "unowned")
        with self.assertRaises(PluginError) as invalid:
            await subsystem.clear_ownership("")
        self.assertEqual(invalid.exception.code, "INVALID_PLUGIN_RESPONSE")

    async def test_deleting_by_identity_ignores_released_and_foreign_rows(self):
        await self.db.set_plugin_scheme_ownership("named", "legacy", IDENTITY)
        await self.db.set_plugin_scheme_ownership("released", "legacy", "")
        await self.db.set_plugin_scheme_ownership("foreign", "legacy", "org.waveflow/other")

        deleted = await self.db.delete_plugin_scheme_ownership_for_identity(IDENTITY)

        self.assertEqual(deleted, ["named"])
        self.assertEqual({row["scheme"] for row in await self.db.list_plugin_scheme_ownership()},
                         {"released", "foreign"})


class FilesystemPruneContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_prune_removes_empty_parents_but_stops_at_the_root(self):
        from plugin_python_runtime import prune_empty_parents

        leaf = self.root / "a" / "b" / "c" / "d"
        leaf.mkdir(parents=True)
        self.assertEqual(prune_empty_parents(leaf, stop=self.root), 4)
        self.assertTrue(self.root.is_dir())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_prune_stops_at_the_first_non_empty_directory(self):
        from plugin_python_runtime import prune_empty_parents

        leaf = self.root / "a" / "b" / "leaf"
        leaf.mkdir(parents=True)
        sibling = self.root / "a" / "b" / "sibling"
        sibling.mkdir()
        self.assertEqual(prune_empty_parents(leaf, stop=self.root), 1)
        self.assertFalse(leaf.exists())
        self.assertTrue(sibling.is_dir())
        self.assertTrue((self.root / "a" / "b").is_dir())

    def test_prune_refuses_to_walk_outside_its_root(self):
        from plugin_python_runtime import prune_empty_parents

        outside = self.root / "outside"
        outside.mkdir()
        stop = self.root / "inside"
        stop.mkdir()
        self.assertEqual(prune_empty_parents(outside, stop=stop), 0)
        self.assertTrue(outside.is_dir())

    def test_remove_environment_prunes_only_that_plugins_empty_shell(self):
        from plugin_python_runtime import PythonEnvironmentManager

        manager = PythonEnvironmentManager(self.root)
        target = manager.environments_root / "org.waveflow" / "gone" / "1.0.0" / "rt" / "digest"
        target.mkdir(parents=True)
        (target / "marker").write_text("x")
        # A sibling version of the same Plugin and another Plugin must survive.
        other_version = manager.environments_root / "org.waveflow" / "gone" / "2.0.0" / "rt" / "digest"
        other_version.mkdir(parents=True)
        (other_version / "marker").write_text("x")
        other_plugin = manager.environments_root / "org.waveflow" / "kept" / "1.0.0" / "rt" / "digest"
        other_plugin.mkdir(parents=True)
        (other_plugin / "marker").write_text("x")

        manager.remove_environment(target)

        self.assertFalse(target.exists())
        self.assertFalse((manager.environments_root / "org.waveflow" / "gone" / "1.0.0").exists())
        self.assertTrue(other_version.is_dir(), "a sibling version must survive")
        self.assertTrue(other_plugin.is_dir(), "another Plugin must survive")
        self.assertTrue(manager.environments_root.is_dir(), "the root is never removed")

    def test_remove_plugin_prunes_the_publisher_only_when_it_empties(self):
        from plugin_market import PluginArtifactStore

        store = PluginArtifactStore(self.root / "artifacts", allowed_local_roots=[self.root])
        last = store.installed_root / "org.waveflow" / "last" / "1.0.0" / "digest"
        last.mkdir(parents=True)
        (last / "artifact").write_text("x")
        store.remove_plugin("org.waveflow", "last")
        self.assertFalse((store.installed_root / "org.waveflow").exists(),
                         "an empty publisher namespace is pruned")
        self.assertTrue(store.installed_root.is_dir())

        keep = store.installed_root / "org.waveflow" / "keep" / "1.0.0" / "digest"
        keep.mkdir(parents=True)
        (keep / "artifact").write_text("x")
        other = store.installed_root / "org.waveflow" / "other" / "1.0.0" / "digest"
        other.mkdir(parents=True)
        (other / "artifact").write_text("x")
        store.remove_plugin("org.waveflow", "other")
        self.assertTrue(keep.is_dir(), "a sibling Plugin must survive")
        self.assertTrue((store.installed_root / "org.waveflow").is_dir())


if __name__ == "__main__":
    unittest.main()
