import asyncio
import hashlib
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
                "epg_preference_evidence", "epg_read_resolver",
                "epg_source_management", "epg_source_model",
                "epg_source_preference", "epg_tasks", "iptv_channels",
                "market", "market_tasks",
            }
            or name == "security"
            or name.startswith("security.")
            or name.startswith("core")
        ):
            sys.modules.pop(name, None)


class EpgBindingManagementApiTest(unittest.IsolatedAsyncioTestCase):
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
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "binding-management-test"
        os.environ["WAVEFLOW_ANONYMOUS_BROWSE"] = "1"
        os.environ["WAVEFLOW_ANONYMOUS_PLAYBACK"] = "1"
        _clear_modules()

        self.db = importlib.import_module("database")
        self.bindings = importlib.import_module("epg_bindings")
        self.evidence = importlib.import_module("epg_preference_evidence")
        self.shadow = importlib.import_module("epg_match_shadow")
        self.source_management = importlib.import_module("epg_source_management")
        self.main = importlib.import_module("main")
        await self.db.initialize()
        self.main.app.state.automation_service = None
        self.now = datetime.now(timezone.utc)

        async def admin_override():
            return {"id": 1, "username": "admin", "role": "admin"}

        self.main.app.dependency_overrides[self.main.require_admin] = admin_override
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.main.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
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

    async def _source(
        self,
        name,
        channel_id,
        *,
        enabled=True,
        status="success",
        title=None,
    ):
        source_id = await self.db.add_epg_source(
            name,
            f"https://guide.example/{name}.xml",
        )
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE epg_sources
                SET enabled=?, last_status=?, revision=1,
                    channel_count=1, programme_count=1, updated_at=?
                WHERE id=?
                """,
                (int(enabled), status, self.now.isoformat(), source_id),
            )
            conn.execute(
                """
                INSERT INTO epg_channels(
                    source_id, channel_id, display_names, normalized_names
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    source_id,
                    channel_id,
                    json.dumps([channel_id]),
                    json.dumps([channel_id.casefold()]),
                ),
            )
            conn.execute(
                """
                INSERT INTO epg_programs(
                    source_id, channel_id, start, stop, title, description
                ) VALUES(?, ?, ?, ?, ?, '')
                """,
                (
                    source_id,
                    channel_id,
                    (self.now - timedelta(hours=1)).isoformat(),
                    (self.now + timedelta(hours=1)).isoformat(),
                    title or f"{name} programme",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return source_id

    def _logical(
        self,
        logical_id,
        *,
        canonical_key=None,
        tvg_id=None,
        display_name=None,
        status="active",
    ):
        key = canonical_key or f"key-{logical_id}"
        display = display_name or logical_id
        conn = self._connect()
        try:
            subscription_id = conn.execute(
                """
                INSERT INTO subscriptions(title, url, created_at)
                VALUES(?, ?, ?)
                """,
                (
                    f"Subscription {logical_id}",
                    f"https://playlist.example/{logical_id}.m3u",
                    self.now.isoformat(),
                ),
            ).lastrowid
            channel_id = conn.execute(
                """
                INSERT INTO channels(
                    subscription_id, name, url, tvg_id, tvg_name
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    subscription_id,
                    display,
                    f"https://stream.example/{logical_id}",
                    tvg_id or "",
                    display,
                ),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO iptv_logical_channels(
                    id, canonical_key, display_name, status,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    logical_id,
                    key,
                    display,
                    status,
                    self.now.isoformat(),
                    self.now.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO iptv_logical_channel_members(
                    logical_channel_id, channel_id, created_at, updated_at
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    logical_id,
                    channel_id,
                    self.now.isoformat(),
                    self.now.isoformat(),
                ),
            )
            conn.commit()
            return int(subscription_id)
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
        shadow_run_id=None,
    ):
        conn = self._connect()
        try:
            binding_id = conn.execute(
                """
                INSERT INTO iptv_logical_channel_epg_bindings(
                    logical_channel_id, epg_source_id, epg_channel_id,
                    status, match_type, confidence, locked, origin,
                    shadow_run_id, created_at, updated_at
                ) VALUES(?, ?, ?, 'matched', 'exact_tvg_id', 100, ?, ?, ?, ?, ?)
                """,
                (
                    logical_id,
                    source_id,
                    channel_id,
                    int(locked),
                    origin,
                    shadow_run_id,
                    self.now.isoformat(),
                    self.now.isoformat(),
                ),
            ).lastrowid
            conn.commit()
            return int(binding_id)
        finally:
            conn.close()

    def _binding_row(self, logical_id):
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM iptv_logical_channel_epg_bindings
                WHERE logical_channel_id=?
                """,
                (logical_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _policy(self, logical_id):
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM iptv_logical_channel_epg_policies
                WHERE logical_channel_id=?
                """,
                (logical_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def _bind(self, logical_id, source_id, channel_id):
        return await self.client.put(
            f"/api/admin/epg/logical-channels/{logical_id}/binding",
            json={
                "epg_source_id": source_id,
                "epg_channel_id": channel_id,
            },
        )

    async def test_manual_bind_replace_catalog_identity_and_read_are_immediate(self):
        source_a = await self._source("source-a", "shared")
        source_b = await self._source("source-b", "shared")
        self._logical("logical-manual")

        catalog = await self.client.get(
            f"/api/admin/epg/catalog?source_id={source_a}&q=shared"
        )
        identity = catalog.json()["items"][0]["identity"]
        created = await self._bind(
            "logical-manual",
            identity["epg_source_id"],
            identity["epg_channel_id"],
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["action"], "created")
        first = self._binding_row("logical-manual")
        self.assertEqual(first["epg_source_id"], source_a)
        self.assertEqual(first["origin"], "manual")
        self.assertEqual(first["match_type"], "manual")
        self.assertEqual(first["status"], "matched")
        self.assertEqual(first["locked"], 1)

        replaced = await self._bind("logical-manual", source_b, "shared")
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(replaced.json()["action"], "replaced")
        second = self._binding_row("logical-manual")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["epg_source_id"], source_b)
        self.assertEqual(second["origin"], "manual")
        self.assertEqual(second["locked"], 1)

        detail = await self.client.get(
            "/api/admin/epg/matching/logical-manual"
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["binding"]["epg_source_id"], source_b)
        self.assertEqual(detail.json()["binding"]["epg_channel_id"], "shared")

        programme = await self.client.get(
            "/api/iptv/epg/programs/key-logical-manual?tz=UTC"
        )
        self.assertEqual(programme.status_code, 200, programme.text)
        self.assertEqual(programme.json()["epg_source_id"], source_b)
        self.assertEqual(programme.json()["current"]["title"], "source-b programme")

    async def test_programme_api_keeps_the_earliest_overlapping_current(self):
        source = await self._source("overlap", "OVERLAP")
        self._logical("logical-overlap")
        self._binding("logical-overlap", source, "OVERLAP")
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO epg_programs(
                    source_id, channel_id, start, stop, title, description
                ) VALUES(?, ?, ?, ?, ?, '')
                """,
                (
                    source,
                    "OVERLAP",
                    (self.now - timedelta(minutes=30)).isoformat(),
                    (self.now + timedelta(hours=2)).isoformat(),
                    "Later overlapping programme",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        response = await self.main.get_epg_programs(
            "key-logical-overlap",
            tz="UTC",
        )
        self.assertEqual(response["current"]["title"], "overlap programme")

    async def test_batch_current_does_not_repeat_programme_at_its_start_as_next(self):
        source = await self._source("boundary", "BOUNDARY")
        self._logical("logical-boundary")
        self._binding("logical-boundary", source, "BOUNDARY")
        fixed_now = self.now.replace(microsecond=0)
        programmes = [
            (
                source,
                "BOUNDARY",
                (fixed_now - timedelta(hours=1)).isoformat(),
                fixed_now.isoformat(),
                "Previous programme",
            ),
            (
                source,
                "BOUNDARY",
                fixed_now.isoformat(),
                (fixed_now + timedelta(hours=1)).isoformat(),
                "Programme at boundary",
            ),
            (
                source,
                "BOUNDARY",
                (fixed_now + timedelta(hours=1)).isoformat(),
                (fixed_now + timedelta(hours=2)).isoformat(),
                "Next programme",
            ),
        ]
        conn = self._connect()
        try:
            conn.execute("DELETE FROM epg_programs WHERE source_id=?", (source,))
            conn.executemany(
                """
                INSERT INTO epg_programs(
                    source_id, channel_id, start, stop, title, description
                ) VALUES(?, ?, ?, ?, ?, '')
                """,
                programmes,
            )
            conn.commit()
        finally:
            conn.close()

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is None else fixed_now.astimezone(tz)

        with mock.patch.object(self.db, "datetime", FixedDateTime):
            result = await self.db.batch_get_current_programs(["key-logical-boundary"])

        self.assertEqual(
            result["key-logical-boundary"]["current"]["title"],
            "Programme at boundary",
        )
        self.assertEqual(
            result["key-logical-boundary"]["next"]["title"],
            "Next programme",
        )

    async def test_replace_all_origins_and_invalid_target_roll_back(self):
        source_a = await self._source("origin-a", "A")
        source_b = await self._source("origin-b", "B")
        for origin in ("automatic", "legacy_migrated", "manual"):
            logical_id = f"logical-{origin}"
            self._logical(logical_id)
            old_id = self._binding(
                logical_id,
                source_a,
                "A",
                origin=origin,
                locked=origin == "manual",
            )
            response = await self._bind(logical_id, source_b, "B")
            self.assertEqual(response.status_code, 200, response.text)
            row = self._binding_row(logical_id)
            self.assertEqual(row["id"], old_id)
            self.assertEqual(
                (row["epg_source_id"], row["epg_channel_id"]),
                (source_b, "B"),
            )
            self.assertEqual((row["origin"], row["locked"]), ("manual", 1))

        before = self._binding_row("logical-manual")
        invalid = await self._bind("logical-manual", source_a, "B")
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(
            invalid.json()["detail"]["code"],
            "invalid_composite_target",
        )
        self.assertEqual(self._binding_row("logical-manual"), before)

    async def test_replace_database_failure_rolls_back_and_hides_sqlite_error(self):
        source_a = await self._source("rollback-a", "A")
        source_b = await self._source("rollback-b", "B")
        self._logical("logical-rollback")
        self._binding(
            "logical-rollback",
            source_a,
            "A",
            origin="manual",
            locked=True,
        )
        before = self._binding_row("logical-rollback")
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TRIGGER reject_binding_replace
                BEFORE UPDATE ON iptv_logical_channel_epg_bindings
                BEGIN
                    SELECT RAISE(ABORT, 'secret fixture database failure');
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

        response = await self._bind("logical-rollback", source_b, "B")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "binding_write_failed",
        )
        self.assertNotIn("secret fixture", response.text)
        self.assertEqual(self._binding_row("logical-rollback"), before)

    async def test_manual_binding_rejects_missing_conflict_and_inactive_logical(self):
        source = await self._source("validation", "VALID")
        missing = await self._bind("missing", source, "VALID")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            missing.json()["detail"]["code"],
            "logical_channel_not_found",
        )

        for logical_id, status, code in (
            ("split", "split_conflict", "logical_channel_conflict"),
            ("merge", "merge_conflict", "logical_channel_conflict"),
            ("orphan", "orphaned", "logical_channel_inactive"),
        ):
            self._logical(logical_id, status=status)
            response = await self._bind(logical_id, source, "VALID")
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["detail"]["code"], code)
            self.assertIsNone(self._binding_row(logical_id))

    async def test_lock_unlock_retains_target_and_restore_is_separate(self):
        source = await self._source("lock", "LOCK")
        self._logical("logical-lock", tvg_id="LOCK")
        binding_id = self._binding(
            "logical-lock",
            source,
            "LOCK",
            origin="automatic",
            shadow_run_id="historical-run",
        )

        locked = await self.client.put(
            "/api/admin/epg/logical-channels/logical-lock/binding/lock"
        )
        self.assertEqual(locked.status_code, 200, locked.text)
        unlocked = await self.client.delete(
            "/api/admin/epg/logical-channels/logical-lock/binding/lock"
        )
        self.assertEqual(unlocked.status_code, 200, unlocked.text)
        self.assertTrue(unlocked.json()["restore_automatic_required"])
        row = self._binding_row("logical-lock")
        self.assertEqual(row["id"], binding_id)
        self.assertEqual((row["epg_source_id"], row["epg_channel_id"]), (source, "LOCK"))
        self.assertEqual((row["origin"], row["locked"]), ("automatic", 0))

        run = await self.shadow.run_epg_match_shadow()
        decision = await self.shadow.get_shadow_decision(
            run["run_id"],
            "logical-lock",
        )
        self.assertEqual(decision.status, "existing_preserved")

    async def test_restore_automatic_rematches_and_suppresses_legacy_remigration(self):
        auto_source = await self._source("auto", "AUTO")
        manual_source = await self._source("manual", "MANUAL")
        self._logical(
            "logical-restore",
            tvg_id="AUTO",
            display_name="Unrelated display",
        )
        self._binding(
            "logical-restore",
            manual_source,
            "MANUAL",
            origin="manual",
            locked=True,
        )

        response = await self.client.post(
            "/api/admin/epg/logical-channels/logical-restore/restore-automatic"
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["mode"], "automatic")
        self.assertEqual(result["maintenance"]["status"], "success")
        row = self._binding_row("logical-restore")
        self.assertEqual(
            (row["epg_source_id"], row["epg_channel_id"]),
            (auto_source, "AUTO"),
        )
        self.assertEqual((row["origin"], row["locked"]), ("automatic", 0))
        self.assertTrue(row["shadow_run_id"])
        self.assertEqual(self._policy("logical-restore")["mode"], "automatic")

        programme = await self.client.get(
            "/api/iptv/epg/programs/key-logical-restore?tz=UTC"
        )
        self.assertEqual(programme.json()["epg_source_id"], auto_source)
        self.assertEqual(programme.json()["current"]["title"], "auto programme")

    async def test_restore_automatic_can_remain_safely_unmatched(self):
        legacy_source = await self._source("legacy", "LEGACY")
        self._logical(
            "logical-unmatched",
            tvg_id="NO-CANDIDATE",
            display_name="No matching display",
        )
        self._binding(
            "logical-unmatched",
            legacy_source,
            "LEGACY",
            origin="manual",
            locked=True,
        )

        response = await self.client.post(
            "/api/admin/epg/logical-channels/logical-unmatched/restore-automatic"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self._binding_row("logical-unmatched"))
        self.assertEqual(self._policy("logical-unmatched")["mode"], "automatic")
        programme = await self.client.get(
            "/api/iptv/epg/programs/key-logical-unmatched?tz=UTC"
        )
        self.assertEqual(programme.status_code, 200, programme.text)
        self.assertNotIn("epg_source_id", programme.json())
        self.assertEqual(programme.json()["programs"], [])

    async def test_restore_automatic_commits_state_when_maintenance_degrades(self):
        source = await self._source("degraded", "DEGRADED")
        self._logical("logical-degraded")
        self._binding(
            "logical-degraded",
            source,
            "DEGRADED",
            origin="manual",
            locked=True,
        )
        with mock.patch.object(
            self.main.epg_binding_management.epg_maintenance,
            "run_epg_binding_maintenance",
            side_effect=RuntimeError("fixture maintenance failure"),
        ):
            response = await self.client.post(
                "/api/admin/epg/logical-channels/logical-degraded/restore-automatic"
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["warning"]["code"],
            "maintenance_degraded",
        )
        self.assertNotIn("fixture maintenance failure", response.text)
        self.assertIsNone(self._binding_row("logical-degraded"))
        self.assertEqual(self._policy("logical-degraded")["mode"], "automatic")

    async def test_explicit_no_epg_blocks_read_match_and_stale_apply_then_restores(self):
        source = await self._source("no-epg", "NOEPG")
        self._logical("logical-no-epg", tvg_id="NOEPG")
        stale_run = await self.shadow.run_epg_match_shadow()
        self.assertEqual(stale_run["matched_count"], 1)

        disabled = await self.client.put(
            "/api/admin/epg/logical-channels/logical-no-epg/no-epg"
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(self._policy("logical-no-epg")["mode"], "no_epg")
        self.assertIsNone(self._binding_row("logical-no-epg"))

        apply = await self.shadow.apply_epg_match_shadow_run(stale_run["run_id"])
        self.assertEqual(apply["applied_count"], 0)
        self.assertEqual(apply["revalidation_mismatch_count"], 1)
        self.assertIsNone(self._binding_row("logical-no-epg"))
        current_run = await self.shadow.run_epg_match_shadow()
        self.assertEqual(current_run["not_applicable_count"], 1)

        detail = await self.client.get(
            "/api/admin/epg/matching/logical-no-epg"
        )
        self.assertEqual(detail.json()["diagnostic"]["status"], "not_applicable")
        self.assertEqual(detail.json()["policy"]["mode"], "no_epg")
        programme = await self.client.get(
            "/api/iptv/epg/programs/key-logical-no-epg?tz=UTC"
        )
        self.assertNotIn("epg_source_id", programme.json())
        batch = await self.client.post(
            "/api/iptv/epg/batch-current",
            json={"canonical_keys": ["key-logical-no-epg"]},
        )
        self.assertIsNone(batch.json()["key-logical-no-epg"])

        restored = await self.client.post(
            "/api/admin/epg/logical-channels/logical-no-epg/restore-automatic"
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        row = self._binding_row("logical-no-epg")
        self.assertEqual((row["origin"], row["epg_channel_id"]), ("automatic", "NOEPG"))

    async def test_manual_bind_repairs_orphan_and_clears_no_epg_policy(self):
        source_a = await self._source("orphan-a", "A")
        source_b = await self._source("orphan-b", "B")
        self._logical("logical-orphan-repair")
        self._binding(
            "logical-orphan-repair",
            source_a,
            "A",
            origin="manual",
            locked=True,
        )
        deleted, _ = await self.source_management.delete_custom_epg_source(
            source_a,
            confirm=True,
        )
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(
            self._binding_row("logical-orphan-repair")["status"],
            "orphan_target",
        )
        orphan_detail = await self.client.get(
            "/api/admin/epg/matching/logical-orphan-repair"
        )
        self.assertEqual(
            orphan_detail.json()["diagnostic"]["status"],
            "missing_target",
        )
        self.assertEqual(
            orphan_detail.json()["binding"]["epg_source_id"],
            source_a,
        )

        repaired = await self._bind("logical-orphan-repair", source_b, "B")
        self.assertEqual(repaired.status_code, 200, repaired.text)
        row = self._binding_row("logical-orphan-repair")
        self.assertEqual(
            (row["status"], row["epg_source_id"], row["epg_channel_id"]),
            ("matched", source_b, "B"),
        )
        detail = await self.client.get(
            "/api/admin/epg/matching/logical-orphan-repair"
        )
        self.assertEqual(detail.json()["diagnostic"]["status"], "healthy_bound")

        no_epg = await self.client.put(
            "/api/admin/epg/logical-channels/logical-orphan-repair/no-epg"
        )
        self.assertEqual(no_epg.status_code, 200)
        rebound = await self._bind("logical-orphan-repair", source_b, "B")
        self.assertEqual(rebound.status_code, 200)
        self.assertIsNone(self._policy("logical-orphan-repair"))

    async def test_stale_shadow_apply_cannot_overwrite_manual_binding(self):
        automatic = await self._source("stale-auto", "AUTO")
        manual = await self._source("stale-manual", "MANUAL")
        self._logical("logical-stale", tvg_id="AUTO")
        run = await self.shadow.run_epg_match_shadow()
        self.assertEqual(run["matched_count"], 1)
        bound = await self._bind("logical-stale", manual, "MANUAL")
        self.assertEqual(bound.status_code, 200)

        applied = await self.shadow.apply_epg_match_shadow_run(run["run_id"])
        self.assertEqual(applied["applied_count"], 0)
        self.assertEqual(applied["locked_conflict_count"], 1)
        row = self._binding_row("logical-stale")
        self.assertEqual(
            (row["epg_source_id"], row["epg_channel_id"], row["locked"]),
            (manual, "MANUAL", 1),
        )
        self.assertNotEqual(automatic, manual)

    async def test_subscription_manual_preference_set_clear_preserves_derived(self):
        derived_source = await self._source("derived", "DERIVED")
        manual_source = await self._source(
            "manual-pref",
            "MANUAL",
            enabled=False,
            status="failed",
        )
        replacement_source = await self._source(
            "replacement-pref",
            "REPLACEMENT",
        )
        subscription_id = self._logical("logical-preference")
        derived_url = (await self.db.get_epg_source(derived_source))["url"]
        await self.evidence.replace_subscription_url_tvg_evidence(
            subscription_id,
            (("url-tvg", derived_url),),
        )
        before_binding = self._binding_row("logical-preference")

        set_response = await self.client.put(
            f"/api/admin/epg/subscriptions/{subscription_id}/source-preference",
            json={"epg_source_id": manual_source},
        )
        self.assertEqual(set_response.status_code, 200, set_response.text)
        self.assertEqual(set_response.json()["resolution"]["status"], "stale_preference")
        self.assertFalse(set_response.json()["binding_changed"])
        self.assertEqual(self._binding_row("logical-preference"), before_binding)

        replaced = await self.client.put(
            f"/api/admin/epg/subscriptions/{subscription_id}/source-preference",
            json={"epg_source_id": replacement_source},
        )
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(replaced.json()["replaced_manual_count"], 1)
        self.assertEqual(replaced.json()["resolution"]["status"], "preferred")
        self.assertEqual(
            replaced.json()["resolution"]["preferred_source_id"],
            replacement_source,
        )

        conn = self._connect()
        try:
            origins = [
                row["origin"]
                for row in conn.execute(
                    """
                    SELECT origin FROM epg_source_preference_evidence
                    WHERE subscription_id=? ORDER BY origin
                    """,
                    (subscription_id,),
                ).fetchall()
            ]
        finally:
            conn.close()
        self.assertEqual(origins, ["manual", "url_tvg"])

        clear = await self.client.delete(
            f"/api/admin/epg/subscriptions/{subscription_id}/source-preference"
        )
        self.assertEqual(clear.status_code, 200, clear.text)
        self.assertEqual(clear.json()["cleared_manual_count"], 1)
        self.assertEqual(clear.json()["preserved_derived_count"], 1)
        self.assertEqual(clear.json()["resolution"]["status"], "preferred")
        self.assertEqual(
            clear.json()["resolution"]["preferred_source_id"],
            derived_source,
        )

        missing = await self.client.put(
            f"/api/admin/epg/subscriptions/{subscription_id}/source-preference",
            json={"epg_source_id": 999999},
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "epg_source_not_found")

    async def test_source_delete_and_manual_bind_serialize_without_dangling_target(self):
        source = await self._source("concurrent", "CONCURRENT")
        self._logical("logical-concurrent")

        async def delete_source():
            try:
                return await self.source_management.delete_custom_epg_source(
                    source,
                    confirm=False,
                )
            except self.source_management.EpgSourceManagementError as error:
                return error.code

        async def bind_source():
            response = await self._bind(
                "logical-concurrent",
                source,
                "CONCURRENT",
            )
            return response.status_code, response.json()

        await asyncio.gather(delete_source(), bind_source())
        source_row = await self.db.get_epg_source(source)
        binding = self._binding_row("logical-concurrent")
        if source_row is None:
            self.assertIsNone(binding)
        else:
            self.assertIsNotNone(binding)
            self.assertEqual(binding["epg_source_id"], source)
            conn = self._connect()
            try:
                self.assertIsNotNone(conn.execute(
                    """
                    SELECT 1 FROM epg_channels
                    WHERE source_id=? AND channel_id='CONCURRENT'
                    """,
                    (source,),
                ).fetchone())
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
