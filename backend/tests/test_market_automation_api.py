import asyncio
import dataclasses
import importlib
import os
import sys
import tempfile
import unittest
from collections import deque
from unittest import mock

import httpx


def _clear_modules():
    for name in list(sys.modules):
        if (
            name in {"main", "automation", "database", "epg", "epg_tasks", "market", "market_tasks"}
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


class MarketAutomationApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_env = {
            key: os.environ.get(key)
            for key in (
                "WAVEFLOW_DB_PATH",
                "WAVEFLOW_PROXY_HANDLE_SECRET",
                "WAVEFLOW_ANONYMOUS_BROWSE",
                "WAVEFLOW_ANONYMOUS_PLAYBACK",
            )
        }
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "waveflow.db")
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "market-automation-api-test-secret"
        os.environ["WAVEFLOW_ANONYMOUS_BROWSE"] = "1"
        os.environ["WAVEFLOW_ANONYMOUS_PLAYBACK"] = "1"
        _clear_modules()

        self.db = importlib.import_module("database")
        self.automation = importlib.import_module("automation")
        self.market_tasks = importlib.import_module("market_tasks")
        self.main = importlib.import_module("main")
        await self.db.initialize()

        self.handler = mock.AsyncMock(
            return_value=self.automation.AutomationHandlerResult(
                status="success",
                checked_count=2,
                updated_count=0,
                skipped_count=2,
            )
        )
        definition = dataclasses.replace(
            self.market_tasks.create_market_task_definition(),
            handler=self.handler,
        )
        registry = self.automation.AutomationRegistry()
        registry.register(definition)
        repository = self.automation.AutomationRepository(self.db)
        runner = self.automation.AutomationRunner(registry, repository)
        self.waiter = ControlledWaiter()
        self.service = self.automation.AutomationService(
            registry=registry,
            repository=repository,
            runner=runner,
            waiter_factory=lambda _definition: self.waiter,
        )
        await self.service.start()
        self.main.app.state.automation_service = self.service

        async def admin_override():
            return {"id": 1, "username": "admin", "role": "admin"}

        self.main.app.dependency_overrides[self.main.require_admin] = admin_override
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.main.app),
            base_url="http://testserver",
        )
        await self._wait_until(lambda: self.waiter.calls == [300])

    async def asyncTearDown(self):
        await self.client.aclose()
        self.main.app.dependency_overrides.clear()
        await self.service.stop(timeout_seconds=0.2)
        self.main.app.state.automation_service = None
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

    async def _post(self, path, json=None):
        return await self.client.post(
            path,
            json=json,
            headers={"X-WaveFlow-Request": "1"},
        )

    async def _patch(self, path, json):
        return await self.client.patch(
            path,
            json=json,
            headers={"X-WaveFlow-Request": "1"},
        )

    async def test_get_returns_default_config_never_run_and_no_internal_fields(self):
        response = await self.client.get("/api/admin/market/automation")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["task_id"], "market_auto_update")
        self.assertTrue(body["enabled"])
        self.assertEqual(body["interval_seconds"], 86400)
        self.assertEqual(body["minimum_interval_seconds"], 300)
        self.assertEqual(body["maximum_interval_seconds"], 2678400)
        self.assertEqual(body["initial_delay_seconds"], 300)
        self.assertEqual(body["scheduled_task_type"], "auto_update")
        self.assertTrue(body["service_started"])
        self.assertFalse(body["is_running"])
        self.assertEqual(body["last_status"], "never_run")
        self.assertNotIn("run_token", body)
        self.assertNotIn("task_handle", body)

    async def test_get_requires_admin(self):
        self.main.app.dependency_overrides.clear()

        response = await self.client.get("/api/admin/market/automation")

        self.assertEqual(response.status_code, 401)

    async def test_all_new_operation_routes_require_admin(self):
        self.main.app.dependency_overrides.clear()
        requests = (
            ("GET", "/api/admin/market/automation", None),
            ("PATCH", "/api/admin/market/automation", {"enabled": False}),
            ("POST", "/api/admin/market/check-updates", None),
            ("POST", "/api/admin/market/run-auto-update", None),
            ("POST", "/api/admin/market/update-all", None),
        )
        for method, path, payload in requests:
            with self.subTest(method=method, path=path):
                response = await self.client.request(
                    method,
                    path,
                    json=payload,
                    headers={"X-WaveFlow-Request": "1"},
                )
                self.assertEqual(response.status_code, 401)

    async def test_patch_supports_partial_updates_and_notifies_same_service(self):
        notify = mock.Mock(wraps=self.service.notify_config_changed)
        with mock.patch.object(self.service, "notify_config_changed", notify):
            disabled = await self._patch(
                "/api/admin/market/automation",
                {"enabled": False},
            )
            self.assertEqual(disabled.status_code, 200)
            self.assertFalse(disabled.json()["enabled"])
            self.assertEqual(disabled.json()["interval_seconds"], 86400)
            self.handler.assert_not_awaited()

            changed = await self._patch(
                "/api/admin/market/automation",
                {"interval_seconds": 900},
            )
            self.assertEqual(changed.status_code, 200)
            self.assertFalse(changed.json()["enabled"])
            self.assertEqual(changed.json()["interval_seconds"], 900)

            enabled = await self._patch(
                "/api/admin/market/automation",
                {"enabled": True},
            )
            self.assertEqual(enabled.status_code, 200)
            self.assertTrue(enabled.json()["enabled"])
            self.assertEqual(enabled.json()["interval_seconds"], 900)

        self.assertEqual(notify.call_args_list, [
            mock.call("market_auto_update"),
            mock.call("market_auto_update"),
            mock.call("market_auto_update"),
        ])
        await self._wait_until(lambda: self.waiter.calls[-1] == 300)
        self.handler.assert_not_awaited()

    async def test_patch_rejects_empty_invalid_types_and_out_of_range_interval(self):
        cases = (
            ({}, 422),
            ({"enabled": "false"}, 422),
            ({"interval_seconds": True}, 422),
            ({"interval_seconds": 299}, 422),
            ({"interval_seconds": 2678401}, 422),
        )
        for payload, expected_status in cases:
            with self.subTest(payload=payload):
                response = await self._patch("/api/admin/market/automation", payload)
                self.assertEqual(response.status_code, expected_status)

    async def test_new_operation_routes_use_same_runner_and_manual_trigger(self):
        results = {
            "check": self.automation.AutomationRunResult(
                task_id="market_auto_update",
                task_type="check",
                status="success",
                checked_count=3,
                updated_count=0,
                skipped_count=3,
                failed_count=0,
                error="",
                started_at="2026-08-05T00:00:00+00:00",
                finished_at="2026-08-05T00:00:01+00:00",
            ),
            "auto_update": self.automation.AutomationRunResult(
                task_id="market_auto_update",
                task_type="auto_update",
                status="partial",
                checked_count=3,
                updated_count=1,
                skipped_count=1,
                failed_count=1,
                error="一个包失败",
                started_at="2026-08-05T00:00:00+00:00",
                finished_at="2026-08-05T00:00:01+00:00",
                errors=("一个包失败",),
            ),
            "update_all": self.automation.AutomationRunResult(
                task_id="market_auto_update",
                task_type="update_all",
                status="failed",
                checked_count=2,
                updated_count=0,
                skipped_count=1,
                failed_count=1,
                error="来源不可用",
                started_at="2026-08-05T00:00:00+00:00",
                finished_at="2026-08-05T00:00:01+00:00",
                errors=("来源不可用",),
            ),
        }

        async def run(request):
            return results[request.task_type]

        with mock.patch.object(self.service.runner, "run", side_effect=run) as runner:
            check = await self._post("/api/admin/market/check-updates")
            auto_update = await self._post("/api/admin/market/run-auto-update")
            update_all = await self._post("/api/admin/market/update-all")

        self.assertEqual(check.status_code, 200)
        self.assertEqual(check.json()["updated_count"], 0)
        self.assertEqual(auto_update.status_code, 200)
        self.assertEqual(auto_update.json()["status"], "partial")
        self.assertEqual(update_all.status_code, 200)
        self.assertEqual(update_all.json()["status"], "failed")
        self.assertEqual(
            [(call.args[0].task_type, call.args[0].trigger) for call in runner.await_args_list],
            [
                ("check", "manual_api"),
                ("auto_update", "manual_api"),
                ("update_all", "manual_api"),
            ],
        )

    async def test_manual_auto_update_runs_while_scheduling_is_disabled(self):
        await self.service.repository.update_config(
            "market_auto_update",
            enabled=False,
        )

        response = await self._post("/api/admin/market/run-auto-update")

        self.assertEqual(response.status_code, 200)
        self.handler.assert_awaited_once()
        request = self.handler.await_args.args[0]
        self.assertEqual(request.task_type, "auto_update")
        self.assertEqual(request.trigger, "manual_api")

    async def test_busy_returns_stable_409_without_run_token_or_second_handler(self):
        claimed = await self.service.repository.claim(
            "market_auto_update",
            "market",
            "check",
            "secret-run-token",
            "2026-08-05T00:00:00+00:00",
        )
        self.assertTrue(claimed.claimed)

        status_response = await self.client.get("/api/admin/market/automation")
        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.json()["is_running"])
        self.assertEqual(status_response.json()["task_type"], "check")
        self.assertNotIn("run_token", status_response.json())

        response = await self._post("/api/admin/market/update-all")

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "automation_busy")
        self.assertEqual(body["current_task_type"], "check")
        self.assertEqual(body["started_at"], "2026-08-05T00:00:00+00:00")
        self.assertEqual(body["status"], "running")
        self.assertNotIn("run_token", body)
        self.handler.assert_not_awaited()

        legacy = await self._post("/api/admin/market/updates/run")
        self.assertEqual(legacy.status_code, 409)
        self.assertEqual(legacy.json()["current_task_type"], "check")
        self.handler.assert_not_awaited()

    async def test_legacy_updates_route_uses_update_all_and_preserves_frontend_fields(self):
        result = self.automation.AutomationRunResult(
            task_id="market_auto_update",
            task_type="update_all",
            status="partial",
            checked_count=4,
            updated_count=2,
            skipped_count=1,
            failed_count=1,
            error="一个包失败",
            started_at="2026-08-05T00:00:00+00:00",
            finished_at="2026-08-05T00:00:01+00:00",
            errors=("一个包失败",),
        )
        with mock.patch.object(self.service.runner, "run", return_value=result) as runner, \
                mock.patch.object(self.main._market, "run_installed_updates") as legacy:
            response = await self._post(
                "/api/admin/market/updates/run",
                {"auto_update_only": True},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["task_type"], "update_all")
        self.assertEqual(body["updated"], 2)
        self.assertEqual(body["skipped"], 1)
        self.assertEqual(body["failed"], 1)
        self.assertEqual(body["results"], [])
        self.assertEqual(runner.await_args.args[0].task_type, "update_all")
        legacy.assert_not_awaited()

    async def test_infrastructure_error_is_sanitized_and_service_missing_is_stable(self):
        with mock.patch.object(
            self.service.runner,
            "run",
            side_effect=RuntimeError("boom /Users/example/private.py traceback details"),
        ):
            failed = await self._post("/api/admin/market/check-updates")

        self.assertEqual(failed.status_code, 500)
        self.assertNotIn("/Users/example", failed.text)
        self.assertNotIn("traceback", failed.text.lower())

        self.main.app.state.automation_service = None
        unavailable = await self.client.get("/api/admin/market/automation")
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["detail"]["code"], "automation_unavailable")
        unavailable_run = await self._post("/api/admin/market/check-updates")
        self.assertEqual(unavailable_run.status_code, 503)
        self.assertEqual(
            unavailable_run.json()["detail"]["code"],
            "automation_unavailable",
        )

    async def test_package_auto_update_change_does_not_notify_or_run_global_task(self):
        with mock.patch.object(self.service, "notify_config_changed") as notify, \
                mock.patch.object(
                    self.main._market,
                    "update_install_config",
                    return_value={"package_id": "pkg", "auto_update": True},
                ):
            response = await self._patch(
                "/api/admin/market/packages/pkg/install",
                {"auto_update": True},
            )

        self.assertEqual(response.status_code, 200)
        notify.assert_not_called()
        self.handler.assert_not_awaited()

    async def test_routes_do_not_create_service_runner_or_background_task(self):
        before_handles = self.service.tasks
        with mock.patch.object(self.main, "create_production_automation_service") as factory:
            check_response = await self._post("/api/admin/market/check-updates")
            patch_response = await self._patch(
                "/api/admin/market/automation",
                {"interval_seconds": 900},
            )
        await asyncio.sleep(0)
        after_handles = self.service.tasks

        self.assertEqual(check_response.status_code, 200)
        self.assertEqual(patch_response.status_code, 200)
        factory.assert_not_called()
        self.assertEqual(set(after_handles), set(before_handles))
        self.assertIs(
            after_handles["market_auto_update"],
            before_handles["market_auto_update"],
        )

    async def test_legacy_route_is_marked_deprecated(self):
        route = next(
            route
            for route in self.main.app.routes
            if route.path == "/api/admin/market/updates/run"
        )
        self.assertTrue(route.deprecated)


if __name__ == "__main__":
    unittest.main()
