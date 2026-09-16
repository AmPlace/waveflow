from __future__ import annotations

import importlib
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx


TARGETS = (
    ("jstv", "org.waveflow/jstv", "jstv://jsws"),
    ("fjtv", "org.waveflow/fjtv", "fjtv://fjzh"),
    ("nd0593tv", "org.waveflow/nd0593tv", "nd0593tv://news"),
    ("gzstv", "org.waveflow/gzstv", "gzstv://ch01"),
)
TARGET_IDENTITIES = {identity for _scheme, identity, _reference in TARGETS}
TARGET_SCHEMES = {scheme for scheme, _identity, _reference in TARGETS}
ROLLOUT2_IDENTITIES = {
    "org.waveflow/nowtv", "org.waveflow/nmtv", "org.waveflow/sdtv",
}
ROLLOUT2_SCHEMES = {identity.rsplit("/", 1)[1] for identity in ROLLOUT2_IDENTITIES}
ROLLOUT_IDENTITIES = TARGET_IDENTITIES | ROLLOUT2_IDENTITIES
ROLLOUT_SCHEMES = TARGET_SCHEMES | ROLLOUT2_SCHEMES
OFFICIAL_IDENTITIES = TARGET_IDENTITIES | {
    "org.waveflow/nowtv", "org.waveflow/nmtv", "org.waveflow/sdtv",
}
STREAMS = {
    "fjtv": "https://media.example/fjtv/live.m3u8",
    "nd0593tv": "https://media.example/nd0593tv/live.m3u8",
    "gzstv": "https://media.example/gzstv/live.m3u8",
}


def _clear_modules() -> None:
    for name in (
        "database", "market", "plugin_market", "plugin_permissions", "plugin_production",
        "official_plugin_distribution", "routers.plugins",
    ):
        sys.modules.pop(name, None)


class ProductionRolloutTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = {
            name: os.environ.get(name)
            for name in (
                "WAVEFLOW_DB_PATH", "WAVEFLOW_MODE", "WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP",
                "WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT",
            )
        }
        os.environ["WAVEFLOW_DB_PATH"] = str(Path(self.tmp.name) / "waveflow.db")
        os.environ["WAVEFLOW_MODE"] = "nas"
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "1"
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_ROLLOUT"] = "1"
        _clear_modules()
        self.db = importlib.import_module("database")
        await self.db.initialize()
        self.requests: list[httpx.Request] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.host == "live.fjtv.net":
                return httpx.Response(200, json=[{"m3u8": STREAMS["fjtv"]}])
            if request.url.host == "app.0593tv.cn":
                return httpx.Response(200, json={"code": 200, "data": {"link": STREAMS["nd0593tv"]}})
            if request.url.host == "api.gzstv.com":
                return httpx.Response(200, json={"stream_url": STREAMS["gzstv"]})
            return httpx.Response(503, text="offline")

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.legacy = mock.AsyncMock(return_value={"url": "https://legacy.example/live.m3u8"})
        self.subsystems = []
        self.safe = mock.patch("plugin_capabilities.assert_safe_target_url", new=mock.AsyncMock())
        self.safe.start()

    async def asyncTearDown(self):
        for subsystem in reversed(self.subsystems):
            await subsystem.shutdown()
        self.safe.stop()
        await self.client.aclose()
        for name, value in self.old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _clear_modules()
        self.tmp.cleanup()

    async def _subsystem(self):
        from plugin_production import ProductionPluginSubsystem

        subsystem = await ProductionPluginSubsystem.create(
            root=Path(self.tmp.name) / "plugin-store", http_client=self.client,
        )
        subsystem.provider_resolver.legacy_resolver = self.legacy
        self.subsystems.append(subsystem)
        return subsystem

    async def _restart(self, subsystem):
        await subsystem.shutdown()
        self.subsystems.remove(subsystem)
        restarted = await self._subsystem()
        results = await restarted.startup()
        return restarted, results

    async def _wait_reconciliation(self, subsystem):
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
        self.fail("Plugin reconciliation did not become idle")

    async def _assert_plugin_routing(self, subsystem, suffix: str) -> None:
        for scheme, identity, reference in TARGETS:
            result = await subsystem.provider_resolver.resolve(reference, self.client)
            self.assertEqual(result["stream_descriptor_version"], "1.0", f"{scheme}:{suffix}")
            self.assertEqual(
                subsystem.service.runtime.registry.route(scheme).manifest.identity,
                identity,
            )

    async def _assert_destructive_operation_is_serialized(self, subsystem, operation) -> None:
        identity = "org.waveflow/jstv"
        await subsystem.set_ownership("jstv", "legacy")
        entered = asyncio.Event()
        release = asyncio.Event()
        original_preflight = subsystem._ownership_preflight

        async def paused_preflight(scheme, plugin_identity=""):
            result = await original_preflight(scheme, plugin_identity)
            entered.set()
            await release.wait()
            return result

        subsystem._ownership_preflight = paused_preflight
        owner_task = asyncio.create_task(
            subsystem.set_ownership("jstv", "plugin", identity),
        )
        operation_started = asyncio.Event()

        async def run_operation():
            operation_started.set()
            return await operation(identity)

        operation_task = None
        try:
            await entered.wait()
            operation_task = asyncio.create_task(run_operation())
            await operation_started.wait()
            # The ownership task holds the same canonical identity lock while
            # its preflight is paused; the destructive task must not pass its
            # guard against the old durable row.
            self.assertFalse(operation_task.done())
            release.set()
            await owner_task
            with self.assertRaises(Exception) as blocked:
                await operation_task
            self.assertEqual(blocked.exception.code, "SCHEME_CONFLICT")
            row = next(item for item in await self.db.list_plugin_scheme_ownership() if item["scheme"] == "jstv")
            self.assertEqual((row["mode"], row["plugin_identity"]), ("plugin", identity))
        finally:
            release.set()
            if not owner_task.done():
                await owner_task
            if operation_task is not None and not operation_task.done():
                await operation_task
            subsystem._ownership_preflight = original_preflight

    async def test_signed_fresh_rollout_restart_offline_routing_and_reversible_ownership(self):
        from adapters import _ADAPTER_REGISTRY

        subsystem = await self._subsystem()
        startup = await subsystem.startup()
        self.assertEqual(
            {item["plugin"] for item in startup if item.get("bootstrap") == "installed"},
            OFFICIAL_IDENTITIES,
        )
        self.assertEqual(
            {item["plugin"] for item in startup if item.get("rollout") == "plugin"},
            ROLLOUT_IDENTITIES,
        )
        installations = await self.db.list_plugin_installations()
        self.assertEqual(
            {f"{row['publisher_id']}/{row['plugin_id']}" for row in installations},
            OFFICIAL_IDENTITIES,
        )
        self.assertTrue(all(
            row["enabled"] and row["lifecycle_state"] == "active"
            and row["trust_state"] == "official" and row["source_key"] == "official"
            for row in installations
        ))
        owners = {row["scheme"]: row for row in await self.db.list_plugin_scheme_ownership()}
        self.assertEqual(set(owners), ROLLOUT_SCHEMES)
        self.assertTrue(all(
            owners[scheme]["mode"] == "plugin" and owners[scheme]["plugin_identity"] == identity
            for scheme, identity, _reference in TARGETS
        ))
        self.assertEqual(
            {scheme for scheme in _ADAPTER_REGISTRY if subsystem.provider_resolver.mode(scheme) == "plugin"},
            ROLLOUT_SCHEMES,
        )
        self.assertEqual(len(_ADAPTER_REGISTRY), 58)

        router = importlib.import_module("routers.plugins")
        projections = [await router._plugin_projection(row) for row in installations]
        for item in projections:
            expected_mode = "plugin" if item["plugin"] in ROLLOUT_IDENTITIES else "legacy"
            self.assertEqual(item["ownership"], [{
                "scheme": item["owned_schemes"][0], "mode": expected_mode,
                "plugin": item["plugin"] if expected_mode == "plugin" else "",
            }])
        self.assertTrue(all(not item["permissions"]["pending"] for item in projections))
        self.assertTrue(all(
            item["permissions"]["approved"] == item["permissions"]["requested"] for item in projections
        ))

        await self._assert_plugin_routing(subsystem, "fresh")
        self.legacy.assert_not_awaited()
        subsystem, recovered = await self._restart(subsystem)
        self.assertEqual(
            {item["plugin"] for item in recovered if item.get("status") == "active"},
            OFFICIAL_IDENTITIES,
        )
        self.assertFalse(any(item.get("rollout") == "plugin" for item in recovered))
        await self._assert_plugin_routing(subsystem, "restart")
        self.legacy.assert_not_awaited()

        market = importlib.import_module("market")
        with mock.patch.object(
            market, "safe_http_fetch", new=mock.AsyncMock(side_effect=market.MarketError("offline", 502)),
        ):
            refreshed = await market.refresh_market()
        source = next(item for item in refreshed["source_results"] if item["source_key"] == "official")
        self.assertEqual(source["status"], "bundled")
        await self._assert_plugin_routing(subsystem, "offline")

        for scheme, identity, reference in TARGETS:
            self.assertEqual((await subsystem.set_ownership(scheme, "legacy"))["mode"], "legacy")
            self.assertEqual(
                (await subsystem.provider_resolver.resolve(reference, self.client))["url"],
                "https://legacy.example/live.m3u8",
            )
            self.assertEqual(
                (await subsystem.set_ownership(scheme, "plugin", identity))["mode"], "plugin",
            )
            self.assertEqual(
                (await subsystem.provider_resolver.resolve(reference, self.client))["stream_descriptor_version"],
                "1.0",
            )
        self.assertEqual(self.legacy.await_count, 4)
        final = {row["scheme"]: row for row in await self.db.list_plugin_scheme_ownership()}
        self.assertTrue(all(final[scheme]["mode"] == "plugin" for scheme in TARGET_SCHEMES))
        for scheme in TARGET_SCHEMES:
            self.assertTrue((Path("backend/adapters") / f"{scheme}.py").is_file())

    async def test_plugin_failure_has_no_legacy_fallback_and_guards_require_explicit_rollback(self):
        from plugin_runtime import PluginError

        subsystem = await self._subsystem()
        await subsystem.startup()
        for operation in (subsystem.disable, subsystem.uninstall):
            with self.assertRaises(PluginError) as blocked:
                await operation("org.waveflow/jstv")
            self.assertEqual(blocked.exception.code, "SCHEME_CONFLICT")
        with self.assertRaises(PluginError) as revoke:
            await subsystem.revoke_permission("org.waveflow/fjtv", "network.direct", "test")
        self.assertEqual(revoke.exception.code, "SCHEME_CONFLICT")

        instance = subsystem.service.runtime.registry.route("jstv")
        process = instance.process.process
        self.assertIsNotNone(process)
        process.kill()
        await instance.process._wait_task
        await self._wait_reconciliation(subsystem)
        self.assertEqual(instance.state.value, "UNHEALTHY")
        crashed_row = await self.db.get_plugin_installation("org.waveflow", "jstv")
        self.assertEqual(crashed_row["lifecycle_state"], "unavailable")
        self.assertTrue(crashed_row["enabled"])
        self.assertIn("PLUGIN_CRASHED", crashed_row["last_error"])
        router = importlib.import_module("routers.plugins")
        crashed_projection = await router._plugin_projection(
            crashed_row, runtime=subsystem.service.runtime,
        )
        self.assertFalse(crashed_projection["runtime_available"])
        with self.assertRaises(PluginError):
            await subsystem.provider_resolver.resolve("jstv://jsws", self.client)
        self.legacy.assert_not_awaited()
        await subsystem.set_ownership("jstv", "legacy")
        self.assertEqual(
            (await subsystem.provider_resolver.resolve("jstv://jsws", self.client))["url"],
            "https://legacy.example/live.m3u8",
        )
        subsystem, _recovered = await self._restart(subsystem)
        self.assertEqual(subsystem.provider_resolver.mode("jstv"), "legacy")
        await subsystem.set_ownership("jstv", "plugin", "org.waveflow/jstv")
        self.assertEqual(
            (await subsystem.provider_resolver.resolve("jstv://jsws", self.client))["stream_descriptor_version"],
            "1.0",
        )

    async def test_intentional_disable_does_not_persist_crash_state(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        await subsystem.set_ownership("jstv", "legacy")
        await subsystem.disable("org.waveflow/jstv")
        row = await self.db.get_plugin_installation("org.waveflow", "jstv")
        self.assertEqual(row["lifecycle_state"], "disabled")
        self.assertNotIn("PLUGIN_CRASHED", row["last_error"])

    async def test_ownership_and_destructive_lifecycle_share_identity_lock(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        await self._assert_destructive_operation_is_serialized(subsystem, subsystem.disable)
        await self._assert_destructive_operation_is_serialized(subsystem, subsystem.uninstall)

    async def test_ownership_cancellation_reconciles_db_and_resolver(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        await subsystem.set_ownership("jstv", "legacy")
        original_write = self.db.set_plugin_scheme_ownership
        committed = asyncio.Event()
        release = asyncio.Event()

        async def delayed_write(scheme, mode, plugin_identity=""):
            row = await original_write(scheme, mode, plugin_identity)
            committed.set()
            await release.wait()
            return row

        with mock.patch.object(self.db, "set_plugin_scheme_ownership", new=delayed_write):
            task = asyncio.create_task(
                subsystem.set_ownership("jstv", "plugin", "org.waveflow/jstv"),
            )
            await committed.wait()
            task.cancel()
            task.cancel()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        row = next(item for item in await self.db.list_plugin_scheme_ownership() if item["scheme"] == "jstv")
        self.assertEqual((row["mode"], row["plugin_identity"]), ("plugin", "org.waveflow/jstv"))
        self.assertEqual(subsystem.provider_resolver.mode("jstv"), "plugin")
        await subsystem.set_ownership("jstv", "legacy")

    async def test_ownership_resolver_failure_fails_closed_then_reconciles_durable_row(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        await subsystem.set_ownership("jstv", "legacy")
        original_set_mode = subsystem.provider_resolver.set_mode
        failures = 0

        def fail_plugin_mode(scheme, mode, plugin_identity="", **kwargs):
            nonlocal failures
            if mode == "plugin" and failures == 0:
                failures += 1
                raise RuntimeError("resolver failure")
            return original_set_mode(scheme, mode, plugin_identity, **kwargs)

        with mock.patch.object(subsystem.provider_resolver, "set_mode", side_effect=fail_plugin_mode), \
                mock.patch.object(
                    self.db, "set_plugin_scheme_ownership",
                    wraps=self.db.set_plugin_scheme_ownership,
                ) as ownership_write:
            with self.assertRaises(Exception) as unavailable:
                await subsystem.set_ownership("jstv", "plugin", "org.waveflow/jstv")
            self.assertEqual(unavailable.exception.code, "PLUGIN_UNAVAILABLE")
            self.assertEqual(ownership_write.await_count, 1)
            self.assertFalse(subsystem.provider_resolver.is_available("jstv"))
            with self.assertRaises(Exception) as routed:
                await subsystem.provider_resolver.resolve("jstv://jsws", self.client)
            self.assertEqual(routed.exception.code, "PLUGIN_UNAVAILABLE")
            self.legacy.assert_not_awaited()
        await self._wait_reconciliation(subsystem)
        row = next(item for item in await self.db.list_plugin_scheme_ownership() if item["scheme"] == "jstv")
        self.assertEqual((row["mode"], row["plugin_identity"]), ("plugin", "org.waveflow/jstv"))
        self.assertEqual(subsystem.provider_resolver.mode("jstv"), "plugin")
        self.assertTrue(subsystem.provider_resolver.is_available("jstv"))
        await subsystem.set_ownership("jstv", "legacy")

    async def test_ownership_read_failure_stays_fail_closed_then_restores_durable_legacy(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        await subsystem.set_ownership("jstv", "legacy")
        original_read = subsystem._ownership_row
        failures = 0

        async def fail_once(scheme):
            nonlocal failures
            if failures == 0:
                failures += 1
                raise RuntimeError("ownership DB unavailable")
            return await original_read(scheme)

        with mock.patch.object(subsystem, "_ownership_row", new=fail_once):
            with self.assertRaises(Exception) as unavailable:
                await subsystem.set_ownership("jstv", "plugin", "org.waveflow/jstv")
            self.assertEqual(unavailable.exception.code, "PLUGIN_UNAVAILABLE")
            self.assertFalse(subsystem.provider_resolver.is_available("jstv"))
            with self.assertRaises(Exception) as routed:
                await subsystem.provider_resolver.resolve("jstv://jsws", self.client)
            self.assertEqual(routed.exception.code, "PLUGIN_UNAVAILABLE")
            self.legacy.assert_not_awaited()
        await self._wait_reconciliation(subsystem)
        owner = next(row for row in await self.db.list_plugin_scheme_ownership() if row["scheme"] == "jstv")
        self.assertEqual((owner["mode"], owner["plugin_identity"]), ("legacy", ""))
        self.assertEqual(subsystem.provider_resolver.mode("jstv"), "legacy")
        self.assertTrue(subsystem.provider_resolver.is_available("jstv"))

    async def test_failed_plugin_preflight_reprojects_durable_legacy_immediately(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        await subsystem.set_ownership("jstv", "legacy")
        await subsystem.disable("org.waveflow/jstv")

        with self.assertRaises(Exception) as blocked:
            await subsystem.set_ownership("jstv", "plugin", "org.waveflow/jstv")
        self.assertIn(blocked.exception.code, {"PLUGIN_UNAVAILABLE", "SCHEME_UNOWNED"})
        owner = next(row for row in await self.db.list_plugin_scheme_ownership()
                     if row["scheme"] == "jstv")
        self.assertEqual((owner["mode"], owner["plugin_identity"]), ("legacy", ""))
        self.assertEqual(subsystem.provider_resolver.mode("jstv"), "legacy")
        self.assertTrue(subsystem.provider_resolver.is_available("jstv"))
        resolved = await subsystem.provider_resolver.resolve("jstv://jsws", self.client)
        self.assertEqual(resolved["url"], "https://legacy.example/live.m3u8")
        self.legacy.assert_awaited_once()

    async def test_failed_plugin_preflight_keeps_durable_plugin_explicitly_unavailable(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        instance = subsystem.service.runtime.registry.route("jstv")
        instance.process.process.kill()
        await instance.process._wait_task
        await self._wait_reconciliation(subsystem)

        with self.assertRaises(Exception):
            await subsystem.set_ownership("jstv", "plugin", "org.waveflow/jstv")
        owner = next(row for row in await self.db.list_plugin_scheme_ownership()
                     if row["scheme"] == "jstv")
        self.assertEqual((owner["mode"], owner["plugin_identity"]),
                         ("plugin", "org.waveflow/jstv"))
        self.assertEqual(subsystem.provider_resolver.mode("jstv"), "plugin")
        self.assertFalse(subsystem.provider_resolver.is_available("jstv"))
        with self.assertRaises(Exception) as unavailable:
            await subsystem.provider_resolver.resolve("jstv://jsws", self.client)
        self.assertEqual(unavailable.exception.code, "PLUGIN_UNAVAILABLE")
        self.legacy.assert_not_awaited()

    async def test_ownership_commit_then_error_reloads_durable_desired_state(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        await subsystem.set_ownership("jstv", "legacy")
        original_write = self.db.set_plugin_scheme_ownership

        async def commit_then_error(scheme, mode, plugin_identity=""):
            await original_write(scheme, mode, plugin_identity)
            raise RuntimeError("post-commit transport failure")

        with mock.patch.object(self.db, "set_plugin_scheme_ownership", new=commit_then_error):
            result = await subsystem.set_ownership("jstv", "plugin", "org.waveflow/jstv")
        self.assertEqual((result["mode"], result["plugin"]), ("plugin", "org.waveflow/jstv"))
        self.assertEqual(subsystem.provider_resolver.mode("jstv"), "plugin")
        self.assertTrue(subsystem.provider_resolver.is_available("jstv"))
        await subsystem.set_ownership("jstv", "legacy")

    async def test_repeated_cancellation_during_reconciliation_cannot_expose_legacy(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        await subsystem.set_ownership("jstv", "legacy")
        entered = asyncio.Event()
        release = asyncio.Event()
        original_project = subsystem._project_durable_ownership_locked

        async def paused_project(scheme):
            entered.set()
            await release.wait()
            return await original_project(scheme)

        subsystem._project_durable_ownership_locked = paused_project
        task = asyncio.create_task(
            subsystem.set_ownership("jstv", "plugin", "org.waveflow/jstv"),
        )
        try:
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            self.assertFalse(subsystem.provider_resolver.is_available("jstv"))
            with self.assertRaises(Exception) as routed:
                await subsystem.provider_resolver.resolve("jstv://jsws", self.client)
            self.assertEqual(routed.exception.code, "PLUGIN_UNAVAILABLE")
            self.legacy.assert_not_awaited()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            subsystem._project_durable_ownership_locked = original_project
        row = next(item for item in await self.db.list_plugin_scheme_ownership() if item["scheme"] == "jstv")
        self.assertEqual((row["mode"], row["plugin_identity"]), ("plugin", "org.waveflow/jstv"))
        self.assertEqual(subsystem.provider_resolver.mode("jstv"), "plugin")
        self.assertTrue(subsystem.provider_resolver.is_available("jstv"))
        await subsystem.set_ownership("jstv", "legacy")

    async def test_cancelled_caller_observes_sanitized_critical_task_failure_in_logs(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        entered = asyncio.Event()
        release = asyncio.Event()
        original = subsystem._set_ownership_serialized

        async def fail_after_caller_leaves(*_args, **_kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("/private/secret/path")

        subsystem._set_ownership_serialized = fail_after_caller_leaves
        try:
            with self.assertLogs("plugin_production", level="ERROR") as captured:
                caller = asyncio.create_task(subsystem.set_ownership("jstv", "legacy"))
                await entered.wait()
                caller.cancel()
                await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
                await asyncio.sleep(0)
            self.assertTrue(any("operation=ownership_transition" in value for value in captured.output))
            self.assertTrue(any("target=jstv" in value for value in captured.output))
            self.assertTrue(any("reconciliation_scheduled=false" in value for value in captured.output))
            self.assertFalse(any("/private/secret/path" in value for value in captured.output))
        finally:
            release.set()
            subsystem._set_ownership_serialized = original

    async def test_crash_restart_converges_runtime_database_settings_and_resolver(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        instance = subsystem.service.runtime.registry.route("jstv")
        instance.process.process.kill()
        await instance.process._wait_task
        await self._wait_reconciliation(subsystem)
        crashed = await self.db.get_plugin_installation("org.waveflow", "jstv")
        self.assertEqual(crashed["lifecycle_state"], "unavailable")
        self.assertFalse(subsystem.provider_resolver.is_available("jstv"))

        subsystem.service.runtime.sleep = lambda _delay: asyncio.sleep(0)
        await subsystem.service.runtime.restart(instance)
        await self._wait_reconciliation(subsystem)
        recovered = await self.db.get_plugin_installation("org.waveflow", "jstv")
        self.assertEqual((recovered["lifecycle_state"], recovered["last_error"]), ("active", ""))
        router = importlib.import_module("routers.plugins")
        projection = await router._plugin_projection(recovered, runtime=subsystem.service.runtime)
        self.assertTrue(projection["runtime_available"])
        self.assertTrue(subsystem.provider_resolver.is_available("jstv"))
        resolved = await subsystem.provider_resolver.resolve("jstv://jsws", self.client)
        self.assertEqual(resolved["stream_descriptor_version"], "1.0")
        self.legacy.assert_not_awaited()

    async def test_crash_persistence_failure_is_retried_and_sanitized(self):
        subsystem = await self._subsystem()
        await subsystem.startup()
        instance = subsystem.service.runtime.registry.route("jstv")
        original_set_enabled = self.db.set_plugin_enabled
        attempts = 0

        async def flaky_set_enabled(publisher, plugin_id, enabled, *, lifecycle_state, error=""):
            nonlocal attempts
            if plugin_id == "jstv" and lifecycle_state == "unavailable":
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("/private/path/should-not-be-logged")
            return await original_set_enabled(
                publisher, plugin_id, enabled, lifecycle_state=lifecycle_state, error=error,
            )

        with self.assertLogs("plugin_production", level="WARNING") as captured, mock.patch.object(
            self.db, "set_plugin_enabled", new=flaky_set_enabled,
        ):
            instance.process.process.kill()
            await instance.process._wait_task
            await self._wait_reconciliation(subsystem)
        self.assertGreaterEqual(attempts, 2)
        self.assertTrue(any("will retry" in message for message in captured.output))
        self.assertFalse(any("/private/path" in message for message in captured.output))
        row = await self.db.get_plugin_installation("org.waveflow", "jstv")
        self.assertEqual(row["lifecycle_state"], "unavailable")

    async def test_missing_installation_never_writes_owner_and_desktop_stays_legacy(self):
        subsystem = await self._subsystem()
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "0"
        blocked = await subsystem.rollout_official_plugins()
        self.assertEqual({item["plugin"] for item in blocked}, ROLLOUT_IDENTITIES)
        self.assertTrue(all(item["rollout"] == "blocked" for item in blocked))
        self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])
        await subsystem.shutdown()
        self.subsystems.remove(subsystem)

        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "1"
        os.environ["WAVEFLOW_MODE"] = "desktop"
        desktop = await self._subsystem()
        startup = await desktop.startup()
        self.assertEqual(
            {item["plugin"] for item in startup if item.get("bootstrap") == "installed"},
            OFFICIAL_IDENTITIES,
        )
        self.assertFalse(any(item.get("rollout") for item in startup))
        self.assertEqual(await self.db.list_plugin_scheme_ownership(), [])
        self.assertTrue(all(desktop.provider_resolver.mode(scheme) == "legacy" for scheme in TARGET_SCHEMES))


if __name__ == "__main__":
    unittest.main()
