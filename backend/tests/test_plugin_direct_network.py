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
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_plugin.py"
IDENTITY = "org.waveflow/fixture-multi-provider"


class DirectNetworkPermissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        for name in (
            "database", "plugin_market", "plugin_permissions", "plugin_production", "plugin_tasks",
            "routers.plugins",
        ):
            sys.modules.pop(name, None)
        import database
        import plugin_market
        from plugin_runtime import PermissionPolicy, PluginRuntime
        self.db, self.pm = database, plugin_market
        await self.db.initialize()
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.runtime = PluginRuntime(permission_policy=PermissionPolicy(frozenset({"network"})))
        self.modes = {}
        self.store = plugin_market.PluginArtifactStore(Path(self.tmp.name) / "store", allowed_local_roots=[FIXTURE.parent])
        self.service = plugin_market.PluginMarketService(
            runtime=self.runtime, store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "fixture-key"): public}),
            command_factory=lambda manifest, artifact: (
                sys.executable, str(artifact), "--mode", self.modes.get(manifest.version, "normal"),
                "--identity", manifest.identity, "--version", manifest.version,
                "--schemes", ",".join(scheme for scheme, _contract in manifest.owned_schemes),
                "--tv-only", "--permissions", "network",
            ),
            os_name="linux", arch="x86_64")

    async def asyncTearDown(self):
        await self.runtime.shutdown()
        if self.old_db is None: os.environ.pop("WAVEFLOW_DB_PATH", None)
        else: os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        self.tmp.cleanup()

    def package(self, version="1.0.0", *, direct=True, schemes=("direct-fixture",)):
        payload = FIXTURE.read_bytes(); digest = hashlib.sha256(payload).hexdigest()
        manifest = {"manifest_version": 1, "publisher_id": "org.waveflow", "plugin_id": "fixture-multi-provider",
            "display_name": "Direct Fixture", "version": version, "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [{"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]}],
            "owned_schemes": [{"scheme": scheme, "contract": "tv_provider"} for scheme in schemes],
            "capabilities": ["tv.resolve_stream"],
            "permissions": {"network": {"managed": True, **({"direct": True} if direct else {})}},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{"os": "linux", "arch": "x86_64", "runtime": "python", "entrypoint": "fixture.py",
                "sha256": digest, "size_bytes": len(payload), "signature": {"algorithm": "ed25519", "key_id": "fixture-key",
                    "value": base64.b64encode(self.private.sign(payload)).decode()}}],
            "dependencies": [], "state_schema_version": 1}
        return {"id": "official::direct-fixture", "package_type": "plugin_package", "version": version,
            "plugin_manifest": manifest, "artifact_references": [{"sha256": digest, "local_path": str(FIXTURE)}],
            "market_source": {"source_key": "official"}}

    async def _production_subsystem(self):
        from plugin_production import ProductionPluginSubsystem
        from provider_resolver import ProviderResolver

        package = self.package()
        await self.service.approve_permission(IDENTITY, [package], "network.direct", "test-admin")
        await self.service.install_from_packages([package], IDENTITY)
        return ProductionPluginSubsystem(
            self.service, self.service.trust_policy, Path(self.tmp.name) / "downloads", None,
            ProviderResolver(runtime=self.runtime), object(),
        )

    async def _crash_with_pending_restart(self, subsystem):
        instance = self.service._active[IDENTITY]
        instance.process.process.kill()
        await instance.process._wait_task
        for task in tuple(subsystem._lifecycle_reconcile_tasks.values()):
            await asyncio.shield(task)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def paused_backoff(_delay):
            entered.set()
            await release.wait()

        self.runtime.sleep = paused_backoff
        restart = asyncio.create_task(self.runtime.restart(instance))
        await entered.wait()
        return instance, restart, release

    async def _wait_reconciliation(self, subsystem):
        for _ in range(20):
            pending = [
                task for task in [
                    *subsystem._lifecycle_reconcile_tasks.values(),
                    *subsystem._ownership_reconcile_tasks.values(),
                ] if not task.done()
            ]
            if not pending:
                await asyncio.sleep(0)
                return
            await asyncio.gather(*(asyncio.shield(task) for task in pending))
        self.fail("Plugin reconciliation did not finish")

    async def test_install_gate_approval_revoke_enable_and_recovery(self):
        from plugin_runtime import PluginError
        package = self.package()
        with self.assertRaises(PluginError) as pending:
            await self.service.install_from_packages([package], IDENTITY)
        self.assertEqual(pending.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        self.assertEqual(await self.db.list_plugin_installations(), [])
        self.assertEqual(list(self.store.installed_root.glob("**/artifact")), [])
        projection = await self.service.approve_permission(IDENTITY, [package], "network.direct", "test-admin")
        self.assertEqual([item["name"] for item in projection["approved"]], ["network.managed", "network.direct"])
        installed = await self.service.install_from_packages([package], IDENTITY)
        self.assertEqual(installed["lifecycle_state"], "active")
        cwd = Path(self.service._active[IDENTITY].process.working_directory)
        self.assertTrue(cwd.is_relative_to(self.store.root))
        self.assertNotEqual(cwd, Path.cwd())
        await self.service.revoke_permission(IDENTITY, "network.direct", "test-admin")
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual((row["enabled"], row["lifecycle_state"]), (1, "unavailable"))
        self.assertNotIn(IDENTITY, self.service._active)
        with self.assertRaises(PluginError) as denied:
            await self.service.enable(IDENTITY)
        self.assertEqual(denied.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        self.assertEqual((await self.service.recover_enabled())[0]["status"], "unavailable")
        await self.service.approve_permission(IDENTITY, [package], "network.direct", "test-admin")
        self.assertEqual((await self.service.enable(IDENTITY))["lifecycle_state"], "active")

    async def test_pending_restart_cannot_bypass_permission_revoke(self):
        from plugin_runtime import PluginError

        subsystem = await self._production_subsystem()
        _instance, restart, release = await self._crash_with_pending_restart(subsystem)
        await subsystem.revoke_permission(IDENTITY, "network.direct", "test-admin")
        release.set()
        with self.assertRaises(PluginError):
            await restart
        with self.assertRaises(PluginError):
            self.runtime.registry.route("direct-fixture")
        approvals = await self.db.list_plugin_permission_approvals()
        direct = next(row for row in approvals if row["permission_name"] == "network.direct")
        self.assertFalse(direct["approved"])
        self.assertNotIn(IDENTITY, self.service._active)

    async def test_raw_runtime_enable_cannot_bypass_production_activation_authority(self):
        from plugin_runtime import LifecycleState, PluginError

        await self._production_subsystem()
        active = self.service._active[IDENTITY]
        rogue = self.runtime.install(
            active.manifest,
            self.runtime._commands[active.instance_id],
            working_directory=self.runtime._execution[active.instance_id][1],
        )
        with self.assertRaises(PluginError) as denied:
            await self.runtime.enable(rogue)
        self.assertEqual(denied.exception.code, "PLUGIN_UNAVAILABLE")
        self.assertEqual(rogue.state, LifecycleState.INSTALLED_DISABLED)
        self.assertIs(self.runtime.registry.route("direct-fixture"), active)
        await self.runtime.uninstall(rogue)

    async def test_pending_restart_cannot_bypass_disable(self):
        from plugin_runtime import PluginError

        subsystem = await self._production_subsystem()
        _instance, restart, release = await self._crash_with_pending_restart(subsystem)
        await subsystem.disable(IDENTITY)
        release.set()
        with self.assertRaises(PluginError):
            await restart
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual((row["enabled"], row["lifecycle_state"]), (0, "disabled"))
        with self.assertRaises(PluginError):
            self.runtime.registry.route("direct-fixture")

    async def test_pending_restart_cannot_bypass_uninstall(self):
        from plugin_runtime import PluginError

        subsystem = await self._production_subsystem()
        _instance, restart, release = await self._crash_with_pending_restart(subsystem)
        self.assertTrue(await subsystem.uninstall(IDENTITY))
        release.set()
        with self.assertRaises(PluginError):
            await restart
        self.assertIsNone(await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider"))
        with self.assertRaises(PluginError):
            self.runtime.registry.route("direct-fixture")

    async def test_approved_restart_converges_database_settings_and_plugin_resolver(self):
        import importlib

        subsystem = await self._production_subsystem()
        await subsystem.set_ownership("direct-fixture", "plugin", IDENTITY)
        _instance, restart, release = await self._crash_with_pending_restart(subsystem)
        release.set()
        await restart
        await self._wait_reconciliation(subsystem)
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        projection = await importlib.import_module("routers.plugins")._plugin_projection(
            row, runtime=self.runtime,
        )
        self.assertEqual((row["lifecycle_state"], row["last_error"]), ("active", ""))
        self.assertTrue(projection["runtime_available"])
        self.assertTrue(subsystem.provider_resolver.is_available("direct-fixture"))
        resolved = await subsystem.provider_resolver.resolve("direct-fixture://fixture", None)
        self.assertEqual(resolved["stream_descriptor_version"], "1.0")
        await subsystem.set_ownership("direct-fixture", "legacy")

    async def test_update_permission_escalation_keeps_old_active_and_reduction_is_allowed(self):
        v1 = self.package("1.0.0", direct=False)
        await self.service.install_from_packages([v1], IDENTITY)
        v2 = self.package("2.0.0", direct=True)
        with self.assertRaises(Exception) as pending:
            await self.service.install_from_packages([v2], IDENTITY)
        self.assertEqual(pending.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        self.assertEqual(self.runtime.registry.route("direct-fixture").manifest.version, "1.0.0")
        await self.service.approve_permission(IDENTITY, [v2], "network.direct", "test-admin")
        await self.service.install_from_packages([v2], IDENTITY)
        self.assertEqual(self.runtime.registry.route("direct-fixture").manifest.version, "2.0.0")
        await self.service.install_from_packages([self.package("3.0.0", direct=False)], IDENTITY)
        self.assertEqual(self.runtime.registry.route("direct-fixture").manifest.version, "3.0.0")

    async def test_initial_activation_final_check_rejects_concurrent_approval_loss(self):
        from plugin_permissions import requested_permissions
        from plugin_runtime import validate_manifest

        package = self.package()
        await self.service.approve_permission(IDENTITY, [package], "network.direct", "test-admin")
        prepared = asyncio.Event()
        release = asyncio.Event()
        original_prepare = self.service._prepare_candidate

        async def paused_prepare(candidate):
            result = await original_prepare(candidate)
            prepared.set()
            await release.wait()
            return result

        self.service._prepare_candidate = paused_prepare
        task = asyncio.create_task(self.service.install_from_packages([package], IDENTITY))
        try:
            await prepared.wait()
            manifest = validate_manifest(package["plugin_manifest"])
            request = next(item for item in requested_permissions(manifest) if item.name == "network.direct")
            async with self.service.lifecycle_lock(IDENTITY):
                await self.db.set_plugin_permission_approval(
                    manifest.publisher_id, manifest.plugin_id, request.name, request.fingerprint,
                    approved=False, actor="test-admin", manifest_version=manifest.version,
                )
            release.set()
            with self.assertRaises(Exception) as denied:
                await task
            self.assertEqual(denied.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        finally:
            release.set()
            self.service._prepare_candidate = original_prepare
        self.assertEqual(await self.db.list_plugin_installations(), [])
        self.assertNotIn(IDENTITY, self.service._active)
        self.assertEqual(list(self.store.staged_root.glob("**/artifact")), [])

    async def test_update_activation_and_revoke_share_final_identity_boundary(self):
        from plugin_permissions import requested_permissions
        from plugin_runtime import validate_manifest

        v1 = self.package("1.0.0")
        await self.service.approve_permission(IDENTITY, [v1], "network.direct", "test-admin")
        await self.service.install_from_packages([v1], IDENTITY)
        v2 = self.package("2.0.0")
        prepared = asyncio.Event()
        release = asyncio.Event()
        original_prepare = self.service._prepare_candidate

        async def paused_prepare(candidate):
            result = await original_prepare(candidate)
            prepared.set()
            await release.wait()
            return result

        self.service._prepare_candidate = paused_prepare
        update = asyncio.create_task(self.service.install_from_packages([v2], IDENTITY))
        try:
            await prepared.wait()
            await self.service.revoke_permission(IDENTITY, "network.direct", "test-admin")
            release.set()
            with self.assertRaises(Exception) as denied:
                await update
            self.assertEqual(denied.exception.code, "PERMISSION_APPROVAL_REQUIRED")
        finally:
            release.set()
            self.service._prepare_candidate = original_prepare
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual((row["active_version"], row["lifecycle_state"]), ("1.0.0", "unavailable"))
        self.assertNotIn(IDENTITY, self.service._active)
        request = next(
            item for item in requested_permissions(validate_manifest(v2["plugin_manifest"]))
            if item.name == "network.direct"
        )
        approval = await self.db.get_plugin_permission_approval(
            "org.waveflow", "fixture-multi-provider", "network.direct",
            request.fingerprint,
        )
        self.assertFalse(approval["approved"])

    async def test_automation_update_observes_concurrent_revoke(self):
        import plugin_tasks

        v1 = self.package("1.0.0")
        await self.service.approve_permission(IDENTITY, [v1], "network.direct", "test-admin")
        await self.service.install_from_packages([v1], IDENTITY)
        v2 = self.package("2.0.0")
        prepared = asyncio.Event()
        release = asyncio.Event()
        original_prepare = self.service._prepare_candidate

        async def paused_prepare(candidate):
            result = await original_prepare(candidate)
            prepared.set()
            await release.wait()
            return result

        async def install(identity, packages):
            return await self.service.install_from_packages(packages, identity)

        context = SimpleNamespace(
            stop_requested=lambda: False, task_type="auto_update", report_progress=mock.AsyncMock(),
        )
        refresh = {"source_results": [{
            "source_key": "official", "status": "success", "usable_for_update": True,
            "package_ids": [v2["id"]],
        }]}
        self.service._prepare_candidate = paused_prepare
        with mock.patch.object(plugin_tasks.market, "refresh_market", new=mock.AsyncMock(return_value=refresh)), \
                mock.patch.object(plugin_tasks.market, "market_packages_snapshot", return_value=[v2]):
            automation = asyncio.create_task(
                plugin_tasks.run_plugin_update_task(context, SimpleNamespace(install=install)),
            )
            try:
                await prepared.wait()
                await self.service.revoke_permission(IDENTITY, "network.direct", "test-admin")
                release.set()
                result = await automation
            finally:
                release.set()
                self.service._prepare_candidate = original_prepare
        self.assertEqual((result.updated_count, result.skipped_count, result.failed_count), (0, 1, 0))
        self.assertIn("permission approval required", result.errors[0])
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual((row["active_version"], row["lifecycle_state"]), ("1.0.0", "unavailable"))

    async def test_plugin_owned_failed_candidate_keeps_v1_owner_approval_and_settings(self):
        import importlib
        from plugin_production import ProductionPluginSubsystem
        from provider_resolver import ProviderResolver

        v1 = self.package("1.0.0")
        await self.service.approve_permission(IDENTITY, [v1], "network.direct", "test-admin")
        await self.service.install_from_packages([v1], IDENTITY)
        subsystem = ProductionPluginSubsystem(
            self.service, self.service.trust_policy, Path(self.tmp.name) / "downloads", None,
            ProviderResolver(runtime=self.runtime), object(),
        )
        await subsystem.set_ownership("direct-fixture", "plugin", IDENTITY)
        self.modes["2.0.0"] = "health_fail"
        with self.assertRaises(Exception):
            await self.service.install_from_packages([self.package("2.0.0")], IDENTITY)
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        owner = next(item for item in await self.db.list_plugin_scheme_ownership()
                     if item["scheme"] == "direct-fixture")
        permissions = await self.service.permission_projection(IDENTITY)
        settings = await importlib.import_module("routers.plugins")._plugin_projection(
            row, runtime=self.runtime,
        )
        self.assertEqual((row["active_version"], row["lifecycle_state"]), ("1.0.0", "active"))
        self.assertEqual((owner["mode"], owner["plugin_identity"]), ("plugin", IDENTITY))
        self.assertEqual(self.runtime.registry.route("direct-fixture").manifest.version, "1.0.0")
        self.assertEqual([item["name"] for item in permissions["approved"]],
                         ["network.managed", "network.direct"])
        self.assertTrue(settings["runtime_available"])
        self.assertEqual(settings["lifecycle_state"], "active")
        self.assertTrue(subsystem.provider_resolver.is_available("direct-fixture"))
        await subsystem.set_ownership("direct-fixture", "legacy")

    async def test_ownership_and_permission_revoke_share_identity_lock(self):
        from plugin_production import ProductionPluginSubsystem
        from provider_resolver import ProviderResolver
        from plugin_runtime import PluginError

        package = self.package()
        await self.service.approve_permission(IDENTITY, [package], "network.direct", "test-admin")
        await self.service.install_from_packages([package], IDENTITY)
        subsystem = ProductionPluginSubsystem(
            self.service, self.service.trust_policy, Path(self.tmp.name) / "downloads", None,
            ProviderResolver(runtime=self.runtime), object(),
        )
        await subsystem.set_ownership("direct-fixture", "legacy")

        entered = asyncio.Event()
        release = asyncio.Event()
        original_preflight = subsystem._ownership_preflight

        async def paused_preflight(scheme, plugin_identity=""):
            result = await original_preflight(scheme, plugin_identity)
            entered.set()
            await release.wait()
            return result

        subsystem._ownership_preflight = paused_preflight
        owner_task = asyncio.create_task(subsystem.set_ownership("direct-fixture", "plugin", IDENTITY))
        revoke_started = asyncio.Event()

        async def revoke():
            revoke_started.set()
            return await subsystem.revoke_permission(IDENTITY, "network.direct", "test-admin")

        revoke_task = None
        try:
            await entered.wait()
            revoke_task = asyncio.create_task(revoke())
            await revoke_started.wait()
            self.assertFalse(revoke_task.done())
            release.set()
            await owner_task
            with self.assertRaises(PluginError) as blocked:
                await revoke_task
            self.assertEqual(blocked.exception.code, "SCHEME_CONFLICT")
            row = next(item for item in await self.db.list_plugin_scheme_ownership()
                       if item["scheme"] == "direct-fixture")
            self.assertEqual((row["mode"], row["plugin_identity"]), ("plugin", IDENTITY))
            # The ownership guard must run before the permission mutation.  The
            # fingerprint is checked through the persisted approval rows.
            approvals = await self.db.list_plugin_permission_approvals()
            self.assertTrue(any(item["permission_name"] == "network.direct" and item["approved"] for item in approvals))
        finally:
            release.set()
            if not owner_task.done():
                await owner_task
            if revoke_task is not None and not revoke_task.done():
                await revoke_task
            subsystem._ownership_preflight = original_preflight
            await subsystem.set_ownership("direct-fixture", "legacy")

    async def test_multi_scheme_ownership_update_and_destructive_operations_do_not_deadlock(self):
        from plugin_production import ProductionPluginSubsystem
        from provider_resolver import ProviderResolver

        schemes = ("direct-a", "direct-b")
        v1 = self.package("1.0.0", schemes=schemes)
        await self.service.approve_permission(IDENTITY, [v1], "network.direct", "test-admin")
        await self.service.install_from_packages([v1], IDENTITY)
        subsystem = ProductionPluginSubsystem(
            self.service, self.service.trust_policy, Path(self.tmp.name) / "downloads", None,
            ProviderResolver(runtime=self.runtime), object(),
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        original_preflight = subsystem._ownership_preflight

        async def paused_preflight(scheme, plugin_identity=""):
            result = await original_preflight(scheme, plugin_identity)
            if scheme == "direct-a":
                entered.set()
                await release.wait()
            return result

        subsystem._ownership_preflight = paused_preflight
        owner_a = asyncio.create_task(subsystem.set_ownership("direct-a", "plugin", IDENTITY))
        await entered.wait()
        owner_b = asyncio.create_task(subsystem.set_ownership("direct-b", "plugin", IDENTITY))
        update = asyncio.create_task(
            self.service.install_from_packages([self.package("2.0.0", schemes=schemes)], IDENTITY),
        )
        disable = asyncio.create_task(subsystem.disable(IDENTITY))
        uninstall = asyncio.create_task(subsystem.uninstall(IDENTITY))
        revoke = asyncio.create_task(subsystem.revoke_permission(IDENTITY, "network.direct", "test-admin"))
        try:
            self.assertFalse(any(task.done() for task in (owner_b, disable, uninstall, revoke)))
            release.set()
            await asyncio.wait_for(asyncio.gather(owner_a, owner_b, update), timeout=10)
            for task in (disable, uninstall, revoke):
                with self.assertRaises(Exception) as blocked:
                    await asyncio.wait_for(task, timeout=10)
                self.assertEqual(blocked.exception.code, "SCHEME_CONFLICT")
        finally:
            release.set()
            subsystem._ownership_preflight = original_preflight
        owners = {row["scheme"]: row for row in await self.db.list_plugin_scheme_ownership()}
        self.assertTrue(all(
            owners[scheme]["mode"] == "plugin" and owners[scheme]["plugin_identity"] == IDENTITY
            for scheme in schemes
        ))
        self.assertEqual(self.runtime.registry.route("direct-a").manifest.version, "2.0.0")
        self.assertEqual(self.runtime.registry.route("direct-b").manifest.version, "2.0.0")
        approvals = await self.db.list_plugin_permission_approvals()
        self.assertTrue(any(row["permission_name"] == "network.direct" and row["approved"] for row in approvals))
        await subsystem.set_ownership("direct-a", "legacy")
        await subsystem.set_ownership("direct-b", "legacy")

    def test_subprocess_environment_allowlist_removes_core_secrets(self):
        from plugin_runtime.process import sanitized_plugin_environment
        value = sanitized_plugin_environment({"PATH": "/bin", "LANG": "C.UTF-8", "WAVEFLOW_TOKEN": "secret",
                                               "DATABASE_URL": "secret", "API_KEY": "secret"})
        self.assertEqual(value, {"PATH": "/bin", "LANG": "C.UTF-8"})


if __name__ == "__main__": unittest.main()
