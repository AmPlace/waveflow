import asyncio
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


def _clear_modules():
    for name in ('epg_match_shadow', 'epg_matcher', 'epg_catalog', 'epg_bindings', 'epg_preference_evidence', 'epg_source_preference', 'iptv_channels', 'database'):
        sys.modules.pop(name, None)


class EpgMatchShadowApplyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        self.db_path = os.path.join(self.tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_DB_PATH'] = self.db_path
        _clear_modules()
        self.db = importlib.import_module('database')
        self.shadow = importlib.import_module('epg_match_shadow')
        self.bindings = importlib.import_module('epg_bindings')
        self.preference_evidence = importlib.import_module('epg_preference_evidence')
        await self.db.initialize()

    async def asyncTearDown(self):
        if self.old_db_path is None:
            os.environ.pop('WAVEFLOW_DB_PATH', None)
        else:
            os.environ['WAVEFLOW_DB_PATH'] = self.old_db_path
        _clear_modules()
        self.tmpdir.cleanup()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    async def _source(self, name='source', channel_id='CCTV1', *, status='success', enabled=1):
        source_id = await self.db.add_epg_source(name, f'https://source.example/{name}')
        conn = self._connect()
        try:
            now = '2026-08-06T00:00:00+00:00'
            conn.execute(
                "UPDATE epg_sources SET enabled=?, last_status=?, revision=1, updated_at=? WHERE id=?",
                (enabled, status, now, source_id),
            )
            conn.execute(
                "INSERT INTO epg_channels(source_id, channel_id, display_names, normalized_names) VALUES(?, ?, ?, ?)",
                (source_id, channel_id, json.dumps([channel_id]), json.dumps([channel_id.lower()])),
            )
            conn.commit()
        finally:
            conn.close()
        return source_id

    async def _logical(self, logical_id='logical-1', *, tvg_id='CCTV1', status='active'):
        sub = await self.db.add_subscription(
            f'sub-{logical_id}', f'https://playlist.example/{logical_id}.m3u'
        )
        await self.db.add_channels_bulk(sub, [{
            'name': 'CCTV 1', 'url': 'https://stream.example/live',
            'group_name': 'test', 'logo_url': '', 'tvg_id': tvg_id, 'tvg_name': 'CCTV1',
        }])
        conn = self._connect()
        try:
            channel_id = conn.execute('SELECT id FROM channels ORDER BY id DESC LIMIT 1').fetchone()['id']
            now = '2026-08-06T00:00:00+00:00'
            conn.execute(
                "INSERT INTO iptv_logical_channels(id, canonical_key, display_name, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (logical_id, logical_id, 'CCTV1', status, now, now),
            )
            conn.execute(
                "INSERT INTO iptv_logical_channel_members(logical_channel_id, channel_id, membership_reason, membership_confidence, variant_type, created_at, updated_at) VALUES(?, ?, 'test', 100, 'standard', ?, ?)",
                (logical_id, channel_id, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return logical_id

    def _binding_rows(self):
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM iptv_logical_channel_epg_bindings ORDER BY id'
            ).fetchall()]
        finally:
            conn.close()

    async def _successful_run(self, logical_id='logical-1', source_name='source', channel_id='CCTV1'):
        source = await self._source(source_name, channel_id)
        await self._logical(logical_id, tvg_id=channel_id)
        run = await self.shadow.run_epg_match_shadow()
        self.assertEqual(run['status'], 'success')
        self.assertEqual(run['matched_count'], 1)
        return run, source

    async def _manual_run(self, status='success', *, logical_id='logical-1', decision_status='matched', source_id=None, channel_id='CCTV1', match_type='exact_tvg_id', confidence=100):
        now = '2026-08-06T00:00:00+00:00'
        run_id = f'run-{status}-{logical_id}-{decision_status}'
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO epg_match_shadow_runs(run_id, status, started_at, finished_at) VALUES(?, ?, ?, ?)",
                (run_id, status, now, now),
            )
            conn.execute(
                """INSERT INTO epg_match_shadow_decisions(
                    run_id, logical_channel_id, status, selected_epg_source_id,
                    selected_epg_channel_id, match_type, confidence,
                    existing_binding_action, reasons_json, hint_conflicts_json,
                    candidate_count, error
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'none', '[]', '[]', 1, '')""",
                (run_id, logical_id, decision_status, source_id, channel_id, match_type, confidence),
            )
            conn.commit()
        finally:
            conn.close()
        return run_id

    async def test_matched_success_applies_with_provenance_and_is_idempotent(self):
        run, source = await self._successful_run()
        first = await self.shadow.apply_epg_match_shadow_run(run['run_id'])
        self.assertEqual(first['run_status'], 'success')
        self.assertEqual(first['eligible_decision_count'], 1)
        self.assertEqual(first['applied_count'], 1)
        rows = self._binding_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['epg_source_id'], rows[0]['epg_channel_id']), (source, 'CCTV1'))
        self.assertEqual(rows[0]['origin'], 'automatic')
        self.assertEqual(rows[0]['shadow_run_id'], run['run_id'])
        second = await self.shadow.apply_epg_match_shadow_run(run['run_id'])
        self.assertEqual(second['applied_count'], 0)
        self.assertEqual(second['already_bound_count'], 1)

    async def test_non_success_and_missing_runs_are_rejected_without_writes(self):
        await self._source()
        await self._logical()
        for status in ('running', 'partial', 'failed', 'cancelled'):
            run_id = await self._manual_run(status)
            result = await self.shadow.apply_epg_match_shadow_run(run_id)
            self.assertEqual(result['applied_count'], 0)
            self.assertFalse(result['rollback'])
            self.assertIn(status, result['error'])
        missing = await self.shadow.apply_epg_match_shadow_run('does-not-exist')
        self.assertEqual(missing['applied_count'], 0)
        self.assertIn('不存在', missing['error'])
        self.assertEqual(len(self._binding_rows()), 0)

    async def test_only_matched_with_identity_is_eligible(self):
        source = await self._source()
        await self._logical()
        statuses = ('ambiguous', 'unmatched', 'conflict', 'locked_preserved', 'existing_preserved', 'not_applicable')
        for status in statuses:
            run_id = await self._manual_run(decision_status=status, source_id=source)
            result = await self.shadow.apply_epg_match_shadow_run(run_id)
            self.assertEqual(result['eligible_decision_count'], 0)
            self.assertEqual(result['skipped_status_count'], 1)
        self.assertEqual(len(self._binding_rows()), 0)

    async def test_target_source_and_logical_revalidation(self):
        source = await self._source()
        await self._logical()
        run_id = await self._manual_run(source_id=source)
        conn = self._connect()
        try:
            conn.execute("UPDATE epg_sources SET enabled=0 WHERE id=?", (source,))
            conn.commit()
        finally:
            conn.close()
        result = await self.shadow.apply_epg_match_shadow_run(run_id)
        self.assertEqual(result['applied_count'], 0)
        self.assertEqual(result['stale_decision_count'], 1)

        source = await self._source('source-2', 'CCTV2')
        await self._logical('logical-2', tvg_id='CCTV2')
        run_id = await self._manual_run(logical_id='logical-2', source_id=source, channel_id='CCTV2')
        conn = self._connect()
        try:
            conn.execute("UPDATE iptv_logical_channels SET status='split_conflict' WHERE id='logical-2'")
            conn.commit()
        finally:
            conn.close()
        result = await self.shadow.apply_epg_match_shadow_run(run_id)
        self.assertEqual(result['logical_conflict_count'], 1)
        self.assertEqual(result['applied_count'], 0)

    async def test_revalidation_requires_same_match_and_selected_target(self):
        run, source = await self._successful_run()
        conn = self._connect()
        try:
            conn.execute("UPDATE channels SET tvg_id='different', tvg_name='Different', name='Different' WHERE tvg_id='CCTV1'")
            conn.execute("UPDATE iptv_logical_channels SET canonical_key='different', display_name='Different' WHERE id='logical-1'")
            conn.commit()
        finally:
            conn.close()
        result = await self.shadow.apply_epg_match_shadow_run(run['run_id'])
        self.assertEqual(result['revalidation_mismatch_count'], 1)
        self.assertEqual(len(self._binding_rows()), 0)

        source = await self._source('source-2', 'CCTV2')
        await self._logical('logical-2', tvg_id='CCTV2')
        run_id = await self._manual_run(logical_id='logical-2', source_id=source, channel_id='CCTV2')
        conn = self._connect()
        try:
            conn.execute("DELETE FROM epg_channels WHERE source_id=? AND channel_id='CCTV2'", (source,))
            conn.commit()
        finally:
            conn.close()
        result = await self.shadow.apply_epg_match_shadow_run(run_id)
        self.assertEqual(result['target_missing_count'], 1)

    async def test_revalidation_different_target_is_not_auto_selected(self):
        run, source_a = await self._successful_run()
        source_b = await self._source('source-b', 'ALT')
        conn = self._connect()
        try:
            conn.execute("UPDATE channels SET tvg_id='ALT', tvg_name='ALT', name='ALT' WHERE tvg_id='CCTV1'")
            conn.execute("UPDATE iptv_logical_channels SET canonical_key='alt', display_name='ALT' WHERE id='logical-1'")
            conn.commit()
        finally:
            conn.close()
        result = await self.shadow.apply_epg_match_shadow_run(run['run_id'])
        self.assertEqual(result['revalidation_mismatch_count'], 1)
        self.assertEqual(result['applied_count'], 0)
        self.assertEqual(len(self._binding_rows()), 0)
        self.assertNotEqual(source_a, source_b)

    async def test_revalidation_uses_current_preference_and_rejects_changed_target(self):
        source_a = await self._source('source-a', 'CCTV1')
        source_b = await self._source('source-b', 'CCTV1')
        await self._logical(tvg_id='CCTV1')
        await self.preference_evidence.set_manual_source_preference(
            epg_source_id=source_a, logical_channel_id='logical-1'
        )
        run = await self.shadow.run_epg_match_shadow()
        decision = await self.shadow.get_shadow_decision(run['run_id'], 'logical-1')
        self.assertEqual(decision.selected_identity, self.shadow.EpgChannelIdentity(source_a, 'CCTV1'))

        await self.preference_evidence.delete_manual_source_preference(
            epg_source_id=source_a, logical_channel_id='logical-1'
        )
        await self.preference_evidence.set_manual_source_preference(
            epg_source_id=source_b, logical_channel_id='logical-1'
        )
        result = await self.shadow.apply_epg_match_shadow_run(run['run_id'])
        self.assertEqual(result['revalidation_mismatch_count'], 1)
        self.assertEqual(result['applied_count'], 0)
        self.assertEqual(self._binding_rows(), [])

    async def test_existing_binding_is_never_overwritten(self):
        run, source = await self._successful_run()
        same = await self.bindings.create_matched_epg_binding('logical-1', source, 'CCTV1', locked=False)
        result = await self.shadow.apply_epg_match_shadow_run(run['run_id'])
        self.assertEqual(result['already_bound_count'], 1)
        self.assertEqual(result['applied_count'], 0)
        self.assertEqual((self._binding_rows()[0]['id'], same.id), (same.id, same.id))

        await self._logical('logical-2', tvg_id='CCTV1')
        other = await self._source('source-2', 'ALT')
        run2 = await self.shadow.run_epg_match_shadow()
        await self.bindings.create_matched_epg_binding('logical-2', other, 'ALT', locked=True)
        result = await self.shadow.apply_epg_match_shadow_run(run2['run_id'])
        self.assertEqual(result['locked_conflict_count'], 1)
        self.assertEqual(result['applied_count'], 0)

    async def test_unlocked_existing_different_target_is_not_overwritten(self):
        source_a = await self._source('source-a', 'CCTV1')
        source_b = await self._source('source-b', 'ALT')
        await self._logical(tvg_id='CCTV1')
        run = await self.shadow.run_epg_match_shadow()
        self.assertEqual(run['matched_count'], 1)
        await self.bindings.create_matched_epg_binding('logical-1', source_b, 'ALT', locked=False)
        result = await self.shadow.apply_epg_match_shadow_run(run['run_id'])
        self.assertEqual(result['existing_conflict_count'], 1)
        binding = (await self.bindings.get_epg_binding('logical-1'))
        self.assertEqual(binding.epg_source_id, source_b)
        self.assertNotEqual(binding.epg_source_id, source_a)

    async def test_atomic_rollback_on_second_insert_failure(self):
        source = await self._source()
        await self._logical('logical-1', tvg_id='CCTV1')
        await self._source('source-2', 'CCTV2')
        await self._logical('logical-2', tvg_id='CCTV2')
        run = await self.shadow.run_epg_match_shadow()
        self.assertEqual(run['matched_count'], 2)
        original_now = self.db._utc_now
        calls = 0

        def fail_on_second_now():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('injected insert failure')
            return original_now()

        with mock.patch.object(self.shadow.db, '_utc_now', side_effect=fail_on_second_now):
            result = await self.shadow.apply_epg_match_shadow_run(run['run_id'])
        self.assertTrue(result['rollback'])
        self.assertIn('injected insert failure', result['error'])
        self.assertEqual(result['applied_count'], 1)
        self.assertEqual(len(self._binding_rows()), 0)

    async def test_provenance_field_is_readable_for_existing_manual_bindings(self):
        source = await self._source()
        await self._logical()
        binding = await self.bindings.create_matched_epg_binding('logical-1', source, 'CCTV1')
        self.assertIsNone(binding.shadow_run_id)
        self.assertIsNone((await self.bindings.list_epg_bindings())[0].shadow_run_id)


if __name__ == '__main__':
    unittest.main()
