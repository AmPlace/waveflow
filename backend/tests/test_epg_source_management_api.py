import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import httpx


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
            }
            or name == "security"
            or name.startswith("security.")
            or name.startswith("core")
        ):
            sys.modules.pop(name, None)


class EpgSourceManagementApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "WAVEFLOW_DB_PATH",
                "WAVEFLOW_PROXY_HANDLE_SECRET",
                "WAVEFLOW_ANONYMOUS_BROWSE",
                "WAVEFLOW_ANONYMOUS_PLAYBACK",
            )
        }
        self.db_path = os.path.join(self.tmpdir.name, "waveflow.db")
        os.environ["WAVEFLOW_DB_PATH"] = self.db_path
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "epg-source-management-test-secret"
        os.environ["WAVEFLOW_ANONYMOUS_BROWSE"] = "1"
        os.environ["WAVEFLOW_ANONYMOUS_PLAYBACK"] = "1"
        _clear_modules()

        self.db = importlib.import_module("database")
        self.automation = importlib.import_module("automation")
        self.epg = importlib.import_module("epg")
        self.evidence = importlib.import_module("epg_preference_evidence")
        self.source_model = importlib.import_module("epg_source_model")
        self.epg_tasks = importlib.import_module("epg_tasks")
        self.main = importlib.import_module("main")
        await self.db.initialize()
        self.main.app.state.automation_service = None
        self.services = []
        self.now = datetime.now(timezone.utc).isoformat()

        async def admin_override():
            return {"id": 1, "username": "admin", "role": "admin"}

        self.main.app.dependency_overrides[self.main.require_admin] = admin_override
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.main.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        for service in reversed(self.services):
            await service.stop(timeout_seconds=0.2)
        await self.client.aclose()
        self.main.app.dependency_overrides.clear()
        self.main.app.state.automation_service = None
        for client in (self.main.http_client,):
            if not client.is_closed:
                await client.aclose()
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _clear_modules()
        self.tmpdir.cleanup()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

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
        self.main.app.state.automation_service = service
        return service

    async def _create_custom(self, *, name="Custom", url=None, enabled=True):
        default_slug = "-".join(name.lower().split())
        response = await self.client.post(
            "/api/admin/epg/sources",
            json={
                "name": name,
                "url": url or f"https://guide.example/{default_slug}.xml",
                "enabled": enabled,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _logical(self, logical_id, *, status="active", display_name=None):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO iptv_logical_channels(
                    id, canonical_key, display_name, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    logical_id,
                    f"key-{logical_id}",
                    display_name or logical_id,
                    status,
                    self.now,
                    self.now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _binding(
        self,
        logical_id,
        source_id,
        channel_id,
        *,
        origin="automatic",
        locked=False,
    ):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO iptv_logical_channel_epg_bindings(
                    logical_channel_id, epg_source_id, epg_channel_id, status,
                    match_type, confidence, locked, origin, created_at, updated_at
                ) VALUES(?, ?, ?, 'matched', 'exact_tvg_id', 100, ?, ?, ?, ?)
                """,
                (
                    logical_id,
                    source_id,
                    channel_id,
                    int(locked),
                    origin,
                    self.now,
                    self.now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _dataset(self, source_id, channel_id="shared"):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO epg_channels(
                    source_id, channel_id, display_names, normalized_names
                ) VALUES(?, ?, '["Shared"]', '["shared"]')
                """,
                (source_id, channel_id),
            )
            conn.execute(
                """
                INSERT INTO epg_programs(
                    source_id, channel_id, start, stop, title, description
                ) VALUES(?, ?, ?, ?, 'Programme', '')
                """,
                (
                    source_id,
                    channel_id,
                    "2026-08-09T00:00:00+00:00",
                    "2026-08-09T01:00:00+00:00",
                ),
            )
            conn.execute(
                """
                UPDATE epg_sources
                SET last_status='success', channel_count=1, programme_count=1
                WHERE id=?
                """,
                (source_id,),
            )
            conn.commit()
        finally:
            conn.close()

    async def test_clean_bootstrap_creates_one_stable_builtin_preset(self):
        first = await self.epg.ensure_default_epg_sources()
        second = await self.epg.ensure_default_epg_sources()

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        source = second[0]
        self.assertEqual(source["id"], first[0]["id"])
        self.assertEqual(source["source_origin"], "builtin")
        self.assertEqual(source["builtin_key"], self.source_model.BUILTIN_CHINA_EPG_KEY)
        self.assertEqual(source["name"], "51zmt")
        self.assertEqual(source["url"], "http://epg.51zmt.top:8000/e.xml.gz")

        rejected = await self.client.post(
            "/api/admin/epg/sources",
            json={
                "name": "Spoof",
                "url": "https://guide.example/spoof.xml",
                "source_origin": "builtin",
            },
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json()["detail"]["code"], "invalid_request")

    async def test_exact_pre_5b_default_row_gets_one_time_persisted_identity(self):
        source_id = await self.db.add_epg_source(
            "51zmt",
            "http://epg.51zmt.top:8000/e.xml.gz",
        )

        await self.db.initialize()
        migrated = await self.db.get_epg_source(source_id)
        self.assertEqual(migrated["source_origin"], "builtin")
        self.assertEqual(migrated["builtin_key"], self.source_model.BUILTIN_CHINA_EPG_KEY)
        ensured = await self.epg.ensure_default_epg_sources()
        self.assertEqual(len(ensured), 1)
        self.assertEqual(ensured[0]["id"], source_id)

    async def test_builtin_enable_disable_and_managed_boundaries(self):
        builtin = (await self.epg.ensure_default_epg_sources())[0]
        service = self._service()
        await self.epg_tasks.reconcile_epg_automation_tasks(service, self.main.http_client)
        task_id = self.epg_tasks.epg_task_id(builtin["id"])

        disabled = await self.client.patch(
            f"/api/admin/epg/sources/{builtin['id']}",
            json={"name": "中国节目单", "enabled": False},
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(disabled.json()["source"]["name"], "中国节目单")
        self.assertFalse(disabled.json()["source"]["enabled"])
        self.assertFalse((await service.repository.get_config(task_id)).enabled)

        secret = "managed-url-secret"
        managed_url = await self.client.patch(
            f"/api/admin/epg/sources/{builtin['id']}",
            json={"url": f"https://guide.example/builtin.xml?token={secret}"},
        )
        self.assertEqual(managed_url.status_code, 409)
        self.assertEqual(managed_url.json()["detail"]["code"], "builtin_url_managed")
        self.assertNotIn(secret, managed_url.text)

        forbidden = await self.client.delete(
            f"/api/admin/epg/sources/{builtin['id']}",
        )
        self.assertEqual(forbidden.status_code, 409)
        self.assertEqual(forbidden.json()["detail"]["code"], "builtin_delete_forbidden")

        enabled = await self.client.patch(
            f"/api/admin/epg/sources/{builtin['id']}",
            json={"enabled": True},
        )
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue((await service.repository.get_config(task_id)).enabled)
        persisted = await self.db.get_epg_source(builtin["id"])
        self.assertEqual(persisted["name"], "中国节目单")
        self.assertEqual(persisted["url"], self.source_model.BUILTIN_CHINA_EPG_PRESET.url)

    async def test_custom_overseas_secret_safe_edit_and_task_lifecycle(self):
        service = self._service()
        original_url = "https://xmltv.example.jp/tokyo.xml?token=private-one&region=jp"
        created = await self._create_custom(name="Tokyo XMLTV", url=original_url)
        source_id = created["source"]["id"]
        task_id = self.epg_tasks.epg_task_id(source_id)
        self.assertEqual(created["source"]["source_origin"], "custom")
        self.assertEqual(created["source"]["builtin_key"], "")
        self.assertNotIn("private-one", json.dumps(created, ensure_ascii=False))
        self.assertEqual((await self.db.get_epg_source(source_id))["url"], original_url)
        self.assertTrue((await service.repository.get_config(task_id)).enabled)
        listed = await self.client.get("/api/admin/epg/sources")
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn("private-one", listed.text)

        renamed = await self.client.patch(
            f"/api/admin/epg/sources/{source_id}",
            json={"name": "Japan Guide", "enabled": False},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual((await self.db.get_epg_source(source_id))["url"], original_url)
        self.assertFalse((await service.repository.get_config(task_id)).enabled)

        before_revision = (await self.db.get_epg_source(source_id))["revision"]
        replacement = "https://self-hosted.example.net/xmltv?signature=private-two&feed=world"
        replaced = await self.client.patch(
            f"/api/admin/epg/sources/{source_id}",
            json={"url": replacement, "enabled": True},
        )
        self.assertEqual(replaced.status_code, 200, replaced.text)
        after = await self.db.get_epg_source(source_id)
        self.assertEqual(after["id"], source_id)
        self.assertEqual(after["url"], replacement)
        self.assertEqual(after["revision"], before_revision + 1)
        self.assertNotIn("private-two", replaced.text)
        self.assertTrue((await service.repository.get_config(task_id)).enabled)

    async def test_validation_and_duplicate_errors_do_not_echo_secret_urls(self):
        secret = "should-not-appear"
        invalid = await self.client.post(
            "/api/admin/epg/sources",
            json={"name": "Invalid", "url": f"ftp://guide.example/a?token={secret}"},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["detail"]["code"], "invalid_url")
        self.assertNotIn(secret, invalid.text)

        url = f"https://guide.example/a.xml?token={secret}&feed=one"
        await self._create_custom(name="First", url=url)
        duplicate = await self.client.post(
            "/api/admin/epg/sources",
            json={"name": "Second", "url": url},
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"]["code"], "source_url_exists")
        self.assertNotIn(secret, duplicate.text)

    async def test_single_source_refresh_reuses_automation_runner_for_builtin_and_custom(self):
        builtin = (await self.epg.ensure_default_epg_sources())[0]
        custom = (await self._create_custom(name="Custom Refresh"))["source"]
        service = self._service()
        expected = self.automation.AutomationRunResult(
            task_id=self.epg_tasks.epg_task_id(custom["id"]),
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
            self.main,
            "run_epg_source_refresh_now",
            new=mock.AsyncMock(return_value=expected),
        ) as refresh:
            for source_id in (builtin["id"], custom["id"]):
                response = await self.client.post(
                    f"/api/admin/epg/sources/{source_id}/refresh"
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "success")
                self.assertNotIn("error", response.json())
        self.assertEqual(refresh.await_count, 2)
        self.assertEqual(
            [call.kwargs["source_id"] for call in refresh.await_args_list],
            [builtin["id"], custom["id"]],
        )
        self.assertTrue(all(call.args[0] is service for call in refresh.await_args_list))

        await self.db.update_epg_source(custom["id"], enabled=False)
        disabled = await self.client.post(
            f"/api/admin/epg/sources/{custom['id']}/refresh"
        )
        self.assertEqual(disabled.status_code, 409)
        self.assertEqual(disabled.json()["detail"]["code"], "source_disabled")

    async def test_refresh_busy_and_failure_are_productized_without_raw_error(self):
        source = (await self._create_custom(name="Refresh Error"))["source"]
        self._service()
        busy = self.automation.AutomationBusy(
            task_id=self.epg_tasks.epg_task_id(source["id"]),
            task_type="refresh",
            conflict_group=self.epg_tasks.epg_conflict_group(source["id"]),
            started_at=self.now,
            status="running",
        )
        with mock.patch.object(
            self.main,
            "run_epg_source_refresh_now",
            new=mock.AsyncMock(return_value=busy),
        ):
            response = await self.client.post(
                f"/api/admin/epg/sources/{source['id']}/refresh"
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "source_refresh_busy")

        failure = self.automation.AutomationRunResult(
            task_id=busy.task_id,
            task_type="refresh",
            status="failed",
            checked_count=1,
            updated_count=0,
            skipped_count=0,
            failed_count=1,
            error="upstream token=raw-secret",
            started_at=self.now,
            finished_at=self.now,
        )
        with mock.patch.object(
            self.main,
            "run_epg_source_refresh_now",
            new=mock.AsyncMock(return_value=failure),
        ):
            response = await self.client.post(
                f"/api/admin/epg/sources/{source['id']}/refresh"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["failure_category"], "refresh_failed")
        self.assertNotIn("raw-secret", response.text)

    async def test_delete_preview_and_custom_delete_clean_automatic_state(self):
        service = self._service()
        source = (await self._create_custom(name="Delete Me"))["source"]
        other = (await self._create_custom(name="Other Source"))["source"]
        self._dataset(source["id"], "shared")
        self._dataset(other["id"], "shared")
        self._logical("logical-auto")
        self._binding("logical-auto", source["id"], "shared")
        task_id = self.epg_tasks.epg_task_id(source["id"])
        self.assertIsNotNone(await service.repository.get_config(task_id))

        preview = await self.client.get(
            f"/api/admin/epg/sources/{source['id']}/delete-impact"
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        impact = preview.json()
        self.assertEqual(impact["active_bindings_count"], 1)
        self.assertEqual(impact["automatic_bindings_count"], 1)
        self.assertEqual(impact["dataset"], {"channel_count": 1, "programme_count": 1})
        self.assertFalse(impact["requires_confirmation"])

        deleted = await self.client.delete(
            f"/api/admin/epg/sources/{source['id']}"
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted_bindings_count"], 1)
        self.assertIsNone(await self.db.get_epg_source(source["id"]))
        self.assertIsNone(await service.repository.get_config(task_id))
        conn = self._connect()
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM epg_channels WHERE source_id=?",
                    (source["id"],),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM epg_programs WHERE source_id=?",
                    (source["id"],),
                ).fetchone()[0],
                0,
            )
            remaining = conn.execute(
                "SELECT source_id, channel_id FROM epg_channels"
            ).fetchall()
            self.assertEqual([tuple(row) for row in remaining], [(other["id"], "shared")])
        finally:
            conn.close()

    async def test_protected_bindings_require_confirmation_and_remain_auditable(self):
        source = (await self._create_custom(name="Protected"))["source"]
        self._dataset(source["id"], "protected")
        for logical_id in ("logical-manual", "logical-locked", "logical-auto"):
            self._logical(logical_id)
        self._binding(
            "logical-manual", source["id"], "protected",
            origin="manual", locked=False,
        )
        self._binding(
            "logical-locked", source["id"], "protected",
            origin="automatic", locked=True,
        )
        self._binding(
            "logical-auto", source["id"], "protected",
            origin="automatic", locked=False,
        )
        await self.evidence.set_manual_source_preference(
            epg_source_id=source["id"],
            logical_channel_id="logical-manual",
        )

        rejected = await self.client.delete(
            f"/api/admin/epg/sources/{source['id']}"
        )
        self.assertEqual(rejected.status_code, 409)
        detail = rejected.json()["detail"]
        self.assertEqual(detail["code"], "delete_confirmation_required")
        self.assertEqual(detail["impact"]["manual_bindings_count"], 1)
        self.assertEqual(detail["impact"]["locked_bindings_count"], 1)
        self.assertIsNotNone(await self.db.get_epg_source(source["id"]))

        deleted = await self.client.delete(
            f"/api/admin/epg/sources/{source['id']}?confirm=true"
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted_bindings_count"], 1)
        self.assertEqual(deleted.json()["orphaned_protected_bindings_count"], 2)
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT logical_channel_id, status, origin, locked
                FROM iptv_logical_channel_epg_bindings ORDER BY logical_channel_id
                """
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("logical-locked", "orphan_target", "automatic", 1),
                    ("logical-manual", "orphan_target", "manual", 0),
                ],
            )
            evidence = conn.execute(
                """
                SELECT epg_source_id, origin FROM epg_source_preference_evidence
                WHERE logical_channel_id='logical-manual'
                """
            ).fetchone()
            self.assertEqual(tuple(evidence), (source["id"], "manual"))
        finally:
            conn.close()
        snapshot = await self.evidence.build_epg_source_preference_snapshot()
        manual = next(
            item for item in snapshot
            if item.logical_channel_id == "logical-manual"
        )
        self.assertEqual(manual.resolution.status, "missing_source")

    async def test_url_evidence_becomes_unresolved_then_rebinds_by_exact_fingerprint(self):
        secret_url = "https://guide.example/xmltv?token=secret-one&feed=china"
        source = (await self._create_custom(name="Derived", url=secret_url))["source"]
        subscription_id = await self.db.add_subscription(
            "Subscription",
            "https://playlist.example/list.m3u",
        )
        await self.evidence.replace_subscription_url_tvg_evidence(
            subscription_id,
            (("url-tvg", secret_url),),
        )

        deleted = await self.client.delete(
            f"/api/admin/epg/sources/{source['id']}"
        )
        self.assertEqual(deleted.status_code, 200)
        conn = self._connect()
        try:
            unresolved = conn.execute(
                """
                SELECT epg_source_id, resolution_status, hint_summary
                FROM epg_source_preference_evidence WHERE subscription_id=?
                """,
                (subscription_id,),
            ).fetchone()
            self.assertEqual(unresolved["resolution_status"], "unresolved")
            self.assertIsNone(unresolved["epg_source_id"])
            self.assertNotIn("secret-one", unresolved["hint_summary"])
        finally:
            conn.close()

        different = await self._create_custom(
            name="Different Query",
            url="https://guide.example/xmltv?token=secret-two&feed=china",
        )
        conn = self._connect()
        try:
            self.assertIsNone(conn.execute(
                "SELECT epg_source_id FROM epg_source_preference_evidence WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()[0])
        finally:
            conn.close()
        restored = await self._create_custom(name="Restored", url=secret_url)
        conn = self._connect()
        try:
            resolved = conn.execute(
                """
                SELECT epg_source_id, resolution_status, hint_summary, evidence_json
                FROM epg_source_preference_evidence WHERE subscription_id=?
                """,
                (subscription_id,),
            ).fetchone()
            self.assertEqual(resolved["resolution_status"], "resolved")
            self.assertEqual(resolved["epg_source_id"], restored["source"]["id"])
            self.assertNotEqual(resolved["epg_source_id"], different["source"]["id"])
            self.assertNotIn("secret-one", repr(tuple(resolved)))
        finally:
            conn.close()
        self.assertNotIn("secret-one", json.dumps(restored, ensure_ascii=False))

    async def test_delete_transaction_failure_rolls_back_source_dataset_and_binding(self):
        source = (await self._create_custom(name="Rollback"))["source"]
        self._dataset(source["id"], "rollback")
        self._logical("logical-rollback")
        self._binding("logical-rollback", source["id"], "rollback")
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TRIGGER reject_source_delete
                BEFORE DELETE ON epg_sources
                BEGIN
                    SELECT RAISE(ABORT, 'fixture delete failure');
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

        response = await self.client.delete(
            f"/api/admin/epg/sources/{source['id']}"
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["code"], "source_delete_failed")
        self.assertNotIn("fixture delete failure", response.text)
        self.assertIsNotNone(await self.db.get_epg_source(source["id"]))
        conn = self._connect()
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM epg_channels WHERE source_id=?",
                    (source["id"],),
                ).fetchone()[0],
                1,
            )
            binding = conn.execute(
                """
                SELECT status FROM iptv_logical_channel_epg_bindings
                WHERE logical_channel_id='logical-rollback'
                """
            ).fetchone()
            self.assertEqual(binding["status"], "matched")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
