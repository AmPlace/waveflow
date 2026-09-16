import asyncio
import importlib
import inspect
import os
import sys
import tempfile
import unittest
from unittest import mock

import httpx


def _clear_modules():
    for name in list(sys.modules):
        if name in {
            "automation",
            "database",
            "epg",
            "epg_maintenance",
            "iptv_logical_gc",
            "epg_preference_evidence",
            "epg_source_management",
            "epg_source_model",
            "epg_tasks",
            "market",
            "market_tasks",
        }:
            sys.modules.pop(name, None)


class EpgAutomationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "waveflow.db")
        _clear_modules()
        self.db = importlib.import_module("database")
        self.automation = importlib.import_module("automation")
        self.epg = importlib.import_module("epg")
        self.epg_tasks = importlib.import_module("epg_tasks")
        await self.db.initialize()
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(500, content=b"fixture failure")
            )
        )
        self.services = []
        self._source_counter = 0

    async def asyncTearDown(self):
        for service in reversed(self.services):
            await service.stop(timeout_seconds=0.2)
        await self.client.aclose()
        if self._old_db is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self._old_db
        _clear_modules()
        self._tmpdir.cleanup()

    async def _source(self, name="Guide", *, enabled=True, url=None):
        source_id = await self.db.add_epg_source(
            name,
            url or f"https://guide.test/{name}.xml",
        )
        if not enabled:
            await self.db.update_epg_source(source_id, enabled=0)
        return await self.db.get_epg_source(source_id)

    def _service(self):
        registry = self.automation.AutomationRegistry()
        repository = self.automation.AutomationRepository(self.db)
        runner = self.automation.AutomationRunner(registry, repository)
        service = self.automation.AutomationService(
            registry=registry,
            repository=repository,
            runner=runner,
        )
        self.services.append(service)
        return service

    async def _wait_until(self, predicate):
        for _ in range(500):
            if await predicate():
                return
            await asyncio.sleep(0)
        self.fail("condition did not become true")

    async def test_existing_sources_bootstrap_one_stable_task_each(self):
        first = await self._source("First")
        second = await self._source("Second", enabled=False)
        service = self._service()

        result = await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)

        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["enabled_source_count"], 1)
        self.assertEqual(result["created_count"], 2)
        first_config = await service.repository.get_config(
            self.epg_tasks.epg_task_id(first["id"])
        )
        second_config = await service.repository.get_config(
            self.epg_tasks.epg_task_id(second["id"])
        )
        self.assertTrue(first_config.enabled)
        self.assertFalse(second_config.enabled)
        self.assertEqual(first_config.interval_seconds, 6 * 60 * 60)
        self.assertEqual(
            service.registry.get(first_config.task_id).conflict_group,
            self.epg_tasks.epg_conflict_group(first["id"]),
        )

    async def test_bootstrap_is_idempotent_and_does_not_duplicate_tasks(self):
        source = await self._source()
        service = self._service()
        first = await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        second = await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)

        self.assertEqual(first["created_count"], 1)
        self.assertEqual(second["created_count"], 0)
        configs = await service.repository.list_configs()
        matching = [row for row in configs if row.task_id == self.epg_tasks.epg_task_id(source["id"])]
        self.assertEqual(len(matching), 1)
        definitions = [
            item for item in service.registry.list_definitions()
            if item.task_id == self.epg_tasks.epg_task_id(source["id"])
        ]
        self.assertEqual(len(definitions), 1)

    async def test_interval_update_keeps_task_identity_and_reconcile_preserves_it(self):
        source = await self._source()
        service = self._service()
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        task_id = self.epg_tasks.epg_task_id(source["id"])

        updated = await service.repository.update_config(task_id, interval_seconds=1234)
        replay = await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)

        self.assertEqual(updated.task_id, task_id)
        self.assertEqual((await service.repository.get_config(task_id)).interval_seconds, 1234)
        self.assertEqual(replay["created_count"], 0)

    async def test_source_name_and_url_update_keep_source_id_task_identity(self):
        source = await self._source()
        service = self._service()
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        task_id = self.epg_tasks.epg_task_id(source["id"])
        definition = service.registry.get(task_id)

        updated_source = await self.db.update_epg_source(
            source["id"],
            name="Renamed",
            url="https://guide.test/revised.xml",
        )
        result = await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)

        self.assertEqual(updated_source["id"], source["id"])
        self.assertEqual(result["created_count"], 0)
        self.assertIs(service.registry.get(task_id), definition)

    async def test_started_service_reconciles_create_disable_reenable_and_delete(self):
        service = self._service()
        await service.start()
        source = await self._source()
        task_id = self.epg_tasks.epg_task_id(source["id"])

        with mock.patch.object(
            self.epg,
            "refresh_epg_source",
            new=mock.AsyncMock(return_value={"status": "revision_discarded", "error": ""}),
        ):
            created = await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
            await asyncio.sleep(0)
        self.assertEqual(created["created_count"], 1)
        self.assertIn(task_id, service.tasks)

        await self.db.update_epg_source(source["id"], enabled=0)
        disabled = await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        self.assertEqual(disabled["updated_count"], 1)
        self.assertFalse((await service.repository.get_config(task_id)).enabled)

        await self.db.update_epg_source(source["id"], enabled=1)
        enabled = await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        self.assertEqual(enabled["updated_count"], 1)
        self.assertTrue((await service.repository.get_config(task_id)).enabled)

        await self.db.delete_epg_source(source["id"])
        deleted = await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        self.assertEqual(deleted["removed_count"], 1)
        self.assertNotIn(task_id, service.tasks)
        self.assertIsNone(await service.repository.get_config(task_id))

    async def test_stale_deleted_snapshot_cannot_recreate_removed_task(self):
        source = await self._source()
        service = self._service()
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        stale_sources = await self.db.get_epg_sources()
        task_id = self.epg_tasks.epg_task_id(source["id"])

        await self.db.delete_epg_source(source["id"])
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        self.assertNotIn(task_id, service.registry)
        self.assertIsNone(await service.repository.get_config(task_id))

        # This is an in-flight old snapshot arriving after the delete path has
        # already reconciled.  The production reconciliation entry point must
        # use its final per-source durable read before any projection mutation.
        with mock.patch.object(
            self.db,
            "get_epg_sources",
            new=mock.AsyncMock(return_value=stale_sources),
        ):
            await self.epg_tasks.reconcile_epg_automation_tasks(
                service,
                self.client,
            )
        self.assertNotIn(task_id, service.registry)
        self.assertIsNone(await service.repository.get_config(task_id))

    async def test_stale_revision_reconciles_latest_source_configuration(self):
        source = await self._source(enabled=False)
        service = self._service()
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        stale_sources = await self.db.get_epg_sources()
        task_id = self.epg_tasks.epg_task_id(source["id"])
        self.assertFalse((await service.repository.get_config(task_id)).enabled)

        await self.db.update_epg_source(source["id"], enabled=1)
        with mock.patch.object(
            self.db,
            "get_epg_sources",
            new=mock.AsyncMock(return_value=stale_sources),
        ):
            await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        current = await self.db.get_epg_source(source["id"])
        self.assertGreater(current["revision"], stale_sources[0]["revision"])
        self.assertTrue((await service.repository.get_config(task_id)).enabled)

    async def test_stale_enabled_snapshot_cannot_reenable_disabled_source(self):
        source = await self._source()
        service = self._service()
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        stale_sources = await self.db.get_epg_sources()
        task_id = self.epg_tasks.epg_task_id(source["id"])

        await self.db.update_epg_source(source["id"], enabled=0)
        with mock.patch.object(
            self.db,
            "get_epg_sources",
            new=mock.AsyncMock(return_value=stale_sources),
        ):
            await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        self.assertFalse((await service.repository.get_config(task_id)).enabled)

    async def test_delete_and_reconcile_waits_for_inflight_projection(self):
        source = await self._source()
        service = self._service()
        entered = asyncio.Event()
        release = asyncio.Event()
        delete_started = asyncio.Event()
        task_id = self.epg_tasks.epg_task_id(source["id"])
        original_add = service.add_definition

        async def blocked_add(definition):
            entered.set()
            await release.wait()
            return await original_add(definition)

        async def delete_and_reconcile():
            delete_started.set()
            await self.source_management.delete_custom_epg_source(source["id"])
            return await self.epg_tasks.reconcile_epg_automation_tasks(
                service,
                self.client,
            )

        self.source_management = importlib.import_module("epg_source_management")
        with mock.patch.object(service, "add_definition", new=blocked_add):
            reconcile_task = asyncio.create_task(
                self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
            )
            await entered.wait()
            self.assertTrue(self.epg_tasks.EPG_RECONCILIATION_LOCK.locked())
            delete_task = asyncio.create_task(delete_and_reconcile())
            await delete_started.wait()
            release.set()
            await asyncio.gather(reconcile_task, delete_task)

        self.assertIsNone(await self.db.get_epg_source(source["id"]))
        self.assertNotIn(task_id, service.registry)
        self.assertIsNone(await service.repository.get_config(task_id))

    async def test_reconciliation_cancellation_drains_projection(self):
        source = await self._source()
        service = self._service()
        entered = asyncio.Event()
        release = asyncio.Event()
        original_add = service.add_definition

        async def blocked_add(definition):
            entered.set()
            await release.wait()
            return await original_add(definition)

        with mock.patch.object(service, "add_definition", new=blocked_add):
            reconcile_task = asyncio.create_task(
                self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
            )
            await entered.wait()
            reconcile_task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await reconcile_task

        task_id = self.epg_tasks.epg_task_id(source["id"])
        self.assertIn(task_id, service.registry)
        self.assertIsNotNone(await service.repository.get_config(task_id))

    async def test_failed_projection_retries_against_current_source(self):
        source = await self._source()
        service = self._service()
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        task_id = self.epg_tasks.epg_task_id(source["id"])
        await self.db.update_epg_source(source["id"], enabled=0)
        original_update = service.repository.update_config

        with mock.patch.object(
            service.repository,
            "update_config",
            new=mock.AsyncMock(side_effect=RuntimeError("injected projection failure")),
        ):
            reconciled = await self.epg_tasks.reconcile_epg_tasks_after_source_change(
                service,
                self.client,
                source_id=source["id"],
                operation="disable",
            )
        self.assertFalse(reconciled)
        self.assertTrue((await service.repository.get_config(task_id)).enabled)

        with mock.patch.object(
            service.repository,
            "update_config",
            new=original_update,
        ):
            await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        self.assertFalse((await service.repository.get_config(task_id)).enabled)

    async def test_failed_deleted_projection_recovers_on_next_reconcile(self):
        source = await self._source()
        service = self._service()
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        task_id = self.epg_tasks.epg_task_id(source["id"])
        await self.db.delete_epg_source(source["id"])
        original_remove = service.remove_definition

        with mock.patch.object(
            service,
            "remove_definition",
            new=mock.AsyncMock(side_effect=RuntimeError("injected removal failure")),
        ):
            reconciled = await self.epg_tasks.reconcile_epg_tasks_after_source_change(
                service,
                self.client,
                source_id=source["id"],
                operation="delete",
            )
        self.assertFalse(reconciled)
        self.assertIn(task_id, service.registry)
        self.assertIsNotNone(await service.repository.get_config(task_id))

        with mock.patch.object(service, "remove_definition", new=original_remove):
            await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        self.assertNotIn(task_id, service.registry)
        self.assertIsNone(await service.repository.get_config(task_id))

    async def test_orphan_persisted_task_is_removed(self):
        service = self._service()
        task_id = self.epg_tasks.epg_task_id(999)
        await self.db.ensure_automation_task_config(
            task_id,
            self.epg_tasks.epg_conflict_group(999),
            True,
            21600,
        )
        malformed_task_id = f"{self.epg_tasks.EPG_TASK_PREFIX}invalid"
        await self.db.ensure_automation_task_config(
            malformed_task_id,
            "epg_source:invalid",
            True,
            21600,
        )

        result = await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)

        self.assertEqual(result["removed_count"], 2)
        self.assertIsNone(await service.repository.get_config(task_id))
        self.assertIsNone(await service.repository.get_state(task_id))
        self.assertIsNone(await service.repository.get_config(malformed_task_id))

    async def _run_handler(self, source_result, *, maintenance=None, maintenance_error=None):
        self._source_counter += 1
        source = await self._source(name=f"Guide-{self._source_counter}")
        service = self._service()
        definition = self.epg_tasks.create_epg_task_definition(source["id"], self.client)
        await service.add_definition(definition)
        maintenance = maintenance or {"status": "success", "error": ""}
        with mock.patch.object(
            self.epg,
            "refresh_epg_source",
            new=mock.AsyncMock(return_value=source_result),
        ) as refresh, mock.patch.object(
            self.epg,
            "run_epg_refresh_maintenance",
            new=mock.AsyncMock(
                side_effect=maintenance_error,
                return_value=maintenance,
            ),
        ) as run_maintenance:
            result = await service.runner.run(self.automation.AutomationRunRequest(
                task_id=definition.task_id,
                task_type="refresh",
                trigger="internal",
            ))
        return source, service, result, refresh, run_maintenance

    async def test_handler_success_uses_existing_refresh_and_maintenance(self):
        source, _service, result, refresh, maintenance = await self._run_handler({
            "status": "success",
            "error": "",
        })
        self.assertEqual(result.status, "success")
        self.assertEqual(result.checked_count, 1)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(refresh.await_args.args[0]["id"], source["id"])
        self.assertIs(refresh.await_args.args[1], self.client)
        self.assertIsInstance(refresh.await_args.kwargs["stop_event"], asyncio.Event)
        maintenance.assert_awaited_once_with(trigger="epg_automation")

    async def test_handler_missing_source_is_safe_skip_without_network(self):
        service = self._service()
        definition = self.epg_tasks.create_epg_task_definition(999, self.client)
        await service.add_definition(definition)
        with mock.patch.object(
            self.epg,
            "refresh_epg_source",
            new=mock.AsyncMock(),
        ) as refresh:
            result = await service.runner.run(self.automation.AutomationRunRequest(
                task_id=definition.task_id,
                task_type="refresh",
                trigger="internal",
            ))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.skipped_count, 1)
        refresh.assert_not_awaited()

    async def test_handler_failed_and_stale_are_automation_failures(self):
        for status in ("failed", "stale"):
            with self.subTest(status=status):
                _source, _service, result, _refresh, maintenance = await self._run_handler({
                    "status": status,
                    "error": "network failure",
                })
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.failed_count, 1)
                maintenance.assert_not_awaited()

    async def test_handler_disabled_and_revision_discarded_are_safe_skips(self):
        for status in ("disabled", "revision_discarded"):
            with self.subTest(status=status):
                _source, _service, result, _refresh, maintenance = await self._run_handler({
                    "status": status,
                    "error": "",
                })
                self.assertEqual(result.status, "success")
                self.assertEqual(result.skipped_count, 1)
                maintenance.assert_not_awaited()

    async def test_dataset_success_with_maintenance_result_failure_is_partial(self):
        _source, _service, result, _refresh, _maintenance = await self._run_handler(
            {"status": "success", "error": ""},
            maintenance={"status": "failed", "error": "maintenance degraded"},
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.failed_count, 1)

    async def test_dataset_success_with_maintenance_exception_is_partial(self):
        _source, _service, result, _refresh, _maintenance = await self._run_handler(
            {"status": "success", "error": ""},
            maintenance_error=RuntimeError("secret maintenance detail"),
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.error, "EPG binding maintenance failed: RuntimeError")

    async def test_handler_error_redacts_sensitive_url_query(self):
        _source, _service, result, _refresh, _maintenance = await self._run_handler({
            "status": "failed",
            "error": "fetch https://guide.test/feed.xml?token=plain-secret failed",
        })
        self.assertNotIn("plain-secret", result.error)
        self.assertNotIn("token=", result.error)
        self.assertIn("EPG source", result.error)

    async def test_runner_cancellation_is_persisted_and_propagated(self):
        source = await self._source()
        service = self._service()
        definition = self.epg_tasks.create_epg_task_definition(source["id"], self.client)
        await service.add_definition(definition)
        with mock.patch.object(
            self.epg,
            "refresh_epg_source",
            new=mock.AsyncMock(side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await service.runner.run(self.automation.AutomationRunRequest(
                    task_id=definition.task_id,
                    task_type="refresh",
                    trigger="internal",
                ))
        state = await service.repository.get_state(definition.task_id)
        self.assertEqual(state.status, "cancelled")

    async def test_scheduled_and_manual_claim_same_source_only_once(self):
        source = await self._source()
        service = self._service()
        definition = self.epg_tasks.create_epg_task_definition(source["id"], self.client)
        await service.add_definition(definition)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked(_context, **_kwargs):
            entered.set()
            await release.wait()
            return self.automation.AutomationHandlerResult(status="success", checked_count=1)

        with mock.patch.object(self.epg_tasks, "run_epg_refresh_task", side_effect=blocked):
            first = asyncio.create_task(service.runner.run(self.automation.AutomationRunRequest(
                task_id=definition.task_id,
                task_type="refresh",
                trigger="scheduler",
            )))
            await entered.wait()
            second = await service.runner.run(self.automation.AutomationRunRequest(
                task_id=definition.task_id,
                task_type="refresh",
                trigger="manual_api",
            ))
            self.assertIsInstance(second, self.automation.AutomationBusy)
            release.set()
            completed = await first
        self.assertEqual(completed.status, "success")

    async def test_manual_run_now_targets_enabled_sources_without_duplicates(self):
        builtin = (await self.epg.ensure_default_epg_sources())[0]
        await self.db.update_epg_source(builtin["id"], enabled=0)
        enabled = await self._source("Enabled")
        await self._source("Disabled", enabled=False)
        service = self._service()
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        expected = self.automation.AutomationRunResult(
            task_id=self.epg_tasks.epg_task_id(enabled["id"]),
            task_type="refresh",
            status="success",
            checked_count=1,
            updated_count=1,
            skipped_count=0,
            failed_count=0,
            error="",
            started_at="2026-08-09T00:00:00+00:00",
            finished_at="2026-08-09T00:00:01+00:00",
        )
        with mock.patch.object(service.runner, "run", new=mock.AsyncMock(return_value=expected)) as run:
            results = await self.epg_tasks.run_epg_refresh_now(service, self.client)
        self.assertEqual(results, (expected,))
        request = run.await_args.args[0]
        self.assertEqual(request.task_id, self.epg_tasks.epg_task_id(enabled["id"]))
        self.assertEqual(request.trigger, "manual_api")

    async def test_single_source_run_now_reuses_reconciled_automation_runner(self):
        source = await self._source("Single")
        service = self._service()
        expected = self.automation.AutomationRunResult(
            task_id=self.epg_tasks.epg_task_id(source["id"]),
            task_type="refresh",
            status="success",
            checked_count=1,
            updated_count=1,
            skipped_count=0,
            failed_count=0,
            error="",
            started_at="2026-08-09T00:00:00+00:00",
            finished_at="2026-08-09T00:00:01+00:00",
        )
        with mock.patch.object(
            self.epg_tasks,
            "reconcile_epg_automation_tasks",
            new=mock.AsyncMock(),
        ) as reconcile, mock.patch.object(
            service.runner,
            "run",
            new=mock.AsyncMock(return_value=expected),
        ) as run:
            result = await self.epg_tasks.run_epg_source_refresh_now(
                service,
                self.client,
                source_id=source["id"],
            )

        self.assertIs(result, expected)
        reconcile.assert_awaited_once_with(service, self.client)
        request = run.await_args.args[0]
        self.assertEqual(request.task_id, self.epg_tasks.epg_task_id(source["id"]))
        self.assertEqual(request.task_type, "refresh")
        self.assertEqual(request.trigger, "manual_api")

    async def test_production_factory_registers_market_and_all_sources(self):
        first = await self._source("First")
        second = await self._source("Second", enabled=False)
        service = await self.epg_tasks.create_production_automation_service(self.client)
        self.services.append(service)
        builtin = next(
            source for source in await self.db.get_epg_sources()
            if source["source_origin"] == "builtin"
        )
        task_ids = {item.task_id for item in service.registry.list_definitions()}
        self.assertEqual(
            task_ids,
            {
                "market_auto_update",
                self.epg_tasks.epg_task_id(builtin["id"]),
                self.epg_tasks.epg_task_id(first["id"]),
                self.epg_tasks.epg_task_id(second["id"]),
            },
        )

    async def test_empty_install_factory_bootstraps_default_source_task_without_refresh(self):
        with mock.patch.object(
            self.epg,
            "refresh_epg_source",
            new=mock.AsyncMock(),
        ) as refresh:
            service = await self.epg_tasks.create_production_automation_service(self.client)
        self.services.append(service)
        sources = await self.db.get_epg_sources()
        self.assertEqual(len(sources), 1)
        self.assertIn(
            self.epg_tasks.epg_task_id(sources[0]["id"]),
            {item.task_id for item in service.registry.list_definitions()},
        )
        refresh.assert_not_awaited()

    async def test_internal_status_is_source_aware_and_omits_source_url(self):
        source = await self._source(
            url="https://guide.test/feed.xml?token=plain-secret&region=one"
        )
        service = self._service()
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.client)
        rows = await self.epg_tasks.list_epg_automation_status(service)
        self.assertEqual(rows[0]["source_id"], source["id"])
        self.assertEqual(rows[0]["task_id"], self.epg_tasks.epg_task_id(source["id"]))
        serialized = repr(rows)
        self.assertNotIn("plain-secret", serialized)
        self.assertNotIn("guide.test", serialized)

    async def test_crud_reconcile_failure_does_not_undo_source_change(self):
        source = await self._source()
        service = self._service()
        with mock.patch.object(
            self.epg_tasks,
            "reconcile_epg_automation_tasks",
            new=mock.AsyncMock(side_effect=RuntimeError("schedule failed")),
        ):
            await self.epg_tasks.reconcile_epg_tasks_after_source_change(
                service,
                self.client,
                source_id=source["id"],
                operation="create",
            )
        self.assertIsNotNone(await self.db.get_epg_source(source["id"]))

    def test_normal_lifespan_has_no_legacy_epg_periodic_loop(self):
        main_path = os.path.join(os.path.dirname(self.epg.__file__), "main.py")
        with open(main_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("_epg_refresh_loop", source)
        self.assertNotIn("asyncio.create_task(_epg.refresh_epg_sources", source)
        self.assertIn("await create_production_automation_service(http_client)", source)
        service_source = inspect.getsource(self.automation.AutomationService)
        self.assertEqual(service_source.count("asyncio.create_task"), 1)


if __name__ == "__main__":
    unittest.main()
