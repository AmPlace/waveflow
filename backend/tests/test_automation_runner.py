import asyncio
import importlib
import inspect
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


def _clear_modules():
    for module_name in list(sys.modules):
        if module_name in {
            "automation",
            "database",
            "security.secrets",
            "security.source_ids",
        }:
            sys.modules.pop(module_name, None)


class AutomationRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        self._old_handle_secret = os.environ.get("WAVEFLOW_PROXY_HANDLE_SECRET")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "waveflow.db")
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "automation-runner-test-secret-32-bytes"
        _clear_modules()
        self.db = importlib.import_module("database")
        await self.db.initialize()
        self.automation = importlib.import_module("automation")

    async def asyncTearDown(self):
        if self._old_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self._old_db_path
        if self._old_handle_secret is None:
            os.environ.pop("WAVEFLOW_PROXY_HANDLE_SECRET", None)
        else:
            os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = self._old_handle_secret
        _clear_modules()
        self._tmpdir.cleanup()

    def _definition(
        self,
        handler,
        *,
        task_id="test-task",
        conflict_group="test-group",
        task_types=("check",),
        allow_manual_trigger=True,
    ):
        return self.automation.AutomationTaskDefinition(
            task_id=task_id,
            display_name=task_id,
            conflict_group=conflict_group,
            default_enabled=True,
            default_interval_seconds=60,
            minimum_interval_seconds=1,
            maximum_interval_seconds=86400,
            handler=handler,
            allowed_task_types=frozenset(task_types),
            allow_manual_trigger=allow_manual_trigger,
        )

    def _runner(self, *definitions, repository=None):
        registry = self.automation.AutomationRegistry()
        for definition in definitions:
            registry.register(definition)
        repository = repository or self.automation.AutomationRepository(self.db)
        return self.automation.AutomationRunner(registry, repository), repository

    def _request(
        self,
        task_id="test-task",
        *,
        task_type="check",
        trigger="internal",
        parameters=None,
        stop_event=None,
    ):
        return self.automation.AutomationRunRequest(
            task_id=task_id,
            task_type=task_type,
            trigger=trigger,
            parameters=parameters or {},
            stop_event=stop_event,
        )

    async def test_register_and_execute_successful_task(self):
        received = {}

        async def handler(context):
            received["task_id"] = context.task_id
            received["task_type"] = context.task_type
            received["trigger"] = context.trigger
            received["parameters"] = dict(context.parameters)
            return self.automation.AutomationHandlerResult(
                checked_count=3,
                updated_count=2,
                skipped_count=1,
            )

        runner, _ = self._runner(self._definition(handler))
        result = await runner.run(self._request(parameters={"source": "test"}))

        self.assertIsInstance(result, self.automation.AutomationRunResult)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.checked_count, 3)
        self.assertEqual(result.updated_count, 2)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(received, {
            "task_id": "test-task",
            "task_type": "check",
            "trigger": "internal",
            "parameters": {"source": "test"},
        })
        state = await self.db.get_automation_task_state("test-task")
        self.assertEqual(state["last_status"], "success")
        self.assertEqual(state["checked_count"], 3)

    async def test_unknown_and_duplicate_tasks_are_rejected_before_claim(self):
        registry = self.automation.AutomationRegistry()

        async def handler(_context):
            return self.automation.AutomationHandlerResult()

        definition = self._definition(handler)
        registry.register(definition)
        with self.assertRaises(self.automation.AutomationRegistrationError):
            registry.register(definition)

        runner = self.automation.AutomationRunner(
            registry,
            self.automation.AutomationRepository(self.db),
        )
        with self.assertRaises(self.automation.AutomationTaskNotFoundError):
            await runner.run(self._request("unknown-task"))
        self.assertIsNone(await self.db.get_automation_task_state("unknown-task"))

    async def test_definition_conflict_group_mismatch_is_rejected(self):
        called = False

        async def handler(_context):
            nonlocal called
            called = True
            return self.automation.AutomationHandlerResult()

        await self.db.ensure_automation_task_config("mismatch", "database-group", True, 60)
        runner, _ = self._runner(
            self._definition(handler, task_id="mismatch", conflict_group="definition-group")
        )
        with self.assertRaises(self.automation.AutomationConfigurationError):
            await runner.run(self._request("mismatch"))
        self.assertFalse(called)
        state = await self.db.get_automation_task_state("mismatch")
        self.assertEqual(state["last_status"], "never_run")

    async def test_manual_trigger_and_task_type_validation_happen_before_claim(self):
        called = False

        async def handler(_context):
            nonlocal called
            called = True
            return self.automation.AutomationHandlerResult()

        runner, _ = self._runner(self._definition(handler, allow_manual_trigger=False))
        with self.assertRaises(self.automation.AutomationTriggerNotAllowedError):
            await runner.run(self._request(trigger="manual_api"))
        with self.assertRaises(self.automation.AutomationRequestError):
            await runner.run(self._request(task_type="update_all"))
        self.assertFalse(called)
        self.assertIsNone(await self.db.get_automation_task_config("test-task"))

    async def test_same_conflict_group_returns_busy_without_waiting_or_calling_handler(self):
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_called = False

        async def first_handler(_context):
            first_entered.set()
            await release_first.wait()
            return self.automation.AutomationHandlerResult()

        async def second_handler(_context):
            nonlocal second_called
            second_called = True
            return self.automation.AutomationHandlerResult()

        runner, _ = self._runner(
            self._definition(first_handler, task_id="first", conflict_group="shared"),
            self._definition(second_handler, task_id="second", conflict_group="shared"),
        )
        first_run = asyncio.create_task(runner.run(self._request("first")))
        await asyncio.wait_for(first_entered.wait(), timeout=1)

        busy = await asyncio.wait_for(
            runner.run(self._request("second")),
            timeout=0.5,
        )
        self.assertIsInstance(busy, self.automation.AutomationBusy)
        self.assertEqual(busy.task_id, "first")
        self.assertEqual(busy.task_type, "check")
        self.assertEqual(busy.conflict_group, "shared")
        self.assertEqual(busy.status, "running")
        self.assertFalse(hasattr(busy, "run_token"))
        self.assertFalse(second_called)

        release_first.set()
        self.assertEqual((await first_run).status, "success")

    async def test_different_conflict_groups_can_run_concurrently(self):
        entered = {"first": asyncio.Event(), "second": asyncio.Event()}
        release = asyncio.Event()

        def make_handler(name):
            async def handler(_context):
                entered[name].set()
                await release.wait()
                return self.automation.AutomationHandlerResult()

            return handler

        runner, _ = self._runner(
            self._definition(make_handler("first"), task_id="first", conflict_group="one"),
            self._definition(make_handler("second"), task_id="second", conflict_group="two"),
        )
        first_run = asyncio.create_task(runner.run(self._request("first")))
        second_run = asyncio.create_task(runner.run(self._request("second")))
        await asyncio.wait_for(
            asyncio.gather(entered["first"].wait(), entered["second"].wait()),
            timeout=1,
        )
        release.set()
        first_result, second_result = await asyncio.gather(first_run, second_run)
        self.assertEqual(first_result.status, "success")
        self.assertEqual(second_result.status, "success")

    async def test_status_calculation_for_empty_partial_and_all_failed_results(self):
        results = [
            self.automation.AutomationHandlerResult(),
            self.automation.AutomationHandlerResult(
                checked_count=3,
                updated_count=1,
                failed_count=1,
                error="one failed",
            ),
            self.automation.AutomationHandlerResult(
                checked_count=2,
                failed_count=2,
                error="all failed",
            ),
        ]

        async def handler(_context):
            return results.pop(0)

        runner, _ = self._runner(self._definition(handler))
        statuses = []
        for _ in range(3):
            statuses.append((await runner.run(self._request())).status)
        self.assertEqual(statuses, ["success", "partial", "failed"])

    async def test_handler_exception_is_sanitized_and_persisted_without_traceback(self):
        unsafe_error = "\x00 failed at /Users/private/project/secret.py " + ("x" * 3000)

        async def handler(_context):
            raise RuntimeError(unsafe_error)

        runner, _ = self._runner(self._definition(handler))
        result = await runner.run(self._request())

        self.assertEqual(result.status, "failed")
        self.assertLessEqual(len(result.error), self.db.AUTOMATION_ERROR_MAX_LENGTH)
        self.assertNotIn("\x00", result.error)
        self.assertNotIn("/Users/private", result.error)
        self.assertNotIn("Traceback", result.error)
        state = await self.db.get_automation_task_state("test-task")
        self.assertEqual(state["last_error"], result.error)

    async def test_invalid_handler_result_and_negative_counts_write_failed(self):
        results = [object(), "negative"]

        async def handler(_context):
            current = results.pop(0)
            if current == "negative":
                return self.automation.AutomationHandlerResult(checked_count=-1)
            return current

        runner, _ = self._runner(self._definition(handler))
        first = await runner.run(self._request())
        second = await runner.run(self._request())
        self.assertEqual(first.status, "failed")
        self.assertEqual(second.status, "failed")

    async def test_progress_uses_run_token_and_ownership_loss_is_not_success(self):
        progress_written = asyncio.Event()
        continue_handler = asyncio.Event()

        async def handler(context):
            await context.report_progress(checked_count=2, updated_count=1)
            progress_written.set()
            await continue_handler.wait()
            return self.automation.AutomationHandlerResult(
                checked_count=2,
                updated_count=1,
            )

        runner, repository = self._runner(self._definition(handler))
        run = asyncio.create_task(runner.run(self._request()))
        await asyncio.wait_for(progress_written.wait(), timeout=1)
        state = await self.db.get_automation_task_state("test-task")
        self.assertEqual(state["checked_count"], 2)
        self.assertFalse(await repository.update_progress(
            "test-task",
            "wrong-token",
            checked_count=99,
        ))
        self.assertTrue(await self.db.complete_automation_task(
            "test-task",
            state["run_token"],
            status="interrupted",
        ))
        continue_handler.set()
        with self.assertRaises(self.automation.AutomationOwnershipLostError):
            await run
        final_state = await self.db.get_automation_task_state("test-task")
        self.assertEqual(final_state["last_status"], "interrupted")
        self.assertEqual(final_state["checked_count"], 2)

    async def test_cooperative_stop_returns_cancelled_without_calling_handler(self):
        called = False
        stop_event = asyncio.Event()
        stop_event.set()

        async def handler(_context):
            nonlocal called
            called = True
            return self.automation.AutomationHandlerResult()

        runner, _ = self._runner(self._definition(handler))
        result = await runner.run(self._request(stop_event=stop_event))
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(called)
        self.assertEqual(
            (await self.db.get_automation_task_state("test-task"))["last_status"],
            "cancelled",
        )

    async def test_forced_cancellation_is_persisted_and_propagated(self):
        entered = asyncio.Event()

        async def handler(_context):
            entered.set()
            await asyncio.Event().wait()

        runner, _ = self._runner(self._definition(handler))
        run = asyncio.create_task(runner.run(self._request()))
        await asyncio.wait_for(entered.wait(), timeout=1)
        run.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run
        state = await self.db.get_automation_task_state("test-task")
        self.assertEqual(state["last_status"], "cancelled")

    async def test_forced_cancellation_still_propagates_after_ownership_loss(self):
        entered = asyncio.Event()

        async def handler(_context):
            entered.set()
            await asyncio.Event().wait()

        runner, _ = self._runner(self._definition(handler))
        run = asyncio.create_task(runner.run(self._request()))
        await asyncio.wait_for(entered.wait(), timeout=1)
        state = await self.db.get_automation_task_state("test-task")
        self.assertTrue(await self.db.complete_automation_task(
            "test-task",
            state["run_token"],
            status="interrupted",
        ))

        run.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run
        final_state = await self.db.get_automation_task_state("test-task")
        self.assertEqual(final_state["last_status"], "interrupted")

    async def test_handler_can_return_cooperative_cancelled_result(self):
        async def handler(context):
            self.assertFalse(context.stop_requested())
            return self.automation.AutomationHandlerResult(
                status="cancelled",
                checked_count=1,
                skipped_count=1,
                error="stopped at safe boundary",
            )

        runner, _ = self._runner(self._definition(handler))
        result = await runner.run(self._request())
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.checked_count, 1)
        self.assertEqual(result.skipped_count, 1)

    async def test_sequential_runs_use_distinct_tokens(self):
        async def handler(_context):
            return self.automation.AutomationHandlerResult()

        runner, _ = self._runner(self._definition(handler))
        await runner.run(self._request())
        first_token = (await self.db.get_automation_task_state("test-task"))["run_token"]
        await runner.run(self._request())
        second_token = (await self.db.get_automation_task_state("test-task"))["run_token"]
        self.assertNotEqual(first_token, second_token)

    async def test_repository_is_a_thin_adapter_over_database_functions(self):
        database_module = SimpleNamespace(
            ensure_automation_task_config=mock.AsyncMock(return_value={
                "task_id": "task",
                "conflict_group": "group",
                "enabled": 1,
                "interval_seconds": 60,
                "updated_at": "2026-08-05T00:00:00+00:00",
            }),
            get_automation_task_config=mock.AsyncMock(return_value={
                "task_id": "task",
                "conflict_group": "group",
                "enabled": 1,
                "interval_seconds": 60,
                "updated_at": "2026-08-05T00:00:00+00:00",
            }),
            list_automation_task_configs=mock.AsyncMock(return_value=[{
                "task_id": "task",
                "conflict_group": "group",
                "enabled": 1,
                "interval_seconds": 60,
                "updated_at": "2026-08-05T00:00:00+00:00",
            }]),
            claim_automation_task=mock.AsyncMock(return_value={
                "claimed": True,
                "state": {
                    "task_id": "task",
                    "conflict_group": "group",
                    "task_type": "check",
                    "run_token": "token",
                    "last_started_at": "2026-08-05T00:00:00+00:00",
                    "last_finished_at": "",
                    "last_status": "running",
                    "checked_count": 0,
                    "updated_count": 0,
                    "skipped_count": 0,
                    "failed_count": 0,
                    "last_error": "",
                },
            }),
            update_automation_task_progress=mock.AsyncMock(return_value=True),
            complete_automation_task=mock.AsyncMock(return_value=True),
            get_automation_task_state=mock.AsyncMock(return_value={
                "task_id": "task",
                "conflict_group": "group",
                "task_type": "check",
                "run_token": "token",
                "last_started_at": "2026-08-05T00:00:00+00:00",
                "last_finished_at": "",
                "last_status": "running",
                "checked_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "last_error": "",
            }),
            get_automation_conflict_group_state=mock.AsyncMock(return_value=None),
            recover_interrupted_automation_tasks=mock.AsyncMock(return_value=1),
        )
        repository = self.automation.AutomationRepository(database_module)
        await repository.ensure_config(self._definition(mock.AsyncMock()))
        await repository.get_config("task")
        self.assertEqual((await repository.list_configs())[0].task_id, "task")
        await repository.claim("task", "group", "check", "token", "2026-08-05T00:00:00+00:00")
        await repository.update_progress("task", "token", checked_count=1)
        await repository.complete(
            "task",
            "token",
            status="success",
            finished_at="2026-08-05T00:01:00+00:00",
            checked_count=1,
            updated_count=0,
            skipped_count=0,
            failed_count=0,
            error="",
        )
        await repository.get_state("task")
        await repository.get_busy("group")
        self.assertEqual(await repository.recover_interrupted(), 1)
        database_module.claim_automation_task.assert_awaited_once()
        database_module.complete_automation_task.assert_awaited_once()
        database_module.update_automation_task_progress.assert_awaited_once()

    def test_runner_contains_no_scheduler_or_background_loop(self):
        source = inspect.getsource(self.automation.AutomationRunner)
        self.assertNotIn("asyncio.create_task", source)
        self.assertNotIn("asyncio.sleep", source)
        self.assertNotIn("AutomationScheduler", source)
        self.assertNotIn("AutomationService", source)


if __name__ == "__main__":
    unittest.main()
