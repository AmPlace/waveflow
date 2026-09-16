import asyncio
import importlib
import os
import tempfile
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock


class ControlledWaiter:
    def __init__(self):
        self.calls = []
        self.pending = deque()

    async def wait(self, delay_seconds, *, stop_event, config_event):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.calls.append(delay_seconds)
        self.pending.append(future)
        stop_task = asyncio.create_task(stop_event.wait())
        config_task = asyncio.create_task(config_event.wait())
        try:
            done, _ = await asyncio.wait(
                (future, stop_task, config_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if future in done:
                return future.result()
            if stop_task in done:
                return "stopped"
            return "reconfigured"
        finally:
            try:
                self.pending.remove(future)
            except ValueError:
                pass
            for task in (stop_task, config_task):
                task.cancel()
            await asyncio.gather(stop_task, config_task, return_exceptions=True)

    def release(self, result="timeout"):
        while self.pending:
            future = self.pending.popleft()
            if not future.done():
                future.set_result(result)
                return
        raise AssertionError("没有等待中的 waiter")


class LifecycleProbeScheduler:
    def __init__(self, *, fail_on_stop=False, hold_on_stop=False):
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.stop_called = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.stop_calls = 0
        self.fail_on_stop = fail_on_stop
        self.hold_on_stop = hold_on_stop

    def stop(self):
        self.stop_calls += 1
        self.stop_called.set()
        if self.fail_on_stop:
            raise RuntimeError("injected scheduler stop failure")
        if not self.hold_on_stop:
            self.stop_event.set()

    async def run(self):
        self.started.set()
        try:
            await self.stop_event.wait()
        finally:
            self.finished.set()


class AutomationSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self.tmpdir.name, "waveflow.db")
        for module_name in ("automation", "database"):
            importlib.sys.modules.pop(module_name, None)
        self.db = importlib.import_module("database")
        await self.db.initialize()
        self.automation = importlib.import_module("automation")
        self.waiter = ControlledWaiter()

    async def asyncTearDown(self):
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.old_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self.old_db_path
        importlib.sys.modules.pop("automation", None)
        importlib.sys.modules.pop("database", None)
        self.tmpdir.cleanup()

    def definition(
        self,
        handler=None,
        *,
        task_id="task-a",
        group="group-a",
        auto=True,
        initial_delay=5,
        interval=20,
    ):
        return self.automation.AutomationTaskDefinition(
            task_id=task_id,
            display_name=task_id,
            conflict_group=group,
            default_enabled=True,
            default_interval_seconds=interval,
            initial_delay_seconds=initial_delay,
            handler=handler or mock.AsyncMock(return_value=self.automation.AutomationHandlerResult()),
            allowed_task_types=frozenset({"check", "auto_update"}),
            scheduled_task_type="check" if auto else None,
            allow_automatic_scheduling=auto,
        )

    def runner(self, definitions):
        registry = self.automation.AutomationRegistry()
        for definition in definitions:
            registry.register(definition)
        repository = self.automation.AutomationRepository(self.db)
        runner = mock.Mock()
        runner.run = mock.AsyncMock(
            return_value=self.automation.AutomationRunResult(
                task_id=definitions[0].task_id,
                task_type="check",
                status="success",
                checked_count=0,
                updated_count=0,
                skipped_count=0,
                failed_count=0,
                error="",
                started_at="start",
                finished_at="finish",
            ),
        )
        return repository, runner

    async def wait_until(self, predicate):
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail("条件未在事件循环中满足")

    async def test_initial_delay_then_runner_then_interval(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await repository.ensure_config(definition)
        scheduler = self.automation.AutomationScheduler(
            definition, runner, repository, waiter=self.waiter
        )
        task = asyncio.create_task(scheduler.run())
        await self.wait_until(lambda: self.waiter.calls == [5])
        self.waiter.release("timeout")
        await self.wait_until(lambda: runner.run.await_count == 1 and self.waiter.calls == [5, 20])
        self.waiter.release("timeout")
        await self.wait_until(lambda: runner.run.await_count == 2)
        scheduler.stop()
        await task

    async def test_disabled_waits_without_runner_and_enable_restarts_initial_delay(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await repository.ensure_config(definition)
        await self.db.update_automation_task_config(definition.task_id, enabled=False)
        scheduler = self.automation.AutomationScheduler(
            definition, runner, repository, waiter=self.waiter
        )
        task = asyncio.create_task(scheduler.run())
        await self.wait_until(lambda: self.waiter.calls == [None])
        await self.db.update_automation_task_config(definition.task_id, enabled=True)
        scheduler.notify_config_changed()
        await self.wait_until(lambda: self.waiter.calls == [None, 5])
        self.waiter.release("timeout")
        await self.wait_until(lambda: runner.run.await_count == 1)
        scheduler.stop()
        await task

    async def test_interval_change_restarts_interval_without_immediate_run(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await repository.ensure_config(definition)
        scheduler = self.automation.AutomationScheduler(
            definition, runner, repository, waiter=self.waiter
        )
        task = asyncio.create_task(scheduler.run())
        await self.wait_until(lambda: self.waiter.calls == [5])
        self.waiter.release("timeout")
        await self.wait_until(lambda: self.waiter.calls == [5, 20])
        await self.db.update_automation_task_config(definition.task_id, interval_seconds=7)
        scheduler.notify_config_changed()
        await self.wait_until(lambda: self.waiter.calls == [5, 20, 7])
        self.assertEqual(runner.run.await_count, 1)
        self.waiter.release("timeout")
        await self.wait_until(lambda: runner.run.await_count == 2)
        scheduler.stop()
        await task

    async def test_interval_change_during_initial_delay_keeps_original_deadline(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await repository.ensure_config(definition)
        now = [0.0]
        scheduler = self.automation.AutomationScheduler(
            definition,
            runner,
            repository,
            waiter=self.waiter,
            monotonic=lambda: now[0],
        )
        task = asyncio.create_task(scheduler.run())
        await self.wait_until(lambda: self.waiter.calls == [5])
        now[0] = 2.0
        await self.db.update_automation_task_config(definition.task_id, interval_seconds=7)
        scheduler.notify_config_changed()
        await self.wait_until(lambda: self.waiter.calls == [5, 3.0])
        self.assertEqual(runner.run.await_count, 0)
        self.waiter.release("timeout")
        await self.wait_until(lambda: runner.run.await_count == 1)
        scheduler.stop()
        await task

    async def test_enabled_to_disabled_interrupts_interval_without_new_run(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await repository.ensure_config(definition)
        scheduler = self.automation.AutomationScheduler(
            definition, runner, repository, waiter=self.waiter
        )
        task = asyncio.create_task(scheduler.run())
        await self.wait_until(lambda: self.waiter.calls == [5])
        self.waiter.release("timeout")
        await self.wait_until(lambda: self.waiter.calls == [5, 20])
        await self.db.update_automation_task_config(definition.task_id, enabled=False)
        scheduler.notify_config_changed()
        await self.wait_until(lambda: self.waiter.calls == [5, 20, None])
        self.assertEqual(runner.run.await_count, 1)
        scheduler.stop()
        await task

    async def test_stop_interrupts_initial_and_interval_waits(self):
        for stop_after_first_run in (False, True):
            waiter = ControlledWaiter()
            definition = self.definition(task_id=f"stop-{stop_after_first_run}")
            repository, runner = self.runner([definition])
            await repository.ensure_config(definition)
            scheduler = self.automation.AutomationScheduler(
                definition, runner, repository, waiter=waiter
            )
            task = asyncio.create_task(scheduler.run())
            await self.wait_until(lambda: waiter.calls == [5])
            if stop_after_first_run:
                waiter.release("timeout")
                await self.wait_until(lambda: waiter.calls == [5, 20])
            scheduler.stop()
            await task
            self.assertEqual(runner.run.await_count, int(stop_after_first_run))

    async def test_runner_result_statuses_continue_but_cancelled_exits(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await repository.ensure_config(definition)
        runner.run.side_effect = [
            self.automation.AutomationRunResult(
                task_id=definition.task_id, task_type="check", status="partial",
                checked_count=1, updated_count=0, skipped_count=0, failed_count=1,
                error="partial", started_at="s", finished_at="f",
            ),
            self.automation.AutomationRunResult(
                task_id=definition.task_id, task_type="check", status="cancelled",
                checked_count=0, updated_count=0, skipped_count=0, failed_count=0,
                error="cancelled", started_at="s", finished_at="f",
            ),
        ]
        scheduler = self.automation.AutomationScheduler(
            definition, runner, repository, waiter=self.waiter
        )
        task = asyncio.create_task(scheduler.run())
        await self.wait_until(lambda: self.waiter.calls == [5])
        self.waiter.release("timeout")
        await self.wait_until(lambda: self.waiter.calls == [5, 20])
        self.waiter.release("timeout")
        await self.wait_until(lambda: runner.run.await_count == 2)
        await task

    async def test_runner_failed_and_busy_use_normal_interval_without_queueing(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await repository.ensure_config(definition)
        runner.run.side_effect = [
            self.automation.AutomationRunResult(
                task_id=definition.task_id, task_type="check", status="failed",
                checked_count=0, updated_count=0, skipped_count=0, failed_count=1,
                error="failed", started_at="s", finished_at="f",
            ),
            self.automation.AutomationBusy(
                task_id="manual-task", task_type="update_all", conflict_group="group-a",
                started_at="s", status="running",
            ),
        ]
        scheduler = self.automation.AutomationScheduler(
            definition, runner, repository, waiter=self.waiter
        )
        task = asyncio.create_task(scheduler.run())
        await self.wait_until(lambda: self.waiter.calls == [5])
        self.waiter.release("timeout")
        await self.wait_until(lambda: self.waiter.calls == [5, 20])
        self.waiter.release("timeout")
        await self.wait_until(lambda: self.waiter.calls == [5, 20, 20])
        self.assertEqual(runner.run.await_count, 2)
        scheduler.stop()
        await task

    async def test_scheduler_never_overlaps_runner_calls(self):
        definition = self.definition()
        repository, _ = self.runner([definition])
        await repository.ensure_config(definition)
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum_active = 0

        async def run(_request):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            entered.set()
            await release.wait()
            active -= 1
            return self.automation.AutomationRunResult(
                task_id=definition.task_id, task_type="check", status="success",
                checked_count=0, updated_count=0, skipped_count=0, failed_count=0,
                error="", started_at="s", finished_at="f",
            )

        runner = SimpleNamespace(run=mock.AsyncMock(side_effect=run))
        scheduler = self.automation.AutomationScheduler(
            definition, runner, repository, waiter=self.waiter
        )
        task = asyncio.create_task(scheduler.run())
        await self.wait_until(lambda: self.waiter.calls == [5])
        self.waiter.release("timeout")
        await entered.wait()
        await asyncio.sleep(0)
        self.assertEqual(runner.run.await_count, 1)
        release.set()
        await self.wait_until(lambda: self.waiter.calls == [5, 20])
        self.assertEqual(maximum_active, 1)
        scheduler.stop()
        await task

    async def test_repository_failure_uses_controlled_infrastructure_backoff(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        config = await repository.ensure_config(definition)
        repository.ensure_config = mock.AsyncMock(side_effect=[RuntimeError("db down"), config])
        scheduler = self.automation.AutomationScheduler(
            definition, runner, repository, waiter=self.waiter,
            infrastructure_retry_seconds=60,
        )
        task = asyncio.create_task(scheduler.run())
        await self.wait_until(lambda: self.waiter.calls == [60])
        self.assertEqual(runner.run.await_count, 0)
        self.waiter.release("timeout")
        await self.wait_until(lambda: self.waiter.calls == [60, 5])
        scheduler.stop()
        await task

    async def test_definition_and_config_conflict_group_mismatch_is_terminal(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await self.db.ensure_automation_task_config(definition.task_id, "wrong-group", True, 20)
        scheduler = self.automation.AutomationScheduler(
            definition, runner, repository, waiter=self.waiter
        )
        with self.assertRaises(self.automation.AutomationConfigurationError):
            await scheduler.run()
        runner.run.assert_not_awaited()

    async def test_production_waiter_cleans_internal_tasks(self):
        waiter = self.automation.AutomationEventWaiter()
        stop_event = asyncio.Event()
        config_event = asyncio.Event()
        current = asyncio.current_task()
        before = {task for task in asyncio.all_tasks() if task is not current}
        wait_task = asyncio.create_task(
            waiter.wait(3600, stop_event=stop_event, config_event=config_event)
        )
        await asyncio.sleep(0)
        config_event.set()
        result = await wait_task
        await asyncio.sleep(0)
        after = {task for task in asyncio.all_tasks() if task is not current}
        self.assertEqual(result, self.automation.AutomationWaitOutcome.RECONFIGURED)
        self.assertEqual(after, before)

    async def test_stop_event_is_passed_to_runner_and_stops_after_current_run(self):
        definition = self.definition()
        repository, _ = self.runner([definition])
        await repository.ensure_config(definition)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def run(request):
            self.assertIs(request.trigger, "scheduler")
            self.assertIsNotNone(request.stop_event)
            entered.set()
            await release.wait()
            return self.automation.AutomationRunResult(
                task_id=definition.task_id, task_type="check", status="success",
                checked_count=0, updated_count=0, skipped_count=0, failed_count=0,
                error="", started_at="s", finished_at="f",
            )

        runner = mock.Mock()
        runner.run = mock.AsyncMock(side_effect=run)
        scheduler = self.automation.AutomationScheduler(
            definition, runner, repository, waiter=self.waiter
        )
        task = asyncio.create_task(scheduler.run())
        await self.wait_until(lambda: self.waiter.calls == [5])
        self.waiter.release("timeout")
        await entered.wait()
        scheduler.stop()
        self.assertTrue(scheduler.stop_event.is_set())
        release.set()
        await task
        self.assertEqual(self.waiter.calls, [5])

    async def test_service_start_stop_restart_and_recover_before_scheduler(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await repository.ensure_config(definition)
        service = self.automation.AutomationService(
            registry=self._registry(definition), repository=repository, runner=runner,
            waiter_factory=lambda _: self.waiter,
        )
        with mock.patch.object(repository, "recover_interrupted", new=mock.AsyncMock(return_value=2)) as recover:
            await service.start()
            recover.assert_awaited_once()
        self.assertEqual(len(service.tasks), 1)
        first_task = service.tasks[definition.task_id]
        await service.start()
        self.assertIs(service.tasks[definition.task_id], first_task)
        await service.stop()

    async def test_service_waits_for_recovery_before_starting_scheduler(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await repository.ensure_config(definition)
        recovery_started = asyncio.Event()
        recovery_release = asyncio.Event()

        async def recover():
            recovery_started.set()
            await recovery_release.wait()
            return 1

        repository.recover_interrupted = mock.AsyncMock(side_effect=recover)
        service = self.automation.AutomationService(
            registry=self._registry(definition), repository=repository, runner=runner,
            waiter_factory=lambda _: self.waiter,
        )
        start_task = asyncio.create_task(service.start())
        await recovery_started.wait()
        self.assertEqual(service.tasks, {})
        recovery_release.set()
        self.assertEqual(await start_task, 1)
        self.assertEqual(set(service.tasks), {definition.task_id})
        await service.stop()
        self.assertEqual(service.tasks, {})
        await service.start()
        self.assertEqual(len(service.tasks), 1)
        await service.stop()

    async def test_staged_start_cleans_scheduler_when_later_factory_fails(self):
        first = self.definition(task_id="first", group="first-group")
        second = self.definition(task_id="second", group="second-group")
        repository, runner = self.runner([first, second])
        await repository.ensure_config(first)
        await repository.ensure_config(second)
        first_scheduler = LifecycleProbeScheduler()

        def factory(definition, *_args, **_kwargs):
            if definition.task_id == "second":
                raise RuntimeError("injected scheduler construction failure")
            return first_scheduler

        service = self.automation.AutomationService(
            registry=self._registry(first, second),
            repository=repository,
            runner=runner,
            scheduler_factory=factory,
        )

        with self.assertRaisesRegex(RuntimeError, "construction failure"):
            await service.start()

        self.assertFalse(service.is_started)
        self.assertEqual(service.schedulers, {})
        self.assertEqual(service.tasks, {})
        self.assertEqual(first_scheduler.stop_calls, 1)
        self.assertFalse(
            any(
                task.get_name() in {
                    "automation-scheduler:first",
                    "automation-scheduler:second",
                }
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            )
        )

    async def test_staged_start_cancellation_cleans_partial_resources(self):
        first = self.definition(task_id="first", group="first-group")
        second = self.definition(task_id="second", group="second-group")
        repository, runner = self.runner([first, second])
        await repository.ensure_config(first)
        await repository.ensure_config(second)
        first_scheduler = LifecycleProbeScheduler()

        def factory(definition, *_args, **_kwargs):
            if definition.task_id == "second":
                asyncio.current_task().cancel()
                raise asyncio.CancelledError
            return first_scheduler

        service = self.automation.AutomationService(
            registry=self._registry(first, second),
            repository=repository,
            runner=runner,
            scheduler_factory=factory,
        )

        with self.assertRaises(asyncio.CancelledError):
            await service.start()

        self.assertFalse(service.is_started)
        self.assertEqual(service.schedulers, {})
        self.assertEqual(service.tasks, {})
        self.assertEqual(first_scheduler.stop_calls, 1)
        self.assertFalse(
            any(
                task.get_name() == "automation-scheduler:first"
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            )
        )

    async def test_partial_start_retry_publishes_one_scheduler_set_and_stops_all(self):
        first = self.definition(task_id="first", group="first-group")
        second = self.definition(task_id="second", group="second-group")
        repository, runner = self.runner([first, second])
        await repository.ensure_config(first)
        await repository.ensure_config(second)
        failed_first = LifecycleProbeScheduler()
        successful = []
        fail_once = True

        def factory(definition, *_args, **_kwargs):
            nonlocal fail_once
            if fail_once and definition.task_id == "second":
                fail_once = False
                raise RuntimeError("injected retry failure")
            if fail_once and definition.task_id == "first":
                return failed_first
            scheduler = LifecycleProbeScheduler()
            successful.append(scheduler)
            return scheduler

        service = self.automation.AutomationService(
            registry=self._registry(first, second),
            repository=repository,
            runner=runner,
            scheduler_factory=factory,
        )

        with self.assertRaisesRegex(RuntimeError, "retry failure"):
            await service.start()
        self.assertEqual(failed_first.stop_calls, 1)
        self.assertEqual(service.tasks, {})

        await service.start()
        self.assertTrue(service.is_started)
        self.assertEqual(set(service.schedulers), {"first", "second"})
        self.assertEqual(set(service.tasks), {"first", "second"})
        await asyncio.gather(*(scheduler.started.wait() for scheduler in successful))
        live_scheduler_tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("automation-scheduler:")
        ]
        self.assertEqual(
            {task.get_name() for task in live_scheduler_tasks},
            {"automation-scheduler:first", "automation-scheduler:second"},
        )

        task_handles = tuple(service.tasks.values())
        await service.stop()

        self.assertEqual(service.schedulers, {})
        self.assertEqual(service.tasks, {})
        self.assertTrue(all(task.done() for task in task_handles))
        self.assertTrue(all(scheduler.stop_calls == 1 for scheduler in successful))

    async def test_cleanup_continues_after_one_scheduler_stop_failure(self):
        first = self.definition(task_id="first", group="first-group")
        second = self.definition(task_id="second", group="second-group")
        repository, runner = self.runner([first, second])
        await repository.ensure_config(first)
        await repository.ensure_config(second)
        schedulers = {
            "first": LifecycleProbeScheduler(fail_on_stop=True),
            "second": LifecycleProbeScheduler(),
        }

        service = self.automation.AutomationService(
            registry=self._registry(first, second),
            repository=repository,
            runner=runner,
            scheduler_factory=lambda definition, *_args, **_kwargs: schedulers[definition.task_id],
        )
        await service.start()
        await asyncio.gather(*(scheduler.started.wait() for scheduler in schedulers.values()))
        task_handles = tuple(service.tasks.values())

        with self.assertLogs(self.automation.logger, level="ERROR") as logs:
            await service.stop()

        self.assertIn("清理失败", "\n".join(logs.output))
        self.assertEqual(schedulers["first"].stop_calls, 1)
        self.assertEqual(schedulers["second"].stop_calls, 1)
        self.assertTrue(all(task.done() for task in task_handles))
        self.assertEqual(service.schedulers, {})
        self.assertEqual(service.tasks, {})

    async def test_stop_cancellation_drains_cleanup_before_releasing_lifecycle(self):
        definition = self.definition()
        repository, runner = self.runner([definition])
        await repository.ensure_config(definition)
        scheduler = LifecycleProbeScheduler(hold_on_stop=True)
        service = self.automation.AutomationService(
            registry=self._registry(definition),
            repository=repository,
            runner=runner,
            scheduler_factory=lambda *_args, **_kwargs: scheduler,
        )
        await service.start()
        await scheduler.started.wait()

        stop_task = asyncio.create_task(service.stop())
        await scheduler.stop_called.wait()
        stop_task.cancel()
        scheduler.stop_event.set()

        with self.assertRaises(asyncio.CancelledError):
            await stop_task
        await scheduler.finished.wait()
        self.assertFalse(service.is_started)
        self.assertEqual(service.schedulers, {})
        self.assertEqual(service.tasks, {})

    async def test_service_only_schedules_automatic_definitions_and_notifies_one_task(self):
        automatic = self.definition(task_id="automatic")
        manual = self.definition(task_id="manual", auto=False)
        repository, runner = self.runner([automatic, manual])
        await repository.ensure_config(automatic)
        await repository.ensure_config(manual)
        service = self.automation.AutomationService(
            registry=self._registry(automatic, manual), repository=repository, runner=runner,
            waiter_factory=lambda definition: self.waiter if definition.task_id == "automatic" else ControlledWaiter(),
        )
        await service.start()
        self.assertEqual(set(service.schedulers), {"automatic"})
        service.notify_config_changed("automatic")
        with self.assertRaises(self.automation.AutomationTaskNotFoundError):
            service.notify_config_changed("manual")
        await service.stop()

    async def test_config_notifications_are_isolated_and_coalesced(self):
        first = self.definition(task_id="first")
        second = self.definition(task_id="second", group="second-group")
        repository, runner = self.runner([first, second])
        await repository.ensure_config(first)
        await repository.ensure_config(second)
        waiters = {"first": ControlledWaiter(), "second": ControlledWaiter()}
        service = self.automation.AutomationService(
            registry=self._registry(first, second), repository=repository, runner=runner,
            waiter_factory=lambda definition: waiters[definition.task_id],
        )
        await service.start()
        await self.wait_until(lambda: waiters["first"].calls == [5] and waiters["second"].calls == [5])
        service.notify_config_changed("first")
        service.notify_config_changed("first")
        await self.wait_until(lambda: len(waiters["first"].calls) == 2)
        self.assertEqual(waiters["second"].calls, [5])
        await service.stop()

    async def test_service_timeout_force_cancels_running_scheduler(self):
        definition = self.definition()
        repository, _ = self.runner([definition])
        await repository.ensure_config(definition)
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def run(_request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        runner = SimpleNamespace(run=mock.AsyncMock(side_effect=run))
        waiter = ControlledWaiter()
        service = self.automation.AutomationService(
            registry=self._registry(definition), repository=repository, runner=runner,
            waiter_factory=lambda _: waiter,
        )
        await service.start()
        await self.wait_until(lambda: waiter.calls == [5])
        waiter.release("timeout")
        await entered.wait()
        await service.stop(timeout_seconds=0.01)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(service.tasks, {})

    async def test_service_observes_scheduler_exception_without_cancelling_other_task(self):
        first = self.definition(task_id="first")
        second = self.definition(task_id="second", group="second-group")
        repository, runner = self.runner([first, second])
        await repository.ensure_config(first)
        await repository.ensure_config(second)
        bad_scheduler = mock.Mock()
        bad_scheduler.run = mock.AsyncMock(side_effect=RuntimeError("scheduler boom"))
        good_scheduler = mock.Mock()
        good_scheduler.run = mock.AsyncMock()
        good_scheduler.stop = mock.Mock()
        factory = mock.Mock(side_effect=[bad_scheduler, good_scheduler])
        service = self.automation.AutomationService(
            registry=self._registry(first, second), repository=repository, runner=runner,
            scheduler_factory=factory,
        )
        await service.start()
        await asyncio.sleep(0)
        self.assertTrue(bad_scheduler.run.await_count)
        self.assertFalse(service.tasks["second"].cancelled())
        await self.wait_until(lambda: "first" in service.scheduler_errors)
        self.assertIn("first", service.scheduler_errors)
        await service.stop()

    def test_definition_rejects_invalid_automatic_schedule(self):
        with self.assertRaises(ValueError):
            self.automation.AutomationTaskDefinition(
                task_id="missing-type", display_name="missing-type", conflict_group="g",
                default_enabled=True, default_interval_seconds=20,
                handler=mock.AsyncMock(), allow_automatic_scheduling=True,
            )
        with self.assertRaises(ValueError):
            self.automation.AutomationTaskDefinition(
                task_id="wrong-type", display_name="wrong-type", conflict_group="g",
                default_enabled=True, default_interval_seconds=20,
                handler=mock.AsyncMock(), allowed_task_types=frozenset({"check"}),
                scheduled_task_type="auto_update", allow_automatic_scheduling=True,
            )

    def test_scheduler_does_not_create_long_lived_tasks(self):
        scheduler_source = __import__("inspect").getsource(self.automation.AutomationScheduler)
        service_source = __import__("inspect").getsource(self.automation.AutomationService)
        self.assertNotIn("asyncio.create_task", scheduler_source)
        self.assertEqual(service_source.count("asyncio.create_task"), 1)

    def _registry(self, *definitions):
        registry = self.automation.AutomationRegistry()
        for definition in definitions:
            registry.register(definition)
        return registry


if __name__ == "__main__":
    unittest.main()
