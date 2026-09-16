import asyncio
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


MARKET_TASK_ID = "market_auto_update"
MARKET_CONFLICT_GROUP = "market"


def _clear_database_module():
    for module_name in list(sys.modules):
        if module_name == "database" or module_name in {
            "security.secrets",
            "security.source_ids",
        }:
            sys.modules.pop(module_name, None)


class AutomationPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = os.environ.get("WAVEFLOW_DB_PATH")
        self._old_handle_secret = os.environ.get("WAVEFLOW_PROXY_HANDLE_SECRET")
        self.db_path = os.path.join(self._tmpdir.name, "waveflow.db")
        os.environ["WAVEFLOW_DB_PATH"] = self.db_path
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "automation-persistence-test-secret-32-bytes"
        _clear_database_module()
        import database

        self.db = database

    async def asyncTearDown(self):
        if self._old_db_path is None:
            os.environ.pop("WAVEFLOW_DB_PATH", None)
        else:
            os.environ["WAVEFLOW_DB_PATH"] = self._old_db_path
        if self._old_handle_secret is None:
            os.environ.pop("WAVEFLOW_PROXY_HANDLE_SECRET", None)
        else:
            os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = self._old_handle_secret
        _clear_database_module()
        self._tmpdir.cleanup()

    def _raw_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def _create_install(self, package_id="pkg"):
        subscription_id = await self.db.add_subscription(
            title=package_id,
            url=f"market://{package_id}",
            channel_count=1,
        )
        await self.db.add_channels_bulk(subscription_id, [
            {
                "name": package_id,
                "url": f"https://{package_id}.test/live.m3u8",
                "source_type": "hls",
            },
        ])
        await self.db.upsert_market_install(
            package_id=package_id,
            market_url="https://market.test/index.json",
            installed_subscription_id=subscription_id,
            installed_version="1.0.0",
        )
        return subscription_id

    async def test_new_database_schema_defaults_and_repeated_initialize(self):
        await self.db.initialize()
        await self.db.initialize()

        conn = self._raw_connection()
        try:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            indexes = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        finally:
            conn.close()

        self.assertIn("automation_task_config", tables)
        self.assertIn("automation_task_state", tables)
        self.assertIn("idx_automation_running_conflict_group", indexes)

        config = await self.db.get_automation_task_config(MARKET_TASK_ID)
        self.assertEqual(config["conflict_group"], MARKET_CONFLICT_GROUP)
        self.assertEqual(config["enabled"], 1)
        self.assertEqual(config["interval_seconds"], 86400)
        self.assertIn(
            MARKET_TASK_ID,
            {item["task_id"] for item in await self.db.list_automation_task_configs()},
        )

        state = await self.db.get_automation_task_state(MARKET_TASK_ID)
        self.assertEqual(state["last_status"], "never_run")
        self.assertEqual(state["conflict_group"], MARKET_CONFLICT_GROUP)

        await self.db.update_automation_task_config(
            MARKET_TASK_ID,
            enabled=False,
            interval_seconds=3600,
        )
        await self.db.initialize()
        config = await self.db.get_automation_task_config(MARKET_TASK_ID)
        self.assertEqual(config["enabled"], 0)
        self.assertEqual(config["interval_seconds"], 3600)

    async def test_config_update_persists_across_reload_and_rejects_invalid_interval(self):
        await self.db.initialize()
        await self.db.update_automation_task_config(
            MARKET_TASK_ID,
            enabled=False,
            interval_seconds=7200,
        )

        _clear_database_module()
        self.db = importlib.import_module("database")
        await self.db.initialize()
        config = await self.db.get_automation_task_config(MARKET_TASK_ID)
        self.assertEqual(config["enabled"], 0)
        self.assertEqual(config["interval_seconds"], 7200)

        for invalid in (0, -1, True, "3600"):
            with self.subTest(interval=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    await self.db.update_automation_task_config(
                        MARKET_TASK_ID,
                        interval_seconds=invalid,
                    )

    async def test_existing_m1_database_upgrades_without_data_loss(self):
        await self.db.initialize()
        from security.source_ids import source_id_for

        subscription_id = await self._create_install()
        before_channels = await self.db.get_channels(subscription_id)
        before_source_ids = [source_id_for(row) for row in before_channels]

        conn = self._raw_connection()
        try:
            install = conn.execute(
                "SELECT * FROM market_packages_installed WHERE package_id='pkg'"
            ).fetchone()
            conn.execute("DROP INDEX IF EXISTS idx_automation_running_conflict_group")
            conn.execute("DROP TABLE automation_task_state")
            conn.execute("DROP TABLE automation_task_config")
            conn.execute("ALTER TABLE market_packages_installed RENAME TO market_packages_installed_m2")
            conn.execute(
                """
                CREATE TABLE market_packages_installed (
                    package_id TEXT PRIMARY KEY,
                    market_url TEXT NOT NULL,
                    installed_subscription_id INTEGER,
                    installed_version TEXT DEFAULT '',
                    installed_at TEXT NOT NULL,
                    auto_update INTEGER DEFAULT 0,
                    metadata_json TEXT DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO market_packages_installed(
                    package_id, market_url, installed_subscription_id,
                    installed_version, installed_at, auto_update, metadata_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(install[key] for key in (
                    "package_id",
                    "market_url",
                    "installed_subscription_id",
                    "installed_version",
                    "installed_at",
                    "auto_update",
                    "metadata_json",
                )),
            )
            conn.execute("DROP TABLE market_packages_installed_m2")
            conn.commit()
        finally:
            conn.close()

        await self.db.initialize()
        install_after = await self.db.get_market_install("pkg")
        channels_after = await self.db.get_channels(subscription_id)

        self.assertEqual(install_after["installed_subscription_id"], subscription_id)
        self.assertEqual(install_after["installed_version"], "1.0.0")
        self.assertEqual(install_after["version_status"], "unknown")
        self.assertEqual(
            [(row["id"], row["url"]) for row in channels_after],
            [(row["id"], row["url"]) for row in before_channels],
        )
        self.assertEqual([source_id_for(row) for row in channels_after], before_source_ids)

    async def test_conflict_group_claim_is_atomic_and_non_queuing(self):
        await self.db.initialize()
        await self.db.ensure_automation_task_config("task-a", "group-a", True, 60)
        await self.db.ensure_automation_task_config("task-b", "group-a", True, 60)
        await self.db.ensure_automation_task_config("task-c", "group-c", True, 60)

        first = await self.db.claim_automation_task(
            task_id="task-a",
            conflict_group="group-a",
            task_type="check",
            run_token="token-a",
            started_at="2026-08-05T01:00:00+00:00",
        )
        second = await self.db.claim_automation_task(
            task_id="task-b",
            conflict_group="group-a",
            task_type="update_all",
            run_token="token-b",
            started_at="2026-08-05T01:00:01+00:00",
        )
        other_group = await self.db.claim_automation_task(
            task_id="task-c",
            conflict_group="group-c",
            task_type="check",
            run_token="token-c",
            started_at="2026-08-05T01:00:02+00:00",
        )

        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertEqual(second["current"]["task_id"], "task-a")
        self.assertEqual(second["current"]["task_type"], "check")
        self.assertEqual(second["current"]["last_started_at"], "2026-08-05T01:00:00+00:00")
        self.assertEqual(second["current"]["last_status"], "running")
        self.assertTrue(other_group["claimed"])

        busy = await self.db.get_automation_conflict_group_state("group-a")
        self.assertEqual(busy["task_id"], "task-a")
        self.assertEqual(busy["last_status"], "running")

    async def test_concurrent_claims_in_same_group_have_one_winner(self):
        await self.db.initialize()
        await self.db.ensure_automation_task_config("task-a", "shared", True, 60)
        await self.db.ensure_automation_task_config("task-b", "shared", True, 60)

        results = await asyncio.gather(
            self.db.claim_automation_task(
                task_id="task-a",
                conflict_group="shared",
                task_type="check",
                run_token="token-a",
                started_at="2026-08-05T02:00:00+00:00",
            ),
            self.db.claim_automation_task(
                task_id="task-b",
                conflict_group="shared",
                task_type="update_all",
                run_token="token-b",
                started_at="2026-08-05T02:00:00+00:00",
            ),
        )

        self.assertEqual(sum(1 for result in results if result["claimed"]), 1)
        self.assertEqual(sum(1 for result in results if not result["claimed"]), 1)

    async def test_run_token_conditionally_updates_progress_and_completion(self):
        await self.db.initialize()
        claimed = await self.db.claim_automation_task(
            task_id=MARKET_TASK_ID,
            conflict_group=MARKET_CONFLICT_GROUP,
            task_type="check",
            run_token="token-1",
            started_at="2026-08-05T03:00:00+00:00",
        )
        self.assertTrue(claimed["claimed"])
        self.assertFalse(await self.db.update_automation_task_progress(
            MARKET_TASK_ID,
            "wrong-token",
            checked_count=9,
        ))
        self.assertTrue(await self.db.update_automation_task_progress(
            MARKET_TASK_ID,
            "token-1",
            checked_count=4,
            updated_count=1,
            skipped_count=2,
            failed_count=1,
        ))
        self.assertFalse(await self.db.complete_automation_task(
            MARKET_TASK_ID,
            "wrong-token",
            status="success",
            finished_at="2026-08-05T03:01:00+00:00",
        ))
        self.assertTrue(await self.db.complete_automation_task(
            MARKET_TASK_ID,
            "token-1",
            status="partial",
            finished_at="2026-08-05T03:01:00+00:00",
            checked_count=4,
            updated_count=1,
            skipped_count=2,
            failed_count=1,
            error="one package failed",
        ))

        state = await self.db.get_automation_task_state(MARKET_TASK_ID)
        self.assertEqual(state["last_status"], "partial")
        self.assertEqual(state["checked_count"], 4)
        self.assertEqual(state["failed_count"], 1)

        next_claim = await self.db.claim_automation_task(
            task_id=MARKET_TASK_ID,
            conflict_group=MARKET_CONFLICT_GROUP,
            task_type="auto_update",
            run_token="token-2",
            started_at="2026-08-05T03:02:00+00:00",
        )
        self.assertTrue(next_claim["claimed"])
        self.assertFalse(await self.db.complete_automation_task(
            MARKET_TASK_ID,
            "token-1",
            status="success",
            finished_at="2026-08-05T03:03:00+00:00",
        ))
        self.assertEqual(
            (await self.db.get_automation_task_state(MARKET_TASK_ID))["run_token"],
            "token-2",
        )

    async def test_all_final_statuses_and_counts_are_persisted(self):
        await self.db.initialize()
        for index, status in enumerate(("success", "partial", "failed", "cancelled"), start=1):
            task_id = f"task-{status}"
            group = f"group-{status}"
            token = f"token-{status}"
            await self.db.ensure_automation_task_config(task_id, group, True, 60)
            result = await self.db.claim_automation_task(
                task_id=task_id,
                conflict_group=group,
                task_type="check",
                run_token=token,
                started_at=f"2026-08-05T04:00:0{index}+00:00",
            )
            self.assertTrue(result["claimed"])
            self.assertTrue(await self.db.complete_automation_task(
                task_id,
                token,
                status=status,
                finished_at=f"2026-08-05T04:01:0{index}+00:00",
                checked_count=index,
                updated_count=index + 1,
                skipped_count=index + 2,
                failed_count=index + 3,
                error="expected" if status in {"partial", "failed"} else "",
            ))
            state = await self.db.get_automation_task_state(task_id)
            self.assertEqual(state["last_status"], status)
            self.assertEqual(state["checked_count"], index)
            self.assertEqual(state["updated_count"], index + 1)
            self.assertEqual(state["skipped_count"], index + 2)
            self.assertEqual(state["failed_count"], index + 3)

    async def test_interrupted_recovery_is_idempotent_and_preserves_counts(self):
        await self.db.initialize()
        await self.db.claim_automation_task(
            task_id=MARKET_TASK_ID,
            conflict_group=MARKET_CONFLICT_GROUP,
            task_type="update_all",
            run_token="token-running",
            started_at="2026-08-05T05:00:00+00:00",
        )
        await self.db.update_automation_task_progress(
            MARKET_TASK_ID,
            "token-running",
            checked_count=7,
            updated_count=3,
            skipped_count=2,
            failed_count=2,
        )

        completed_task = "completed-task"
        await self.db.ensure_automation_task_config(completed_task, "completed", True, 60)
        await self.db.claim_automation_task(
            task_id=completed_task,
            conflict_group="completed",
            task_type="check",
            run_token="completed-token",
            started_at="2026-08-05T05:00:00+00:00",
        )
        await self.db.complete_automation_task(
            completed_task,
            "completed-token",
            status="success",
            finished_at="2026-08-05T05:01:00+00:00",
            checked_count=1,
        )

        recovered = await self.db.recover_interrupted_automation_tasks(
            finished_at="2026-08-05T05:02:00+00:00"
        )
        self.assertEqual(recovered, 1)
        state = await self.db.get_automation_task_state(MARKET_TASK_ID)
        self.assertEqual(state["last_status"], "interrupted")
        self.assertEqual(state["last_finished_at"], "2026-08-05T05:02:00+00:00")
        self.assertEqual(state["checked_count"], 7)
        self.assertEqual(state["updated_count"], 3)
        self.assertEqual(state["skipped_count"], 2)
        self.assertEqual(state["failed_count"], 2)
        self.assertIn("中断", state["last_error"])

        self.assertEqual(await self.db.recover_interrupted_automation_tasks(), 0)
        self.assertEqual(
            (await self.db.get_automation_task_state(completed_task))["last_status"],
            "success",
        )

    async def test_error_messages_are_plain_sanitized_and_bounded(self):
        await self.db.initialize()
        await self.db.claim_automation_task(
            task_id=MARKET_TASK_ID,
            conflict_group=MARKET_CONFLICT_GROUP,
            task_type="check",
            run_token="token-error",
            started_at="2026-08-05T06:00:00+00:00",
        )
        error = "bad\x00response" + ("x" * 5000)
        await self.db.complete_automation_task(
            MARKET_TASK_ID,
            "token-error",
            status="failed",
            finished_at="2026-08-05T06:01:00+00:00",
            error=error,
        )
        stored = (await self.db.get_automation_task_state(MARKET_TASK_ID))["last_error"]
        self.assertLessEqual(len(stored), self.db.AUTOMATION_ERROR_MAX_LENGTH)
        self.assertNotIn("\x00", stored)

        with self.assertRaises(TypeError):
            await self.db.recover_interrupted_automation_tasks(error=RuntimeError("boom"))

    async def test_market_install_check_and_update_state_are_token_guarded(self):
        await self.db.initialize()
        await self._create_install()
        install = await self.db.get_market_install("pkg")
        self.assertEqual(install["version_status"], "unknown")
        self.assertEqual(install["last_update_status"], "never_run")
        self.assertEqual(install["last_update_run_token"], "")

        self.assertTrue(await self.db.update_market_install_check_state(
            "pkg",
            checked_at="2026-08-05T07:00:00+00:00",
            remote_version="1.1.0",
            version_status="upgrade",
        ))
        self.assertFalse(await self.db.update_market_install_check_state(
            "missing",
            checked_at="2026-08-05T07:00:00+00:00",
            remote_version="1.1.0",
            version_status="upgrade",
        ))
        self.assertFalse(await self.db.mark_market_install_update_started(
            "missing",
            run_token="missing-token",
            started_at="2026-08-05T07:00:00+00:00",
        ))

        self.assertTrue(await self.db.mark_market_install_update_started(
            "pkg",
            run_token="package-token-1",
            started_at="2026-08-05T07:01:00+00:00",
        ))
        self.assertFalse(await self.db.complete_market_install_update(
            "pkg",
            run_token="wrong-token",
            status="success",
            finished_at="2026-08-05T07:02:00+00:00",
        ))
        self.assertTrue(await self.db.complete_market_install_update(
            "pkg",
            run_token="package-token-1",
            status="success",
            finished_at="2026-08-05T07:02:00+00:00",
        ))

        self.assertTrue(await self.db.mark_market_install_update_started(
            "pkg",
            run_token="package-token-2",
            started_at="2026-08-05T07:03:00+00:00",
        ))
        self.assertFalse(await self.db.complete_market_install_update(
            "pkg",
            run_token="package-token-1",
            status="failed",
            finished_at="2026-08-05T07:04:00+00:00",
            error="late failure",
        ))
        self.assertTrue(await self.db.complete_market_install_update(
            "pkg",
            run_token="package-token-2",
            status="failed",
            finished_at="2026-08-05T07:04:00+00:00",
            error="remote failed" + ("x" * 5000),
        ))

        install = await self.db.get_market_install("pkg")
        self.assertEqual(install["last_checked_at"], "2026-08-05T07:00:00+00:00")
        self.assertEqual(install["remote_version"], "1.1.0")
        self.assertEqual(install["version_status"], "upgrade")
        self.assertEqual(install["last_update_status"], "failed")
        self.assertTrue(install["last_update_error"].startswith("remote failed"))
        self.assertLessEqual(
            len(install["last_update_error"]),
            self.db.AUTOMATION_ERROR_MAX_LENGTH,
        )
        self.assertEqual(install["last_update_run_token"], "package-token-2")

    async def test_m1_atomic_update_and_upsert_preserve_automation_state_and_identity(self):
        await self.db.initialize()
        from security.source_ids import source_id_for

        subscription_id = await self.db.install_market_package_atomic(
            package_id="pkg",
            market_url="https://market.test/index.json",
            title="Package",
            subscription_url="market://pkg",
            channels=[{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}],
            installed_version="1.0.0",
        )
        before_channel = (await self.db.get_channels(subscription_id))[0]
        before_identity = (before_channel["id"], source_id_for(before_channel))
        await self.db.update_market_install_check_state(
            "pkg",
            checked_at="2026-08-05T08:00:00+00:00",
            remote_version="1.1.0",
            version_status="upgrade",
        )
        await self.db.mark_market_install_update_started(
            "pkg",
            run_token="package-token",
            started_at="2026-08-05T08:01:00+00:00",
        )
        await self.db.complete_market_install_update(
            "pkg",
            run_token="package-token",
            status="success",
            finished_at="2026-08-05T08:02:00+00:00",
        )

        updated_subscription_id = await self.db.install_market_package_atomic(
            package_id="pkg",
            market_url="https://market.test/index.json",
            title="Package renamed",
            subscription_url="market://pkg",
            channels=[{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}],
            installed_version="1.1.0",
        )
        await self.db.upsert_market_install(
            package_id="pkg",
            market_url="https://market.test/index.json",
            installed_subscription_id=subscription_id,
            installed_version="1.1.0",
        )

        after_channel = (await self.db.get_channels(subscription_id))[0]
        install = await self.db.get_market_install("pkg")
        self.assertEqual(updated_subscription_id, subscription_id)
        self.assertEqual((after_channel["id"], source_id_for(after_channel)), before_identity)
        self.assertEqual(install["last_checked_at"], "2026-08-05T08:00:00+00:00")
        self.assertEqual(install["remote_version"], "1.1.0")
        self.assertEqual(install["version_status"], "upgrade")
        self.assertEqual(install["last_update_status"], "success")
        self.assertEqual(install["last_update_run_token"], "package-token")

    async def test_m1_atomic_update_failure_rolls_back_data_and_install_state(self):
        await self.db.initialize()
        subscription_id = await self.db.install_market_package_atomic(
            package_id="pkg",
            market_url="https://market.test/index.json",
            title="Package",
            subscription_url="market://pkg",
            channels=[{"name": "A", "url": "https://a.test/live.m3u8", "source_type": "hls"}],
            installed_version="1.0.0",
        )
        await self.db.update_market_install_check_state(
            "pkg",
            checked_at="2026-08-05T09:00:00+00:00",
            remote_version="1.1.0",
            version_status="upgrade",
        )
        before_install = await self.db.get_market_install("pkg")
        before_subscription = await self.db.get_subscription(subscription_id)
        before_channels = await self.db.get_channels(subscription_id)

        with mock.patch.object(self.db, "_sync_channels_conn", side_effect=RuntimeError("sync failed")):
            with self.assertRaisesRegex(RuntimeError, "sync failed"):
                await self.db.install_market_package_atomic(
                    package_id="pkg",
                    market_url="https://market.test/index.json",
                    title="Broken update",
                    subscription_url="market://pkg",
                    channels=[{"name": "B", "url": "https://b.test/live.m3u8", "source_type": "hls"}],
                    installed_version="1.1.0",
                )

        self.assertEqual(await self.db.get_market_install("pkg"), before_install)
        self.assertEqual(await self.db.get_subscription(subscription_id), before_subscription)
        self.assertEqual(await self.db.get_channels(subscription_id), before_channels)

    async def test_uninstall_still_removes_install_subscription_and_channels(self):
        await self.db.initialize()
        subscription_id = await self._create_install()
        removed = await self.db.uninstall_market_package_atomic("pkg")

        self.assertTrue(removed)
        self.assertIsNone(await self.db.get_market_install("pkg"))
        self.assertIsNone(await self.db.get_subscription(subscription_id))
        self.assertEqual(await self.db.get_channels(subscription_id), [])


if __name__ == "__main__":
    unittest.main()
