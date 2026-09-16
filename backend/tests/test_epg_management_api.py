import hashlib
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
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
                "epg_tasks", "epg_preference_evidence", "epg_source_preference",
                "epg_source_management", "epg_source_model",
            }
            or name == "security"
            or name.startswith("security.")
            or name.startswith("core")
        ):
            sys.modules.pop(name, None)


class EpgManagementApiTest(unittest.IsolatedAsyncioTestCase):
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
        os.environ["WAVEFLOW_PROXY_HANDLE_SECRET"] = "epg-management-test-secret"
        os.environ["WAVEFLOW_ANONYMOUS_BROWSE"] = "1"
        os.environ["WAVEFLOW_ANONYMOUS_PLAYBACK"] = "1"
        _clear_modules()
        self.db = importlib.import_module("database")
        self.automation = importlib.import_module("automation")
        self.epg = importlib.import_module("epg")
        self.epg_tasks = importlib.import_module("epg_tasks")
        self.management = importlib.import_module("epg_management")
        self.main = importlib.import_module("main")
        await self.db.initialize()
        self.main.app.state.automation_service = None

        async def admin_override():
            return {"id": 1, "username": "admin", "role": "admin"}

        self.main.app.dependency_overrides[self.main.require_admin] = admin_override
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.main.app),
            base_url="http://testserver",
        )
        self.now = datetime.now(timezone.utc).isoformat()

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
        *,
        status="success",
        enabled=True,
        url=None,
        channel_id=None,
        channel_name=None,
        last_error="",
    ):
        source_id = await self.db.add_epg_source(
            name,
            url or f"https://guide.example/{name}.xml",
        )
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE epg_sources
                SET enabled=?, last_status=?, last_attempt_at=?, last_success_at=?,
                    last_error=?, channel_count=?, programme_count=?,
                    data_start_at=?, data_end_at=?
                WHERE id=?
                """,
                (
                    int(enabled), status, self.now,
                    self.now if status == "success" else "",
                    last_error, 1 if channel_id else 0, 2 if channel_id else 0,
                    "2026-08-09T00:00:00+00:00" if channel_id else "",
                    "2026-08-10T00:00:00+00:00" if channel_id else "",
                    source_id,
                ),
            )
            if channel_id:
                conn.execute(
                    """
                    INSERT INTO epg_channels(source_id, channel_id, display_names, normalized_names)
                    VALUES(?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        channel_id,
                        json.dumps([channel_name or channel_id], ensure_ascii=False),
                        json.dumps([(channel_name or channel_id).casefold()], ensure_ascii=False),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return source_id

    def _logical(self, logical_id, display_name, *, status="active", source_count=1):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO iptv_logical_channels(id, canonical_key, display_name, status, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (logical_id, f"key-{logical_id}", display_name, status, self.now, self.now),
            )
            for index in range(source_count):
                sub = conn.execute(
                    """
                    INSERT INTO subscriptions(title, url, created_at)
                    VALUES(?, ?, ?)
                    """,
                    (f"Subscription {logical_id}-{index}", f"https://iptv.example/{logical_id}/{index}.m3u", self.now),
                ).lastrowid
                channel = conn.execute(
                    """
                    INSERT INTO channels(subscription_id, name, url, tvg_id, tvg_name)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (sub, display_name, f"https://stream.example/{logical_id}/{index}", logical_id, display_name),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO iptv_logical_channel_members(
                        logical_channel_id, channel_id, created_at, updated_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (logical_id, channel, self.now, self.now),
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
        status="matched",
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
                ) VALUES(?, ?, ?, ?, 'exact_tvg_id', 100, ?, ?, ?, ?)
                """,
                (logical_id, source_id, channel_id, status, int(locked), origin, self.now, self.now),
            )
            conn.commit()
        finally:
            conn.close()

    def _shadow_run(self, decisions):
        run_id = "management-run"
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO epg_match_shadow_runs(run_id, status, started_at, finished_at)
                VALUES(?, 'success', ?, ?)
                """,
                (run_id, self.now, self.now),
            )
            for logical_id, status, candidate_count in decisions:
                conn.execute(
                    """
                    INSERT INTO epg_match_shadow_decisions(
                        run_id, logical_channel_id, status, match_type, confidence,
                        reasons_json, hint_conflicts_json, candidate_count
                    ) VALUES(?, ?, ?, 'normalized_name', 80, ?, ?, ?)
                    """,
                    (
                        run_id, logical_id, status,
                        json.dumps(["deterministic fixture"]),
                        json.dumps([]), candidate_count,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return run_id

    def _candidate(
        self,
        run_id,
        logical_id,
        rank,
        source_id,
        channel_id,
        *,
        evidence=None,
    ):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO epg_match_shadow_candidates(
                    run_id, logical_channel_id, rank, epg_source_id, epg_channel_id,
                    match_type, confidence, source_status, source_enabled,
                    auto_applicable, evidence_json
                ) VALUES(?, ?, ?, ?, ?, 'normalized_name', 80, 'success', 1, 1, ?)
                """,
                (
                    run_id, logical_id, rank, source_id, channel_id,
                    json.dumps(evidence or [{"rule": "normalized"}], ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _business_digest(self):
        tables = (
            "epg_sources", "epg_channels", "epg_programs",
            "iptv_logical_channels", "iptv_logical_channel_members",
            "iptv_logical_channel_epg_bindings", "epg_source_preference_evidence",
            "epg_match_shadow_runs", "epg_match_shadow_decisions",
            "epg_match_shadow_candidates",
            "automation_task_config", "automation_task_state",
        )
        conn = self._connect()
        try:
            digest = hashlib.sha256()
            for table in tables:
                digest.update(table.encode())
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall():
                    digest.update(json.dumps(dict(row), sort_keys=True, ensure_ascii=False).encode())
            return digest.hexdigest()
        finally:
            conn.close()

    async def test_empty_overview_and_lists_are_stable(self):
        overview = await self.management.get_epg_management_overview()
        self.assertEqual(overview["sources"], {
            "total": 0, "healthy": 0, "stale": 0, "failed": 0, "disabled": 0,
        })
        self.assertEqual(overview["logical_channels"], {
            "total": 0, "bound": 0, "unbound": 0,
            "not_applicable": 0, "needs_attention": 0,
        })
        self.assertEqual((await self.management.list_epg_matching_channels())["items"], [])
        self.assertEqual((await self.management.search_epg_catalog())["items"], [])

    async def test_default_source_bootstrap_has_stable_builtin_identity(self):
        sources = await self.epg.ensure_default_epg_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["name"], "51zmt")
        self.assertEqual(sources[0]["url"], "http://epg.51zmt.top:8000/e.xml.gz")
        conn = self._connect()
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(epg_sources)")}
        finally:
            conn.close()
        self.assertTrue({"id", "name", "url", "enabled"}.issubset(columns))
        self.assertTrue({"source_origin", "builtin_key"}.issubset(columns))
        projection = (await self.management.list_epg_source_statuses())[0]
        self.assertEqual(projection["name"], "51zmt")
        self.assertEqual(projection["source_origin"], "builtin")
        self.assertEqual(projection["builtin_key"], "china_51zmt")
        self.assertFalse(projection["capabilities"]["can_delete"])
        self.assertFalse(projection["capabilities"]["can_edit_url"])

    async def test_existing_custom_source_does_not_replace_builtin_bootstrap(self):
        custom_id = await self._source("Custom")
        sources = await self.epg.ensure_default_epg_sources()
        self.assertEqual(len(sources), 2)
        custom = next(row for row in sources if row["id"] == custom_id)
        builtin = next(row for row in sources if row["source_origin"] == "builtin")
        self.assertEqual(custom["source_origin"], "custom")
        self.assertEqual(builtin["builtin_key"], "china_51zmt")

    async def test_source_projection_health_security_and_missing_automation(self):
        secret_url = "https://user:password@guide.example/feed.xml?token=plain-token&signature=plain-signature"
        await self._source(
            "Healthy", status="success", url=secret_url,
            channel_id="healthy", last_error=f"fetch {secret_url} Authorization: Bearer plain-auth Cookie=plain-cookie",
        )
        await self._source("Stale", status="stale")
        await self._source("Failed", status="failed")
        await self._source("Disabled", status="disabled", enabled=False)

        response = await self.client.get("/api/admin/epg/sources")
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual([row["refresh"]["status"] for row in rows], [
            "healthy", "stale", "failed", "disabled",
        ])
        self.assertEqual(rows[0]["display_url"], "https://guide.example/feed.xml")
        self.assertEqual(rows[0]["automation"]["last_run_status"], "never_run")
        self.assertFalse(rows[0]["automation"]["scheduled"])
        encoded = json.dumps(rows, ensure_ascii=False)
        for secret in ("plain-token", "plain-signature", "password@", "plain-auth", "plain-cookie"):
            self.assertNotIn(secret, encoded)
        for internal in ("revision", "evidence_json", "task_id", "run_token"):
            self.assertNotIn(internal, encoded)

    async def test_source_automation_status_is_bulk_and_handles_missing_task(self):
        first = await self._source("First")
        second = await self._source("Second")
        registry = self.automation.AutomationRegistry()
        definition = self.epg_tasks.create_epg_task_definition(first, self.main.http_client)
        registry.register(definition)
        repository = self.automation.AutomationRepository(self.db)
        await repository.ensure_config(definition)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE automation_task_state SET last_status='running', last_started_at=? WHERE task_id=?",
                (self.now, definition.task_id),
            )
            conn.commit()
        finally:
            conn.close()
        service = SimpleNamespace(
            registry=registry,
            repository=repository,
            is_started=True,
        )
        self.main.app.state.automation_service = service
        with mock.patch.object(repository, "get_config", wraps=repository.get_config) as get_config, \
             mock.patch.object(repository, "get_state", wraps=repository.get_state) as get_state, \
             mock.patch.object(repository, "list_configs", wraps=repository.list_configs) as list_configs, \
             mock.patch.object(repository, "list_states", wraps=repository.list_states) as list_states:
            rows = await self.management.list_epg_source_statuses(service)
        by_id = {row["id"]: row for row in rows}
        self.assertTrue(by_id[first]["automation"]["scheduled"])
        self.assertTrue(by_id[first]["automation"]["running"])
        self.assertFalse(by_id[second]["automation"]["scheduled"])
        get_config.assert_not_awaited()
        get_state.assert_not_awaited()
        list_configs.assert_awaited_once()
        list_states.assert_awaited_once()

    async def test_realistic_overview_uses_logical_authority_and_excludes_legacy_orphans(self):
        healthy = await self._source("Healthy", channel_id="epg-bound", channel_name="Bound EPG")
        stale = await self._source("Stale", status="stale")
        failed = await self._source("Failed", status="failed")
        disabled = await self._source("Disabled", status="disabled", enabled=False)
        self.assertEqual(len({healthy, stale, failed, disabled}), 4)

        self._logical("lc-bound", "Bound")
        self._binding("lc-bound", healthy, "epg-bound")
        self._logical("lc-unmatched", "Unmatched")
        self._logical("lc-ambiguous", "Ambiguous")
        self._logical("lc-missing", "Missing")
        self._binding("lc-missing", healthy, "gone")
        self._logical("lc-preference", "Preference")
        self._logical("lc-split", "Split", status="split_conflict")
        self._logical("lc-merge", "Merge", status="merge_conflict")
        self._logical("lc-orphan", "Orphan", status="orphaned", source_count=0)
        self._shadow_run((("lc-ambiguous", "ambiguous", 2),))

        conn = self._connect()
        try:
            for source_id in (healthy, stale):
                conn.execute(
                    """
                    INSERT INTO epg_source_preference_evidence(
                        logical_channel_id, origin, epg_source_id, resolution_status,
                        evidence_json, created_at, updated_at
                    ) VALUES('lc-preference', 'manual', ?, 'manual', '{}', ?, ?)
                    """,
                    (source_id, self.now, self.now),
                )
            conn.commit()
        finally:
            conn.close()

        overview = await self.management.get_epg_management_overview()
        self.assertEqual(overview["sources"], {
            "total": 4, "healthy": 1, "stale": 1, "failed": 1, "disabled": 1,
        })
        self.assertEqual(overview["logical_channels"], {
            "total": 5, "bound": 1, "unbound": 1,
            "not_applicable": 0, "needs_attention": 3,
        })
        self.assertEqual(overview["diagnostics"]["ambiguous"], 1)
        self.assertEqual(overview["diagnostics"]["missing_target"], 1)
        self.assertEqual(overview["diagnostics"]["preference_conflict"], 1)
        self.assertEqual(overview["diagnostics"]["split_conflict"], 1)
        self.assertEqual(overview["diagnostics"]["merge_conflict"], 1)
        self.assertEqual(overview["diagnostics"]["orphan"], 1)
        self.assertNotEqual(overview["logical_channels"]["needs_attention"], 67)

    async def test_matching_list_is_paginated_filterable_and_deterministic(self):
        source = await self._source("Guide", channel_id="shared", channel_name="Shared EPG")
        self._logical("lc-z", "Zulu", source_count=2)
        self._binding("lc-z", source, "shared", origin="manual", locked=True)
        self._logical("lc-a", "Alpha")
        self._logical("lc-m", "Middle")
        self._logical("lc-history-a", "Aardvark history", status="orphaned", source_count=0)
        self._logical("lc-history-z", "Zulu history", status="orphaned", source_count=0)

        first_page = await self.management.list_epg_matching_channels(page=1, page_size=2)
        second_page = await self.management.list_epg_matching_channels(page=2, page_size=2)
        self.assertEqual(first_page["total"], 3)
        self.assertEqual(first_page["logical_scope"], "active")
        self.assertEqual(
            [item["channel"]["display_name"] for item in first_page["items"]],
            ["Alpha", "Middle"],
        )
        self.assertTrue(all(item["channel"]["state"] == "active" for item in first_page["items"]))
        self.assertEqual(second_page["items"][0]["channel"]["display_name"], "Zulu")
        history = await self.management.list_epg_matching_channels(logical_scope="history")
        self.assertEqual(history["total"], 2)
        self.assertTrue(all(item["channel"]["state"] != "active" for item in history["items"]))
        all_rows = await self.management.list_epg_matching_channels(logical_scope="all")
        self.assertEqual(all_rows["total"], 5)
        api_default = (await self.client.get(
            "/api/admin/epg/matching?page=1&page_size=2"
        )).json()
        self.assertEqual(api_default["total"], 3)
        self.assertEqual(api_default["logical_scope"], "active")
        self.assertTrue(all(
            item["channel"]["state"] == "active" for item in api_default["items"]
        ))
        api_history = (await self.client.get(
            "/api/admin/epg/matching?logical_scope=history"
        )).json()
        self.assertEqual(api_history["total"], 2)
        bound = await self.management.list_epg_matching_channels(scope="bound")
        self.assertEqual(bound["total"], 1)
        self.assertEqual(bound["items"][0]["binding"]["epg_source_id"], source)
        self.assertEqual(bound["items"][0]["channel"]["member_count"], 2)
        self.assertEqual(bound["items"][0]["channel"]["source_count"], 2)
        searched = await self.management.list_epg_matching_channels(text="shared epg")
        self.assertEqual(searched["total"], 1)
        source_filtered = await self.management.list_epg_matching_channels(source_id=source)
        self.assertEqual(source_filtered["total"], 1)
        unmatched = await self.management.list_epg_matching_channels(
            diagnostic_status="unmatched"
        )
        self.assertEqual(unmatched["total"], 2)
        self.assertNotIn("canonical_key", json.dumps(first_page))
        self.assertNotIn("shadow_run_id", json.dumps(first_page))

    async def test_detail_does_not_treat_legacy_mapping_as_production_read(self):
        source = await self._source(
            "LegacyGuide", channel_id="legacy-target", channel_name="Legacy Target"
        )
        self._logical("lc-legacy", "Legacy fallback")
        self._logical(
            "lc-legacy-history", "Legacy fallback history",
            status="orphaned", source_count=0,
        )
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE iptv_logical_channels SET canonical_key=? WHERE id=?",
                ("key-lc-legacy", "lc-legacy-history"),
            )
            conn.commit()
        finally:
            conn.close()
        overview = await self.management.get_epg_management_overview()
        detail = await self.management.get_epg_matching_detail("lc-legacy")
        history_detail = await self.management.get_epg_matching_detail(
            "lc-legacy-history"
        )

        self.assertEqual(overview["logical_channels"]["bound"], 0)
        self.assertEqual(overview["logical_channels"]["unbound"], 1)
        self.assertEqual(detail["binding"]["status"], "unbound")
        self.assertEqual(detail["production_read"], {"status": "none"})
        self.assertEqual(history_detail["production_read"]["status"], "not_applicable")
        self.assertNotIn("legacy_mapping", json.dumps(detail))

    async def test_composite_identity_keeps_same_channel_id_sources_distinct(self):
        first = await self._source("First", channel_id="shared", channel_name="First Shared")
        second = await self._source("Second", channel_id="shared", channel_name="Second Shared")
        self._logical("lc-second", "Second logical")
        self._binding("lc-second", second, "shared")
        rows = await self.management.list_epg_matching_channels(scope="bound")
        self.assertEqual(rows["items"][0]["binding"]["epg_source_id"], second)
        self.assertEqual(rows["items"][0]["binding"]["epg_source_name"], "Second")
        self.assertEqual(rows["items"][0]["binding"]["epg_channel_display_name"], "Second Shared")
        self.assertNotEqual(first, second)

    async def test_detail_returns_bounded_sanitized_candidates_without_running_matcher(self):
        first = await self._source(
            "First", channel_id="shared", channel_name="First Shared",
            url="https://guide.example/a.xml?token=source-secret",
        )
        second = await self._source("Second", channel_id="shared", channel_name="Second Shared")
        self._logical("lc-detail", "Detail")
        run_id = self._shadow_run((("lc-detail", "ambiguous", 2),))
        self._candidate(
            run_id, "lc-detail", 1, first, "shared",
            evidence=[{
                "rule": "normalized",
                "debug": "https://guide.example/a.xml?token=candidate-secret",
                "note": "signature=signature-secret",
                "Authorization": "Bearer auth-secret",
            }],
        )
        self._candidate(run_id, "lc-detail", 2, second, "shared")

        with mock.patch.object(
            importlib.import_module("epg_match_shadow"),
            "run_epg_match_shadow",
            new=mock.AsyncMock(),
        ) as matcher:
            detail = await self.management.get_epg_matching_detail(
                "lc-detail", candidate_limit=1
            )
        matcher.assert_not_awaited()
        self.assertEqual(detail["diagnostic"]["status"], "ambiguous")
        self.assertEqual(len(detail["candidates"]), 1)
        self.assertEqual(detail["latest_matcher_decision"]["candidate_count"], 2)
        self.assertEqual(detail["candidates"][0]["epg_source_id"], first)
        encoded = json.dumps(detail, ensure_ascii=False)
        for secret in ("source-secret", "candidate-secret", "signature-secret", "auth-secret"):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("run_id", encoded)

    async def test_catalog_search_is_paginated_source_aware_and_filters_health(self):
        current = await self._source("Current", channel_id="shared", channel_name="News One")
        stale = await self._source("Stale", status="stale", channel_id="shared", channel_name="News Two")
        disabled = await self._source(
            "Disabled", status="disabled", enabled=False,
            channel_id="other", channel_name="Other",
        )
        page = await self.management.search_epg_catalog(text="news", page_size=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 1)
        current_only = await self.management.search_epg_catalog(availability="current")
        self.assertEqual(current_only["total"], 1)
        self.assertEqual(current_only["items"][0]["epg_source_id"], current)
        stale_only = await self.management.search_epg_catalog(source_id=stale)
        self.assertEqual(stale_only["items"][0]["source_health"], "stale")
        enabled = await self.management.search_epg_catalog(availability="enabled")
        self.assertEqual(enabled["total"], 2)
        identities = {
            (item["epg_source_id"], item["epg_channel_id"])
            for item in (await self.management.search_epg_catalog())["items"]
        }
        self.assertIn((current, "shared"), identities)
        self.assertIn((stale, "shared"), identities)
        self.assertIn((disabled, "other"), identities)
        self.assertNotIn("programme", json.dumps(enabled))

    async def test_all_management_gets_are_zero_business_writes(self):
        source = await self._source("Guide", channel_id="epg", channel_name="Guide EPG")
        self._logical("lc-read", "Read only")
        self._binding("lc-read", source, "epg")
        before = self._business_digest()
        paths = (
            "/api/admin/epg/overview",
            "/api/admin/epg/sources",
            "/api/admin/epg/matching",
            "/api/admin/epg/matching/lc-read",
            "/api/admin/epg/catalog?q=guide",
        )
        for path in paths:
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self._business_digest(), before)

    async def test_matching_list_connection_count_does_not_grow_with_rows(self):
        original_connect = self.db._connect
        with mock.patch.object(self.db, "_connect", wraps=original_connect) as connect:
            await self.management.list_epg_matching_channels(page_size=1)
            empty_count = connect.call_count
        for index in range(80):
            self._logical(f"lc-{index:03d}", f"Channel {index:03d}")
        with mock.patch.object(self.db, "_connect", wraps=original_connect) as connect:
            await self.management.list_epg_matching_channels(page_size=80)
            populated_count = connect.call_count
        self.assertEqual(empty_count, 1)
        self.assertEqual(populated_count, 1)

    async def test_routes_require_admin_validate_filters_and_return_not_found(self):
        self.main.app.dependency_overrides.clear()
        for path in (
            "/api/admin/epg/overview",
            "/api/admin/epg/sources",
            "/api/admin/epg/matching",
            "/api/admin/epg/catalog",
        ):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 401)

        async def admin_override():
            return {"id": 1, "username": "admin", "role": "admin"}

        self.main.app.dependency_overrides[self.main.require_admin] = admin_override
        self.assertEqual(
            (await self.client.get("/api/admin/epg/matching?scope=invalid")).status_code,
            400,
        )
        self.assertEqual(
            (await self.client.get("/api/admin/epg/matching?logical_scope=invalid")).status_code,
            400,
        )
        self.assertEqual(
            (await self.client.get("/api/admin/epg/catalog?availability=invalid")).status_code,
            400,
        )
        self.assertEqual(
            (await self.client.get("/api/admin/epg/matching/not-found")).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
