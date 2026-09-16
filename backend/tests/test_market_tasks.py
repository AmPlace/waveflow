import asyncio
import inspect
import os
import sys
import tempfile
import unittest
from unittest import mock


def _clear_modules():
    for name in list(sys.modules):
        if name in {"automation", "database", "market", "market_tasks"}:
            sys.modules.pop(name, None)


class RecordingContext:
    def __init__(self, task_type, run_token="market-task-run-token"):
        self.task_id = "market_auto_update"
        self.task_type = task_type
        self.trigger = "internal"
        self.parameters = {}
        self.run_token = run_token
        self.stop_event = asyncio.Event()
        self.progress = []

    def stop_requested(self):
        return self.stop_event.is_set()

    async def report_progress(self, **counts):
        self.progress.append(counts)


class MarketTasksTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        self._old_handle_secret = os.environ.get("WAVEFLOW_PROXY_HANDLE_SECRET")
        os.environ["WAVEFLOW_DB_PATH"] = os.path.join(self._tmpdir.name, "waveflow.db")
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "market-tasks-test-secret-32-bytes"
        _clear_modules()

        import automation
        import database as db
        import market
        import market_tasks

        self.automation = automation
        self.db = db
        self.market = market
        self.market_tasks = market_tasks
        await db.initialize()

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

    async def _create_install(self, package_id, installed_version="1.0.0", *, auto_update=False):
        subscription_id = await self.db.add_subscription(
            title=package_id,
            url=f"market://{package_id}",
            channel_count=1,
        )
        await self.db.add_channels_bulk(subscription_id, [{
            "name": package_id,
            "url": f"https://{package_id.replace('::', '-')}.test/live.m3u8",
            "source_type": "hls",
        }])
        await self.db.upsert_market_install(
            package_id=package_id,
            market_url="https://market.test/market.json",
            installed_subscription_id=subscription_id,
            installed_version=installed_version,
            auto_update=1 if auto_update else 0,
        )
        return subscription_id

    @staticmethod
    def _source_result(source_key, package_ids, *, status="success", error=""):
        return {
            "source_id": abs(hash(source_key)) % 10000 + 1,
            "source_key": source_key,
            "source_name": source_key,
            "requested_url": f"https://{source_key}.test/market.json",
            "current_url": f"https://{source_key}.test/market.json",
            "source_revision": source_key,
            "current_source_revision": source_key,
            "status": status,
            "usable_for_update": status == "success",
            "stale": status == "stale",
            "error": error,
            "cache_updated": status == "success",
            "package_count": len(package_ids) if status == "success" else 0,
            "package_ids": list(package_ids) if status == "success" else [],
        }

    @classmethod
    def _refresh_result(cls, *source_results):
        success = sum(item["status"] == "success" for item in source_results)
        failed = sum(item["status"] in {"stale", "failed", "revision_discarded"} for item in source_results)
        return {
            "refresh_status": "partial" if success and failed else "failed" if failed else "success",
            "source_results": list(source_results),
        }

    @staticmethod
    def _package(package_id, version):
        return {"id": package_id, "version": version}

    def _patch_market(self, refresh_result, packages, *, update_side_effect=None):
        return (
            mock.patch.object(self.market_tasks.market, "refresh_market", return_value=refresh_result),
            mock.patch.object(self.market_tasks.market, "market_packages_snapshot", return_value=packages),
            mock.patch.object(
                self.market_tasks.market,
                "update_installed_package",
                side_effect=update_side_effect,
            ),
        )

    async def test_market_task_definition_and_registration(self):
        definition = self.market_tasks.create_market_task_definition()
        self.assertEqual(definition.task_id, "market_auto_update")
        self.assertEqual(definition.conflict_group, "market")
        self.assertEqual(definition.allowed_task_types, frozenset({"check", "auto_update", "update_all"}))
        self.assertEqual(definition.scheduled_task_type, "auto_update")
        self.assertTrue(definition.default_enabled)
        self.assertEqual(definition.default_interval_seconds, 86400)
        self.assertEqual(definition.initial_delay_seconds, 300)
        self.assertTrue(definition.allow_automatic_scheduling)

        registry = self.automation.AutomationRegistry()
        registered = self.market_tasks.register_market_task(registry)
        self.assertIs(registry.get("market_auto_update"), registered)

    async def test_check_persists_all_version_statuses_without_updating(self):
        versions = {
            "same": ("1.0.0", "1.0.0"),
            "upgrade": ("1.0.0", "2.0.0"),
            "downgrade": ("2.0.0", "1.0.0"),
            "different": ("stable", "next"),
            "unknown": ("1.0.0", ""),
        }
        package_ids = []
        packages = []
        for expected, (installed, remote) in versions.items():
            package_id = f"pkg-{expected}"
            package_ids.append(package_id)
            packages.append(self._package(package_id, remote))
            await self._create_install(package_id, installed, auto_update=True)

        refresh = self._refresh_result(self._source_result("official", package_ids))
        refresh_patch, snapshot_patch, update_patch = self._patch_market(refresh, packages)
        with refresh_patch as refresh_mock, snapshot_patch, update_patch as update_mock:
            result = await self.market_tasks.run_market_task(RecordingContext("check"))

        refresh_mock.assert_awaited_once_with()
        update_mock.assert_not_awaited()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.checked_count, 5)
        self.assertEqual(result.updated_count, 0)
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(result.failed_count, 0)
        for expected in versions:
            install = await self.db.get_market_install(f"pkg-{expected}")
            self.assertEqual(install["version_status"], expected)
            self.assertTrue(install["last_checked_at"])

    async def test_unusable_sources_are_skipped_without_reusing_stale_cache(self):
        for source_key, status in (
            ("stale-source", "stale"),
            ("failed-source", "failed"),
            ("discarded-source", "revision_discarded"),
        ):
            package_id = f"{source_key}::pkg"
            await self._create_install(package_id, "1.0.0", auto_update=True)
            await self.db.update_market_install_check_state(
                package_id,
                checked_at="2026-08-04T00:00:00+00:00",
                remote_version="1.1.0",
                version_status="upgrade",
            )

        success_id = "official-pkg"
        await self._create_install(success_id, "1.0.0", auto_update=True)
        source_results = [
            self._source_result("official", [success_id]),
            self._source_result("stale-source", [], status="stale", error="timeout"),
            self._source_result("failed-source", [], status="failed", error="invalid schema"),
            self._source_result("discarded-source", [], status="revision_discarded", error="revision changed"),
        ]
        packages = [
            self._package(success_id, "1.0.0"),
            self._package("stale-source::pkg", "9.0.0"),
            self._package("failed-source::pkg", "9.0.0"),
            self._package("discarded-source::pkg", "9.0.0"),
        ]
        refresh_patch, snapshot_patch, update_patch = self._patch_market(
            self._refresh_result(*source_results),
            packages,
        )
        with refresh_patch, snapshot_patch, update_patch as update_mock:
            result = await self.market_tasks.run_market_task(RecordingContext("auto_update"))

        update_mock.assert_not_awaited()
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.checked_count, 1)
        self.assertEqual(result.skipped_count, 4)
        self.assertEqual(result.failed_count, 3)
        for source_key in ("stale-source", "failed-source", "discarded-source"):
            install = await self.db.get_market_install(f"{source_key}::pkg")
            self.assertEqual(install["last_checked_at"], "2026-08-04T00:00:00+00:00")
            self.assertEqual(install["remote_version"], "1.1.0")
            self.assertEqual(install["version_status"], "upgrade")

    async def test_auto_update_only_updates_enabled_upgrade(self):
        await self._create_install("enabled-upgrade", "1.0.0", auto_update=True)
        await self._create_install("disabled-upgrade", "1.0.0", auto_update=False)
        await self._create_install("same", "1.0.0", auto_update=True)
        package_ids = ["enabled-upgrade", "disabled-upgrade", "same"]
        packages = [
            self._package("enabled-upgrade", "2.0.0"),
            self._package("disabled-upgrade", "2.0.0"),
            self._package("same", "1.0.0"),
        ]
        refresh_patch, snapshot_patch, update_patch = self._patch_market(
            self._refresh_result(self._source_result("official", package_ids)),
            packages,
        )
        with refresh_patch, snapshot_patch, update_patch as update_mock:
            result = await self.market_tasks.run_market_task(RecordingContext("auto_update"))

        update_mock.assert_awaited_once_with("enabled-upgrade")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.checked_count, 3)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.skipped_count, 2)
        self.assertEqual(result.failed_count, 0)
        install = await self.db.get_market_install("enabled-upgrade")
        self.assertEqual(install["last_update_status"], "success")
        self.assertEqual(install["last_update_run_token"], "market-task-run-token")

    async def test_update_all_ignores_auto_update_flag_but_skips_non_upgrade(self):
        await self._create_install("auto-off", "1.0.0", auto_update=False)
        await self._create_install("auto-on", "1.0.0", auto_update=True)
        await self._create_install("downgrade", "2.0.0", auto_update=True)
        package_ids = ["auto-off", "auto-on", "downgrade"]
        packages = [
            self._package("auto-off", "2.0.0"),
            self._package("auto-on", "2.0.0"),
            self._package("downgrade", "1.0.0"),
        ]
        refresh_patch, snapshot_patch, update_patch = self._patch_market(
            self._refresh_result(self._source_result("official", package_ids)),
            packages,
        )
        with refresh_patch, snapshot_patch, update_patch as update_mock:
            result = await self.market_tasks.run_market_task(RecordingContext("update_all"))

        self.assertEqual(
            [call.args[0] for call in update_mock.await_args_list],
            ["auto-on", "auto-off"],
        )
        self.assertEqual(result.checked_count, 3)
        self.assertEqual(result.updated_count, 2)
        self.assertEqual(result.skipped_count, 1)

    async def test_official_and_third_party_same_original_id_do_not_collide(self):
        await self._create_install("shared", "1.0.0", auto_update=True)
        await self._create_install("third::shared", "1.0.0", auto_update=True)
        refresh = self._refresh_result(
            self._source_result("official", ["shared"]),
            self._source_result("third", ["third::shared"]),
        )
        packages = [self._package("shared", "2.0.0"), self._package("third::shared", "2.0.0")]
        refresh_patch, snapshot_patch, update_patch = self._patch_market(refresh, packages)
        with refresh_patch, snapshot_patch, update_patch as update_mock:
            result = await self.market_tasks.run_market_task(RecordingContext("update_all"))

        self.assertEqual({call.args[0] for call in update_mock.await_args_list}, {"shared", "third::shared"})
        self.assertEqual(result.updated_count, 2)

    async def test_missing_package_from_successful_source_persists_unknown(self):
        await self._create_install("removed-package", "1.0.0", auto_update=True)
        refresh_patch, snapshot_patch, update_patch = self._patch_market(
            self._refresh_result(self._source_result("official", [])),
            [],
        )
        with refresh_patch, snapshot_patch, update_patch as update_mock:
            result = await self.market_tasks.run_market_task(RecordingContext("auto_update"))

        update_mock.assert_not_awaited()
        install = await self.db.get_market_install("removed-package")
        self.assertEqual(install["version_status"], "unknown")
        self.assertEqual(install["remote_version"], "")
        self.assertEqual(result.checked_count, 1)
        self.assertEqual(result.skipped_count, 1)

    async def test_update_failure_preserves_install_and_continues(self):
        first_sub = await self._create_install("first", "1.0.0", auto_update=True)
        second_sub = await self._create_install("second", "1.0.0", auto_update=True)
        refresh = self._refresh_result(self._source_result("official", ["first", "second"]))
        packages = [self._package("first", "2.0.0"), self._package("second", "2.0.0")]

        async def update(package_id):
            if package_id == "first":
                raise self.market.MarketError("update failed", 502)
            return {"ok": True, "subscription_id": second_sub}

        refresh_patch, snapshot_patch, update_patch = self._patch_market(
            refresh,
            packages,
            update_side_effect=update,
        )
        with refresh_patch, snapshot_patch, update_patch as update_mock:
            result = await self.market_tasks.run_market_task(RecordingContext("update_all"))

        self.assertEqual([call.args[0] for call in update_mock.await_args_list], ["second", "first"])
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.failed_count, 1)
        first_install = await self.db.get_market_install("first")
        self.assertEqual(first_install["installed_subscription_id"], first_sub)
        self.assertEqual(first_install["installed_version"], "1.0.0")
        self.assertEqual(first_install["last_update_status"], "failed")
        self.assertIn("update failed", first_install["last_update_error"])
        self.assertIsNotNone(await self.db.get_subscription(first_sub))

    async def test_update_completion_persistence_failure_does_not_stop_other_packages(self):
        await self._create_install("first", "1.0.0", auto_update=True)
        await self._create_install("second", "1.0.0", auto_update=True)
        refresh = self._refresh_result(self._source_result("official", ["first", "second"]))
        packages = [self._package("first", "2.0.0"), self._package("second", "2.0.0")]
        original_complete = self.db.complete_market_install_update
        completion_calls = 0

        async def complete(*args, **kwargs):
            nonlocal completion_calls
            completion_calls += 1
            if completion_calls == 1:
                raise RuntimeError("state write failed")
            return await original_complete(*args, **kwargs)

        refresh_patch, snapshot_patch, update_patch = self._patch_market(refresh, packages)
        with refresh_patch, snapshot_patch, update_patch as update_mock, mock.patch.object(
            self.market_tasks.db,
            "complete_market_install_update",
            side_effect=complete,
        ):
            result = await self.market_tasks.run_market_task(RecordingContext("update_all"))

        self.assertEqual(update_mock.await_count, 2)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.updated_count, 2)
        self.assertEqual(result.failed_count, 1)
        self.assertTrue(any("状态完成保存失败" in error for error in result.errors))

    async def test_all_sources_failed_is_failed_and_no_update_is_success(self):
        failed_refresh = self._refresh_result(
            self._source_result("official", [], status="failed", error="offline")
        )
        refresh_patch, snapshot_patch, update_patch = self._patch_market(failed_refresh, [])
        with refresh_patch, snapshot_patch, update_patch:
            failed = await self.market_tasks.run_market_task(RecordingContext("check"))
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.failed_count, 1)

        success_refresh = self._refresh_result(self._source_result("official", []))
        refresh_patch, snapshot_patch, update_patch = self._patch_market(success_refresh, [])
        with refresh_patch, snapshot_patch, update_patch:
            success = await self.market_tasks.run_market_task(RecordingContext("update_all"))
        self.assertEqual(success.status, "success")
        self.assertEqual(success.updated_count, 0)

    async def test_stop_between_checks_returns_cancelled_with_completed_counts(self):
        await self._create_install("first", "1.0.0", auto_update=True)
        await self._create_install("second", "1.0.0", auto_update=True)
        context = RecordingContext("check")

        async def report_progress(**counts):
            context.progress.append(counts)
            if counts.get("checked_count") == 1:
                context.stop_event.set()

        context.report_progress = report_progress
        refresh_patch, snapshot_patch, update_patch = self._patch_market(
            self._refresh_result(self._source_result("official", ["first", "second"])),
            [self._package("first", "1.0.0"), self._package("second", "1.0.0")],
        )
        with refresh_patch, snapshot_patch, update_patch:
            result = await self.market_tasks.run_market_task(context)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.checked_count, 1)
        self.assertGreaterEqual(len(context.progress), 2)

    async def test_stop_during_current_update_finishes_it_and_skips_next(self):
        await self._create_install("first", "1.0.0", auto_update=True)
        await self._create_install("second", "1.0.0", auto_update=True)
        context = RecordingContext("update_all")

        async def update(_package_id):
            context.stop_event.set()
            return {"ok": True}

        refresh_patch, snapshot_patch, update_patch = self._patch_market(
            self._refresh_result(self._source_result("official", ["first", "second"])),
            [self._package("first", "2.0.0"), self._package("second", "2.0.0")],
            update_side_effect=update,
        )
        with refresh_patch, snapshot_patch, update_patch as update_mock:
            result = await self.market_tasks.run_market_task(context)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(update_mock.await_count, 1)
        self.assertEqual(result.updated_count, 1)
        updated_id = update_mock.await_args.args[0]
        self.assertEqual((await self.db.get_market_install(updated_id))["last_update_status"], "success")

    async def test_late_package_completion_cannot_overwrite_newer_token(self):
        await self._create_install("pkg", "1.0.0", auto_update=True)
        context = RecordingContext("update_all", run_token="old-token")

        async def update(_package_id):
            self.assertTrue(await self.db.mark_market_install_update_started(
                "pkg",
                run_token="new-token",
            ))
            return {"ok": True}

        refresh_patch, snapshot_patch, update_patch = self._patch_market(
            self._refresh_result(self._source_result("official", ["pkg"])),
            [self._package("pkg", "2.0.0")],
            update_side_effect=update,
        )
        with refresh_patch, snapshot_patch, update_patch:
            result = await self.market_tasks.run_market_task(context)

        install = await self.db.get_market_install("pkg")
        self.assertEqual(install["last_update_run_token"], "new-token")
        self.assertEqual(install["last_update_status"], "running")
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.failed_count, 1)

    async def test_existing_runner_executes_market_task_and_enforces_busy(self):
        await self._create_install("pkg", "1.0.0", auto_update=True)
        registry = self.automation.AutomationRegistry()
        self.market_tasks.register_market_task(registry)
        runner = self.automation.AutomationRunner(
            registry,
            token_factory=lambda: "runner-token",
        )
        refresh_patch, snapshot_patch, update_patch = self._patch_market(
            self._refresh_result(self._source_result("official", ["pkg"])),
            [self._package("pkg", "1.0.0")],
        )
        with refresh_patch, snapshot_patch, update_patch:
            result = await runner.run(self.automation.AutomationRunRequest(
                task_id="market_auto_update",
                task_type="check",
                trigger="internal",
            ))
        self.assertIsInstance(result, self.automation.AutomationRunResult)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.checked_count, 1)

        await self.db.claim_automation_task(
            task_id="market_auto_update",
            conflict_group="market",
            task_type="update_all",
            run_token="busy-token",
        )
        with mock.patch.object(self.market_tasks.market, "refresh_market") as refresh_mock:
            busy = await runner.run(self.automation.AutomationRunRequest(
                task_id="market_auto_update",
                task_type="update_all",
                trigger="internal",
            ))
        self.assertIsInstance(busy, self.automation.AutomationBusy)
        self.assertEqual(busy.conflict_group, "market")
        refresh_mock.assert_not_called()

    def test_handler_does_not_claim_complete_schedule_or_touch_api(self):
        source = inspect.getsource(self.market_tasks)
        self.assertNotIn("claim_automation_task", source)
        self.assertNotIn("complete_automation_task", source)
        self.assertNotIn("asyncio.create_task", source)
        self.assertNotIn("FastAPI", source)
        self.assertNotIn("APIRouter", source)
        self.assertNotIn("import main", source)
        self.assertNotIn("run_installed_updates", source)
        self.assertIn("context.report_progress", source)
        self.assertIn("update_installed_package", source)


if __name__ == "__main__":
    unittest.main()
