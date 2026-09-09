from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_plugin.py"


def _clear_modules():
    for name in list(sys.modules):
        if name in {"database", "market", "plugin_market"}:
            sys.modules.pop(name, None)


class MarketPluginLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        _clear_modules()
        import database
        import market
        import plugin_market
        from plugin_runtime import PluginRuntime

        self.db = database
        self.market = market
        self.pm = plugin_market
        await self.db.initialize()
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.runtime = PluginRuntime()
        self.store = plugin_market.PluginArtifactStore(
            Path(self.tmp.name) / "plugin-store", allowed_local_roots=[FIXTURE.parent]
        )
        self.modes: dict[str, str] = {}

        def command_factory(manifest, artifact):
            schemes = ",".join(scheme for scheme, _contract in manifest.owned_schemes)
            return [
                sys.executable, str(artifact), "--mode", self.modes.get(manifest.version, "normal"),
                "--identity", manifest.identity, "--version", manifest.version, "--schemes", schemes, "--tv-only",
            ]

        self.service = plugin_market.PluginMarketService(
            runtime=self.runtime,
            store=self.store,
            trust_policy=plugin_market.FixtureTrustPolicy({("org.waveflow", "fixture-key"): public}),
            command_factory=command_factory,
            os_name="linux",
            arch="x86_64",
        )

    async def asyncTearDown(self):
        await self.runtime.shutdown()
        if self.old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db
        _clear_modules()
        self.tmp.cleanup()

    async def test_domain_observer_sees_committed_install_disable_enable_and_uninstall(self):
        observed = []

        async def changed(identity):
            publisher, plugin_id = identity.split("/", 1)
            row = await self.db.get_plugin_installation(publisher, plugin_id)
            observed.append(row["lifecycle_state"] if row else "removed")

        self.service.lifecycle_changed = changed
        package = self.package()
        identity = "org.waveflow/fixture-multi-provider"
        await self.service.install_from_packages([package], identity)
        await self.service.disable(identity)
        await self.service.enable(identity)
        await self.service.uninstall(identity)
        self.assertEqual(observed, ["active", "disabled", "active", "removed"])

    def package(
        self,
        version="1.0.0",
        *,
        source_key="fixture-a",
        publisher="org.waveflow",
        schemes=("fixture-a", "fixture-b", "fixture-c"),
        artifact_path=FIXTURE,
        signature=True,
        sha256=None,
        os_name="linux",
        arch="x86_64",
    ):
        payload = artifact_path.read_bytes()
        digest = sha256 or hashlib.sha256(payload).hexdigest()
        signature_value = base64.b64encode(self.private.sign(payload)).decode("ascii") if signature else "invalid"
        manifest = {
            "manifest_version": 1,
            "publisher_id": publisher,
            "plugin_id": "fixture-multi-provider",
            "display_name": "Fixture Multi Provider",
            "version": version,
            "plugin_api_version": "1.0",
            "core_version_range": ">=0.1.0 <1.0.0",
            "provider_contracts": [
                {"contract": "tv_provider", "contract_version": "1.0", "features": ["resolve_stream"]},
            ],
            "owned_schemes": [{"scheme": value, "contract": "tv_provider"} for value in schemes],
            "capabilities": ["tv.resolve_stream"],
            "permissions": {},
            "runtime": {"type": "subprocess", "ipc": "stdio_framed_json_v1"},
            "artifacts": [{
                "os": os_name, "arch": arch, "runtime": "python", "entrypoint": "synthetic_plugin.py",
                "sha256": digest, "size_bytes": len(payload),
                "signature": {"algorithm": "ed25519", "key_id": "fixture-key", "value": signature_value},
            }],
            "dependencies": [],
            "state_schema_version": 1,
        }
        return {
            "schema_version": 1,
            "id": f"{source_key}::fixture-plugin",
            "original_id": "fixture-plugin",
            "name": "Fixture Plugin",
            "kind": "plugin_package",
            "package_type": "plugin_package",
            "version": version,
            "plugin_manifest": manifest,
            "artifact_references": [{"sha256": digest, "local_path": str(artifact_path)}],
            "market_source": {"source_key": source_key},
        }

    async def _wait_reconciliation(self, subsystem) -> None:
        """Wait for the lifecycle callback's durable/live projection to settle."""
        for _ in range(20):
            tasks = [
                *subsystem._lifecycle_reconcile_tasks.values(),
                *subsystem._ownership_reconcile_tasks.values(),
                *subsystem._critical_tasks,
            ]
            pending = [task for task in tasks if not task.done()]
            if not pending:
                await asyncio.sleep(0)
                if not any(
                    not task.done()
                    for task in [
                        *subsystem._lifecycle_reconcile_tasks.values(),
                        *subsystem._ownership_reconcile_tasks.values(),
                        *subsystem._critical_tasks,
                    ]
                ):
                    return
                continue
            await asyncio.gather(*(asyncio.shield(task) for task in pending))
        self.fail("Plugin lifecycle reconciliation did not become idle")

    @staticmethod
    def requirement(version_range=">=1.0.0 <2.0.0"):
        return {
            "plugin": "org.waveflow/fixture-multi-provider",
            "version_range": version_range,
            "contract": "tv_provider",
            "required_schemes": ["fixture-a"],
        }

    async def test_clean_schema_and_initialize_is_idempotent(self):
        await self.db.initialize()
        conn = sqlite3.connect(os.environ["WAVEFLOW_DB_PATH"])
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("plugin_installations", tables)
            self.assertIn("plugin_artifacts", tables)
            self.assertIn("plugin_dependency_artifacts", tables)
            self.assertIn("plugin_python_environments", tables)
            self.assertIn("plugin_environment_dependencies", tables)
            self.assertIn("market_packages_installed", tables)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(plugin_installations)")}
            self.assertIn("manifest_signature_json", columns)
        finally:
            conn.close()

    def test_market_discovery_distinguishes_plugin_and_content(self):
        plugin = self.market._normalize_package(self.package())
        self.assertEqual(plugin["package_type"], "plugin_package")
        self.assertTrue(plugin["plugin_installable"])
        self.assertFalse(plugin["previewable"])
        self.assertFalse(plugin["importable"])
        self.assertNotIn("artifact_references", self.market._package_card(plugin))
        content = self.market._normalize_package({"id": "content", "kind": "playlist"})
        self.assertEqual(content["package_type"], "content_package")
        self.assertTrue(content["importable"])
        with self.assertRaises(self.market.MarketError):
            self.market._normalize_package({"id": "bad", "kind": "playlist", "requires_plugins": [{}]})

    def test_candidate_selection_is_deterministic_and_conflicts_on_digest(self):
        packages = [self.package(source_key="z"), self.package(source_key="a")]
        first = self.pm.select_candidate(
            self.pm.candidates_from_packages(packages, os_name="linux", arch="x86_64"),
            "org.waveflow/fixture-multi-provider",
        )
        second = self.pm.select_candidate(
            self.pm.candidates_from_packages(reversed(packages), os_name="linux", arch="x86_64"),
            "org.waveflow/fixture-multi-provider",
        )
        self.assertEqual((first.source_key, first.artifact["sha256"]), (second.source_key, second.artifact["sha256"]))
        conflict = self.package(source_key="conflict", sha256="0" * 64)
        with self.assertRaises(self.pm.PluginError) as raised:
            self.pm.select_candidate(
                self.pm.candidates_from_packages([packages[0], conflict], os_name="linux", arch="x86_64"),
                "org.waveflow/fixture-multi-provider",
            )
        self.assertEqual(raised.exception.code, "PLUGIN_INCOMPATIBLE")
        self.assertEqual(
            self.pm.candidates_from_packages([self.package(os_name="windows")], os_name="linux", arch="x86_64"),
            [],
        )

    async def test_same_digest_conflicting_manifest_is_rejected(self):
        first = self.package(source_key="a")
        second = self.package(source_key="b")
        second["plugin_manifest"]["capabilities"] = []
        with self.assertRaises(self.pm.PluginError) as conflict:
            await self.service.install_from_packages(
                [first, second], "org.waveflow/fixture-multi-provider"
            )
        self.assertEqual(conflict.exception.code, "PLUGIN_INCOMPATIBLE")

    async def test_trust_digest_and_path_gates_cleanup_failed_staging(self):
        for package in (
            self.package(publisher="org.unknown"),
            self.package(signature=False),
            self.package(sha256="0" * 64),
        ):
            with self.subTest(publisher=package["plugin_manifest"]["publisher_id"], sha=package["plugin_manifest"]["artifacts"][0]["sha256"]):
                with self.assertRaises(self.pm.PluginError):
                    await self.service.install_from_packages([package], f"{package['plugin_manifest']['publisher_id']}/fixture-multi-provider")
        outside = Path(self.tmp.name) / "outside.py"
        shutil.copyfile(FIXTURE, outside)
        with self.assertRaises(self.pm.PluginError) as denied:
            await self.service.install_from_packages([self.package(artifact_path=outside)], "org.waveflow/fixture-multi-provider")
        self.assertEqual(denied.exception.code, "CAPABILITY_DENIED")
        self.assertEqual(list(self.store.staged_root.glob("*/*/*/*/artifact")), [])
        self.assertEqual(await self.db.list_plugin_installations(), [])

        self.modes["1.0.0"] = "health_fail"
        with self.assertRaises(self.pm.PluginError):
            await self.service.install_from_packages(
                [self.package()], "org.waveflow/fixture-multi-provider"
            )
        self.assertEqual(await self.db.list_plugin_installations(), [])
        self.assertEqual(list(self.store.installed_root.glob("*/*/*/*/artifact")), [])

    async def test_trusted_duplicate_wins_over_untrusted_same_digest_without_order_dependency(self):
        trusted = self.package(source_key="z-trusted")
        untrusted = self.package(source_key="a-untrusted")
        untrusted["plugin_manifest"]["artifacts"][0]["signature"]["key_id"] = "unknown-key"
        installed = await self.service.install_from_packages(
            [untrusted, trusted], "org.waveflow/fixture-multi-provider"
        )
        self.assertEqual(installed["source_key"], "z-trusted")

    async def test_install_multischeme_dependency_disable_enable_uninstall(self):
        requirement = self.requirement()
        await self.db.install_market_package_atomic(
            package_id="tv-content-fixture",
            market_url="fixture://market",
            title="TV Content Fixture",
            subscription_url="market://tv-content-fixture",
            channels=[{"name": "Fixture Channel", "url": "fixture-a://channel/one"}],
            installed_version="1.0.0",
            metadata_json=json.dumps({"requires_plugins": [requirement]}),
        )
        install_before = await self.db.get_market_install("tv-content-fixture")
        subscription_before = await self.db.get_subscription(install_before["installed_subscription_id"])
        channels_before = await self.db.get_channels(install_before["installed_subscription_id"])
        self.assertEqual((await self.service.installed_content_dependency_projection("tv-content-fixture"))["status"], "dependency_missing")

        installed = await self.service.install_from_packages([self.package()], "org.waveflow/fixture-multi-provider")
        self.assertEqual(installed["active_version"], "1.0.0")
        owners = {self.runtime.registry.route(scheme).instance_id for scheme in ("fixture-a", "fixture-b", "fixture-c")}
        self.assertEqual(len(owners), 1)
        self.assertEqual((await self.service.installed_content_dependency_projection("tv-content-fixture"))["status"], "ready")
        incompatible = await self.service.dependency_projection([self.requirement(">=2.0.0 <3.0.0")])
        self.assertEqual(incompatible["status"], "plugin_incompatible")

        await self.service.disable("org.waveflow/fixture-multi-provider")
        self.assertEqual((await self.service.installed_content_dependency_projection("tv-content-fixture"))["status"], "provider_unavailable")
        await self.service.enable("org.waveflow/fixture-multi-provider")
        self.assertEqual((await self.service.installed_content_dependency_projection("tv-content-fixture"))["status"], "ready")

        instance_id = self.service._active["org.waveflow/fixture-multi-provider"].instance_id
        self.assertTrue(await self.service.uninstall("org.waveflow/fixture-multi-provider"))
        self.assertNotIn(instance_id, self.runtime.registry.instances)
        install_after = await self.db.get_market_install("tv-content-fixture")
        self.assertEqual(install_after["installed_subscription_id"], install_before["installed_subscription_id"])
        self.assertEqual(await self.db.get_subscription(install_after["installed_subscription_id"]), subscription_before)
        self.assertEqual(await self.db.get_channels(install_after["installed_subscription_id"]), channels_before)
        self.assertEqual((await self.service.installed_content_dependency_projection("tv-content-fixture"))["status"], "dependency_missing")
        self.assertFalse((self.store.installed_root / "org.waveflow" / "fixture-multi-provider").exists())

    async def test_update_success_failure_and_persistence_rollback(self):
        await self.service.install_from_packages([self.package("1.0.0")], "org.waveflow/fixture-multi-provider")
        same = await self.service.install_from_packages([self.package("1.0.0")], "org.waveflow/fixture-multi-provider")
        self.assertEqual(same["active_version"], "1.0.0")
        await self.service.install_from_packages([self.package("1.1.0")], "org.waveflow/fixture-multi-provider")
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual(row["active_version"], "1.1.0")
        self.assertEqual(self.runtime.registry.route("fixture-a").manifest.version, "1.1.0")

        with self.assertRaises(self.pm.PluginError):
            await self.service.install_from_packages([self.package("1.0.0")], "org.waveflow/fixture-multi-provider")
        conn = sqlite3.connect(os.environ["WAVEFLOW_DB_PATH"])
        try:
            artifacts = conn.execute(
                "SELECT version, state FROM plugin_artifacts WHERE publisher_id='org.waveflow' AND plugin_id='fixture-multi-provider'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(artifacts, [("1.1.0", "active")])

        self.modes["1.2.0"] = "health_fail"
        with self.assertRaises(self.pm.PluginError):
            await self.service.install_from_packages([self.package("1.2.0")], "org.waveflow/fixture-multi-provider")
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual(row["active_version"], "1.1.0")
        self.assertEqual(row["candidate_version"], "")
        self.assertEqual(self.runtime.registry.route("fixture-a").manifest.version, "1.1.0")

    async def test_update_can_extend_bundle_scheme_set_without_rewriting_ownership(self):
        identity = "org.waveflow/fixture-multi-provider"
        v1 = self.package("1.0.0", schemes=("fixture-a", "fixture-b"))
        v2 = self.package("1.1.0", schemes=("fixture-a", "fixture-b", "fixture-c"))

        await self.service.install_from_packages([v1], identity)
        self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])

        # A failed replacement must roll back the newly added scheme too;
        # the old runtime must not become the owner of a scheme it never
        # declared.
        with mock.patch.object(self.db, "activate_plugin_candidate", return_value=False):
            with self.assertRaises(self.pm.PluginError):
                await self.service.install_from_packages([v2], identity)
        self.assertEqual(self.runtime.registry.route("fixture-a").manifest.version, "1.0.0")
        self.assertEqual(self.runtime.registry.route("fixture-b").manifest.version, "1.0.0")
        with self.assertRaises(self.pm.PluginError) as missing:
            self.runtime.registry.route("fixture-c")
        self.assertEqual(missing.exception.code, "SCHEME_UNOWNED")

        installed = await self.service.install_from_packages([v2], identity)

        self.assertEqual(installed["active_version"], "1.1.0")
        self.assertEqual(
            {self.runtime.registry.route(scheme).manifest.version
             for scheme in ("fixture-a", "fixture-b", "fixture-c")},
            {"1.1.0"},
        )
        self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])

        with mock.patch.object(self.db, "activate_plugin_candidate", return_value=False):
            with self.assertRaises(self.pm.PluginError):
                await self.service.install_from_packages([self.package("1.3.0")], "org.waveflow/fixture-multi-provider")
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual(row["active_version"], "1.1.0")
        self.assertEqual(self.runtime.registry.route("fixture-a").manifest.version, "1.1.0")

    async def test_post_commit_cancellation_converges_forward_and_keeps_artifact(self):
        from plugin_production import ProductionPluginSubsystem
        from provider_resolver import ProviderResolver

        identity = "org.waveflow/fixture-multi-provider"
        await self.service.install_from_packages([self.package("1.0.0")], identity)
        subsystem = ProductionPluginSubsystem(
            service=self.service,
            trust_policy=self.service.trust_policy,
            download_root=Path(self.tmp.name) / "downloads",
            http_client=None,
            provider_resolver=ProviderResolver(runtime=self.runtime),
            capability_gateway=object(),
        )
        await subsystem.set_ownership("fixture-a", "plugin", identity)
        original_activate = self.db.activate_plugin_candidate
        committed = asyncio.Event()
        release = asyncio.Event()

        async def commit_then_pause(**kwargs):
            result = await original_activate(**kwargs)
            committed.set()
            await release.wait()
            return result

        with mock.patch.object(self.db, "activate_plugin_candidate", new=commit_then_pause):
            update = asyncio.create_task(
                self.service.install_from_packages([self.package("2.0.0")], identity),
            )
            await committed.wait()
            update.cancel()
            update.cancel()
            await asyncio.sleep(0)
            self.assertFalse(update.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await update

        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual((row["active_version"], row["candidate_version"]), ("2.0.0", ""))
        self.assertEqual(self.runtime.registry.route("fixture-a").manifest.version, "2.0.0")
        self.assertEqual(self.service._active[identity].manifest.version, "2.0.0")
        self.assertTrue(Path(row["artifact_path"]).is_file())
        owner = next(item for item in await self.db.list_plugin_scheme_ownership()
                     if item["scheme"] == "fixture-a")
        self.assertEqual((owner["mode"], owner["plugin_identity"]), ("plugin", identity))
        await self._wait_reconciliation(subsystem)
        self.assertTrue(subsystem.provider_resolver.is_available("fixture-a"))
        await subsystem.set_ownership("fixture-a", "legacy")
        await subsystem.shutdown()

    async def test_post_commit_projection_failure_reconciles_without_candidate_cleanup(self):
        identity = "org.waveflow/fixture-multi-provider"
        await self.service.install_from_packages([self.package("1.0.0")], identity)
        original_projection = self.service._project_committed_activation
        attempts = 0

        async def fail_once(plugin_identity, instance):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("projection fault")
            return await original_projection(plugin_identity, instance)

        with mock.patch.object(self.service, "_project_committed_activation", new=fail_once):
            result = await self.service.install_from_packages([self.package("2.0.0")], identity)
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual(result["active_version"], "2.0.0")
        self.assertGreaterEqual(attempts, 2)
        self.assertEqual(self.runtime.registry.route("fixture-a").manifest.version, "2.0.0")
        self.assertEqual(self.service._active[identity].manifest.version, "2.0.0")
        self.assertTrue(Path(row["artifact_path"]).is_file())

    async def test_permanent_post_commit_failure_shutdown_is_bounded_and_recovers(self):
        from plugin_production import ProductionPluginSubsystem
        from provider_resolver import ProviderResolver

        identity = "org.waveflow/fixture-multi-provider"
        await self.service.install_from_packages([self.package("1.0.0")], identity)
        subsystem = ProductionPluginSubsystem(
            service=self.service,
            trust_policy=self.service.trust_policy,
            download_root=Path(self.tmp.name) / "downloads",
            http_client=None,
            provider_resolver=ProviderResolver(runtime=self.runtime),
            capability_gateway=object(),
        )
        await subsystem.set_ownership("fixture-a", "plugin", identity)
        projection_failed = asyncio.Event()

        async def fail_projection(_plugin_identity, _instance):
            projection_failed.set()
            raise RuntimeError("permanent projection fault")

        with mock.patch.object(self.service, "_project_committed_activation", new=fail_projection):
            update = asyncio.create_task(
                self.service.install_from_packages([self.package("2.0.0")], identity),
            )
            await projection_failed.wait()
            await asyncio.wait_for(subsystem.shutdown(), timeout=1.0)
            with self.assertRaises(asyncio.CancelledError):
                await update

        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual((row["active_version"], row["candidate_version"]), ("2.0.0", ""))
        self.assertTrue(Path(row["artifact_path"]).is_file())

        from plugin_runtime import PluginRuntime

        recovered_runtime = PluginRuntime()
        self.runtime = recovered_runtime
        recovered = self.pm.PluginMarketService(
            runtime=recovered_runtime,
            store=self.store,
            trust_policy=self.service.trust_policy,
            command_factory=self.service.command_factory,
            os_name="linux",
            arch="x86_64",
        )
        results = await recovered.recover_enabled()
        self.assertEqual(results, [{"plugin": identity, "status": "active"}])
        self.assertEqual(recovered_runtime.registry.route("fixture-a").manifest.version, "2.0.0")

    async def test_commit_then_repository_error_uses_durable_activation_boundary(self):
        identity = "org.waveflow/fixture-multi-provider"
        await self.service.install_from_packages([self.package("1.0.0")], identity)
        original_activate = self.db.activate_plugin_candidate

        async def commit_then_error(**kwargs):
            await original_activate(**kwargs)
            raise RuntimeError("repository return fault")

        with mock.patch.object(self.db, "activate_plugin_candidate", new=commit_then_error):
            result = await self.service.install_from_packages([self.package("2.0.0")], identity)
        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual(result["active_version"], "2.0.0")
        self.assertEqual(self.runtime.registry.route("fixture-a").manifest.version, "2.0.0")
        self.assertTrue(Path(row["artifact_path"]).is_file())

    async def test_plugin_owned_update_failure_keeps_canonical_owner_and_old_runtime(self):
        from plugin_production import ProductionPluginSubsystem
        from provider_resolver import ProviderResolver

        await self.service.install_from_packages([self.package("1.0.0")], "org.waveflow/fixture-multi-provider")
        subsystem = ProductionPluginSubsystem(
            service=self.service,
            trust_policy=self.service.trust_policy,
            download_root=Path(self.tmp.name) / "downloads",
            http_client=None,
            provider_resolver=ProviderResolver(runtime=self.runtime),
            capability_gateway=object(),
        )
        try:
            await subsystem.set_ownership("fixture-a", "plugin", "org.waveflow/fixture-multi-provider")
            self.modes["1.1.0"] = "health_fail"
            with self.assertRaises(self.pm.PluginError):
                await self.service.install_from_packages(
                    [self.package("1.1.0")], "org.waveflow/fixture-multi-provider",
                )
            row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
            owner = next(item for item in await self.db.list_plugin_scheme_ownership()
                         if item["scheme"] == "fixture-a")
            self.assertEqual(row["active_version"], "1.0.0")
            self.assertEqual(self.runtime.registry.route("fixture-a").manifest.version, "1.0.0")
            self.assertEqual((owner["mode"], owner["plugin_identity"]),
                             ("plugin", "org.waveflow/fixture-multi-provider"))
            self.assertEqual(subsystem.provider_resolver.mode("fixture-a"), "plugin")
        finally:
            await subsystem.set_ownership("fixture-a", "legacy")

    async def test_scheme_conflict_is_atomic(self):
        await self.service.install_from_packages([self.package(schemes=("occupied",))], "org.waveflow/fixture-multi-provider")
        second = self.package(publisher="org.waveflow", schemes=("new-a", "occupied"))
        second["plugin_manifest"]["plugin_id"] = "second-provider"
        identity = "org.waveflow/second-provider"
        with self.assertRaises(self.pm.PluginError) as conflict:
            await self.service.install_from_packages([second], identity)
        self.assertEqual(conflict.exception.code, "SCHEME_CONFLICT")
        self.assertIs(self.runtime.registry.route("occupied"), self.service._active["org.waveflow/fixture-multi-provider"])
        with self.assertRaises(self.pm.PluginError):
            self.runtime.registry.route("new-a")
        self.assertIsNone(await self.db.get_plugin_installation("org.waveflow", "second-provider"))

    async def test_restart_recovery_isolated_and_orphan_cleanup(self):
        await self.service.install_from_packages([self.package()], "org.waveflow/fixture-multi-provider")
        row_before = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        interrupted = self.package("1.1.0")
        candidate = self.pm.candidates_from_packages([interrupted], os_name="linux", arch="x86_64")[0]
        staged, trust_state = self.store.stage(candidate, self.service.trust_policy)
        promoted = self.store.promote(candidate, staged)
        manifest_json = json.dumps(candidate.manifest.raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        await self.db.begin_plugin_candidate(
            publisher_id="org.waveflow", plugin_id="fixture-multi-provider", version="1.1.0",
            trust_state=trust_state, source_key=candidate.source_key, source_package_id=candidate.package_id,
            manifest_json=manifest_json, manifest_sha256=hashlib.sha256(manifest_json.encode()).hexdigest(),
            artifact_sha256=candidate.artifact["sha256"], artifact_path=str(promoted),
            runtime_type="python", entrypoint="synthetic_plugin.py", platform_os="linux", platform_arch="x86_64",
        )
        await self.runtime.shutdown()

        from plugin_runtime import PluginRuntime
        new_runtime = PluginRuntime()
        self.runtime = new_runtime
        recovered = self.pm.PluginMarketService(
            runtime=new_runtime,
            store=self.store,
            trust_policy=self.service.trust_policy,
            command_factory=self.service.command_factory,
            os_name="linux", arch="x86_64",
        )
        results = await recovered.recover_enabled()
        self.assertEqual(results, [{"plugin": "org.waveflow/fixture-multi-provider", "status": "active"}])
        self.assertEqual(new_runtime.registry.route("fixture-a").manifest.version, "1.0.0")
        recovered_row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        self.assertEqual(recovered_row["candidate_version"], "")
        self.assertEqual(recovered_row["active_version"], row_before["active_version"])
        self.assertFalse(promoted.exists())

        row = await self.db.get_plugin_installation("org.waveflow", "fixture-multi-provider")
        Path(row["artifact_path"]).write_text("corrupt", encoding="utf-8")
        await new_runtime.shutdown()
        third_runtime = PluginRuntime()
        self.runtime = third_runtime
        broken = self.pm.PluginMarketService(
            runtime=third_runtime, store=self.store, trust_policy=self.service.trust_policy,
            command_factory=self.service.command_factory, os_name="linux", arch="x86_64",
        )
        results = await broken.recover_enabled()
        self.assertEqual(results[0]["status"], "unavailable")

        orphan = self.store.staged_root / "org.waveflow" / "orphan" / "1.0.0" / ("f" * 64)
        orphan.mkdir(parents=True)
        (orphan / "artifact").write_text("orphan", encoding="utf-8")
        self.assertEqual(self.store.cleanup_orphan_staging([]), 1)
        installed_orphan = self.store.installed_root / "org.waveflow" / "orphan" / "1.0.0" / ("e" * 64)
        installed_orphan.mkdir(parents=True)
        (installed_orphan / "artifact").write_text("orphan", encoding="utf-8")
        self.assertEqual(self.store.cleanup_orphan_installed([row["artifact_path"]]), 1)


if __name__ == "__main__":
    unittest.main()
