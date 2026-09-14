import asyncio
import contextlib
import importlib
import os
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _clear_modules():
    for name in list(sys.modules):
        if (
            name in {
                "main", "automation", "database", "epg", "epg_management",
                "epg_binding_management", "epg_bindings", "epg_catalog",
                "epg_maintenance", "epg_match_shadow", "epg_matcher",
                "iptv_logical_gc",
                "epg_read_resolver",
                "epg_preference_evidence", "epg_source_management",
                "epg_source_model", "epg_source_preference", "epg_tasks",
                "market", "market_tasks",
                "plugin_market", "plugin_production", "plugin_tasks",
                "official_plugin_distribution",
            }
            or name == "security"
            or name.startswith("security.")
            or name.startswith("core")
        ):
            sys.modules.pop(name, None)


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


class AutomationLifespanTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_env = {
            key: os.environ.get(key)
            for key in (
                "WAVEFLOW_DB_PATH",
                "WAVEFLOW_PLUGIN_ROOT",
                "WAVEFLOW_PROXY_HANDLE_SECRET",
                "WAVEFLOW_ANONYMOUS_BROWSE",
                "WAVEFLOW_ANONYMOUS_PLAYBACK",
                "WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP",
            )
        }
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "waveflow.db")
        os.environ["WAVEFLOW_PLUGIN_ROOT"] = os.path.join(self._tmpdir.name, "plugins")
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "automation-lifespan-test-secret-32-bytes"
        os.environ["WAVEFLOW_ANONYMOUS_BROWSE"] = "1"
        os.environ["WAVEFLOW_ANONYMOUS_PLAYBACK"] = "1"
        # These tests cover automation lifespan ordering, not fresh-install
        # official Plugin bootstrap or Plugin automation registration.
        os.environ["WAVEFLOW_OFFICIAL_PLUGIN_BOOTSTRAP"] = "0"
        _clear_modules()

        self.db = importlib.import_module("database")
        self.automation = importlib.import_module("automation")
        self.epg_tasks = importlib.import_module("epg_tasks")
        self.market_tasks = importlib.import_module("market_tasks")
        self.main = importlib.import_module("main")
        await self.db.initialize()
        self.main.app.state.automation_service = None

    async def asyncTearDown(self):
        service = getattr(self.main.app.state, "automation_service", None)
        if service is not None:
            await service.stop(timeout_seconds=0.1)
        current = asyncio.current_task()
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for client in (self.main.http_client,):
            if not client.is_closed:
                await client.aclose()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _clear_modules()
        self._tmpdir.cleanup()

    async def _wait_until(self, predicate):
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(0)
        self.fail("条件未在事件循环中满足")

    async def test_default_plugin_root_follows_configured_database(self):
        plugin_production = importlib.import_module("plugin_production")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WAVEFLOW_PLUGIN_ROOT", None)
            self.assertEqual(
                plugin_production.default_plugin_root(),
                Path(self._tmpdir.name).resolve() / "plugins",
            )

    def _patch_legacy_dependencies(self, events=None):
        events = events if events is not None else []
        stack = contextlib.ExitStack()

        def recorder(name):
            async def record(*_args, **_kwargs):
                events.append(name)

            return record

        for name in ("_rtsp_hls_cleanup_task",):
            stack.enter_context(mock.patch.object(
                self.main,
                name,
                new=mock.AsyncMock(side_effect=recorder(name)),
            ))
        stack.enter_context(mock.patch.object(
            self.main,
            "refresh_logo_template_from_remote",
            new=mock.AsyncMock(side_effect=recorder("logo")),
        ))
        stack.enter_context(mock.patch.object(
            self.main,
            "_clear_stale_rtsp_hls_dirs",
            side_effect=lambda: events.append("clear_rtsp"),
        ))
        stack.enter_context(mock.patch.object(
            self.main,
            "_stop_all_rtsp_sessions",
            new=mock.AsyncMock(side_effect=recorder("stop_rtsp")),
        ))
        stack.enter_context(mock.patch.object(
            self.main.http_client,
            "aclose",
            new=mock.AsyncMock(side_effect=recorder("close_http")),
        ))
        return stack, events

    async def test_factory_assembles_single_real_market_task(self):
        waiter = ControlledWaiter()
        service = self.market_tasks.create_market_automation_service(
            waiter_factory=lambda _definition: waiter,
        )

        definitions = service.registry.list_definitions()
        self.assertEqual(len(definitions), 1)
        definition = definitions[0]
        self.assertEqual(definition.task_id, "market_auto_update")
        self.assertEqual(definition.conflict_group, "market")
        self.assertEqual(definition.scheduled_task_type, "auto_update")
        self.assertEqual(definition.initial_delay_seconds, 300)
        self.assertEqual(definition.default_interval_seconds, 86400)
        self.assertEqual(
            definition.allowed_task_types,
            frozenset({"check", "auto_update", "update_all"}),
        )
        self.assertIs(definition.handler, self.market_tasks.run_market_task)
        self.assertIs(service.runner.registry, service.registry)
        self.assertIs(service.runner.repository, service.repository)

    async def test_lifespan_orders_start_and_stop_around_shared_clients(self):
        events = []
        service = SimpleNamespace(
            start=mock.AsyncMock(side_effect=lambda: events.append("service_start")),
            stop=mock.AsyncMock(side_effect=lambda: events.append("service_stop")),
        )
        initialize = mock.AsyncMock(side_effect=lambda: events.append("database_initialize"))
        legacy_stack, _ = self._patch_legacy_dependencies(events)
        with legacy_stack, mock.patch.object(self.main.database, "initialize", new=initialize), mock.patch.object(
            self.main,
            "create_production_automation_service",
            new=mock.AsyncMock(return_value=service),
        ):
            async with self.main.lifespan(self.main.app):
                self.assertIs(self.main.app.state.automation_service, service)
                self.assertLess(events.index("database_initialize"), events.index("service_start"))
                await asyncio.sleep(0)

        service.start.assert_awaited_once()
        service.stop.assert_awaited_once()
        self.assertIsNone(self.main.app.state.automation_service)
        self.assertLess(events.index("service_stop"), events.index("close_http"))

    async def test_real_service_waits_300_seconds_and_owns_one_handle(self):
        waiter = ControlledWaiter()
        handler = mock.AsyncMock(return_value=self.automation.AutomationHandlerResult())
        original_factory = self.market_tasks.create_market_automation_service
        legacy_stack, _ = self._patch_legacy_dependencies()

        async def factory(_client):
            with mock.patch.object(self.market_tasks, "run_market_task", handler):
                return original_factory(waiter_factory=lambda _definition: waiter)

        with legacy_stack, mock.patch.object(
            self.main,
            "create_production_automation_service",
            side_effect=factory,
        ):
            async with self.main.lifespan(self.main.app):
                service = self.main.app.state.automation_service
                await self._wait_until(lambda: waiter.calls == [300])
                self.assertEqual(set(service.schedulers), {"market_auto_update"})
                self.assertEqual(set(service.tasks), {"market_auto_update"})
                first_handle = service.tasks["market_auto_update"]
                self.assertEqual(await service.start(), 0)
                self.assertIs(service.tasks["market_auto_update"], first_handle)
                handler.assert_not_awaited()

            self.assertEqual(service.tasks, {})
            self.assertEqual(service.schedulers, {})
            self.assertFalse(waiter.pending)

    async def test_disabled_config_waits_without_running_handler(self):
        await self.db.update_automation_task_config("market_auto_update", enabled=False)
        waiter = ControlledWaiter()
        handler = mock.AsyncMock(return_value=self.automation.AutomationHandlerResult())
        original_factory = self.market_tasks.create_market_automation_service
        legacy_stack, _ = self._patch_legacy_dependencies()

        async def factory(_client):
            with mock.patch.object(self.market_tasks, "run_market_task", handler):
                return original_factory(waiter_factory=lambda _definition: waiter)

        with legacy_stack, mock.patch.object(
            self.main,
            "create_production_automation_service",
            side_effect=factory,
        ):
            async with self.main.lifespan(self.main.app):
                await self._wait_until(lambda: waiter.calls == [None])
                handler.assert_not_awaited()

    async def test_interrupted_recovery_finishes_before_scheduler_wait(self):
        await self.db.claim_automation_task(
            task_id="market_auto_update",
            conflict_group="market",
            task_type="auto_update",
            run_token="stale-run-token",
        )
        waiter = ControlledWaiter()
        original_factory = self.market_tasks.create_market_automation_service
        legacy_stack, _ = self._patch_legacy_dependencies()

        async def factory(_client):
            return original_factory(waiter_factory=lambda _definition: waiter)

        with legacy_stack, mock.patch.object(
            self.main,
            "create_production_automation_service",
            side_effect=factory,
        ):
            async with self.main.lifespan(self.main.app):
                await self._wait_until(lambda: waiter.calls == [300])
                state = await self.db.get_automation_task_state("market_auto_update")
                self.assertEqual(state["last_status"], "interrupted")

    async def test_shutdown_signals_running_handler_and_awaits_completion(self):
        waiter = ControlledWaiter()
        entered = asyncio.Event()
        release = asyncio.Event()
        contexts = []

        async def handler(context):
            contexts.append(context)
            entered.set()
            await release.wait()
            return self.automation.AutomationHandlerResult(status="cancelled")

        original_factory = self.market_tasks.create_market_automation_service
        legacy_stack, events = self._patch_legacy_dependencies()

        async def factory(_client):
            with mock.patch.object(self.market_tasks, "run_market_task", handler):
                return original_factory(waiter_factory=lambda _definition: waiter)

        with legacy_stack, mock.patch.object(
            self.main,
            "create_production_automation_service",
            side_effect=factory,
        ):
            manager = self.main.lifespan(self.main.app)
            await manager.__aenter__()
            await self._wait_until(lambda: waiter.calls == [300])
            waiter.release("timeout")
            await entered.wait()
            exit_task = asyncio.create_task(manager.__aexit__(None, None, None))
            await self._wait_until(lambda: contexts[0].stop_requested())
            self.assertNotIn("close_http", events)
            release.set()
            await exit_task

        self.assertIsNone(self.main.app.state.automation_service)
        self.assertIn("close_http", events)

    async def test_startup_failure_cleans_service_and_fails_loudly(self):
        events = []

        async def fail_start():
            events.append("service_start")
            raise RuntimeError("automation startup failed")

        service = SimpleNamespace(
            start=mock.AsyncMock(side_effect=fail_start),
            stop=mock.AsyncMock(side_effect=lambda: events.append("service_stop")),
        )
        legacy_stack, _ = self._patch_legacy_dependencies(events)
        with legacy_stack, mock.patch.object(
            self.main,
            "create_production_automation_service",
            new=mock.AsyncMock(return_value=service),
        ):
            with self.assertRaisesRegex(RuntimeError, "automation startup failed"):
                async with self.main.lifespan(self.main.app):
                    self.fail("startup 失败时不应进入应用生命周期")

        service.stop.assert_awaited_once()
        self.assertIsNone(self.main.app.state.automation_service)

    async def test_real_configuration_failure_blocks_startup_before_yield(self):
        legacy_stack, _ = self._patch_legacy_dependencies()
        configuration_error = self.automation.AutomationConfigurationError(
            "任务 conflict_group 与数据库配置不一致: market_auto_update"
        )
        with legacy_stack, mock.patch.object(
            self.automation.AutomationRepository,
            "ensure_config",
            new=mock.AsyncMock(side_effect=configuration_error),
        ):
            with self.assertRaises(self.automation.AutomationConfigurationError):
                async with self.main.lifespan(self.main.app):
                    self.fail("配置预检失败时不应进入应用生命周期")

        self.assertIsNone(self.main.app.state.automation_service)

    async def test_non_epg_legacy_tasks_still_start_once_with_market_automation_api(self):
        service = SimpleNamespace(
            start=mock.AsyncMock(),
            stop=mock.AsyncMock(),
        )
        legacy_stack, events = self._patch_legacy_dependencies()
        with legacy_stack, mock.patch.object(
            self.main,
            "create_production_automation_service",
            new=mock.AsyncMock(return_value=service),
        ):
            async with self.main.lifespan(self.main.app):
                await asyncio.sleep(0)

        for name in ("_rtsp_hls_cleanup_task", "logo"):
            self.assertEqual(events.count(name), 1)
        self.assertFalse(hasattr(self.main, "_epg_refresh_loop"))
        paths = {route.path for route in self.main.app.routes}
        self.assertIn("/api/admin/market/automation", paths)
        self.assertIn("/api/admin/market/check-updates", paths)
        self.assertIn("/api/admin/market/run-auto-update", paths)
        self.assertIn("/api/admin/market/update-all", paths)
        self.assertNotIn("/api/admin/market/automation/status", paths)

    async def test_rtsp_cleanup_has_one_owned_task_and_bounded_shutdown(self):
        entered = asyncio.Event()

        async def owned_cleanup(stop_event):
            entered.set()
            await stop_event.wait()

        with mock.patch.object(self.main, "_rtsp_hls_cleanup_task", new=owned_cleanup):
            first = self.main._start_rtsp_hls_cleanup()
            second = self.main._start_rtsp_hls_cleanup()
            self.assertIs(first, second)
            await entered.wait()
            await asyncio.wait_for(self.main._stop_rtsp_hls_cleanup(), timeout=1.0)

        self.assertTrue(first.done())
        self.assertIsNone(self.main._RTSP_HLS_CLEANUP_TASK)

    async def test_app_background_jobs_are_cancelled_and_drained(self):
        entered = asyncio.Event()
        finished = asyncio.Event()

        async def job():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        task = self.main._track_app_background_task(
            asyncio.create_task(job()), owner="test_background_job",
        )
        await entered.wait()
        await asyncio.wait_for(self.main._shutdown_app_background_tasks(), timeout=1.0)
        self.assertTrue(task.cancelled())
        self.assertTrue(finished.is_set())

    async def test_epg_source_crud_reconciles_committed_source_state(self):
        service = SimpleNamespace(is_started=True, stop=mock.AsyncMock())
        self.main.app.state.automation_service = service

        def request(body):
            return SimpleNamespace(
                app=self.main.app,
                json=mock.AsyncMock(return_value=body),
            )

        with mock.patch.object(
            self.main,
            "reconcile_epg_tasks_after_source_change",
            new=mock.AsyncMock(),
        ) as reconcile:
            created = await self.main.add_epg_source(request({
                "name": "Guide",
                "url": "https://guide.test/feed.xml",
            }))
            source_id = created["source"]["id"]
            reconcile.assert_awaited_with(
                service,
                self.main.http_client,
                source_id=source_id,
                operation="create",
            )

            updated = await self.main.update_epg_source(
                source_id,
                request({"enabled": False}),
            )
            self.assertFalse(updated["source"]["enabled"])
            reconcile.assert_awaited_with(
                service,
                self.main.http_client,
                source_id=source_id,
                operation="update",
            )

            deleted = await self.main.delete_epg_source(
                source_id,
                request({}),
                confirm=False,
            )
            self.assertEqual(deleted["status"], "deleted")
            reconcile.assert_awaited_with(
                service,
                self.main.http_client,
                source_id=source_id,
                operation="delete",
            )
        self.assertIsNone(await self.db.get_epg_source(source_id))

    async def test_manual_epg_refresh_response_stays_immediate_and_uses_run_now_wrapper(self):
        service = SimpleNamespace(is_started=True, stop=mock.AsyncMock())
        self.main.app.state.automation_service = service
        request = SimpleNamespace(app=self.main.app)
        completed = asyncio.Event()

        async def run(current_service):
            self.assertIs(current_service, service)
            completed.set()

        with mock.patch.object(self.main, "_run_manual_epg_refresh", side_effect=run) as refresh:
            response = await self.main.refresh_epg(request)
            self.assertEqual(response, {"ok": True})
            await completed.wait()
        refresh.assert_awaited_once_with(service)


if __name__ == "__main__":
    unittest.main()
