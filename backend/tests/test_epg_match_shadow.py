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
    for name in ('epg_match_shadow', 'epg_matcher', 'epg_catalog', 'epg_bindings', 'iptv_channels', 'database'):
        sys.modules.pop(name, None)


class EpgMatchShadowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        self.db_path = os.path.join(self.tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_DB_PATH'] = self.db_path
        _clear_modules()
        self.db = importlib.import_module('database')
        self.shadow = importlib.import_module('epg_match_shadow')
        self.matcher = importlib.import_module('epg_matcher')
        self.catalog_module = importlib.import_module('epg_catalog')
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

    async def _source(self, name, channel_ids, *, status='success', enabled=1):
        source_id = await self.db.add_epg_source(name, f'https://source.example/{name}')
        conn = self._connect()
        try:
            now = '2026-08-06T00:00:00+00:00'
            conn.execute(
                "UPDATE epg_sources SET enabled=?, last_status=?, revision=1, updated_at=? WHERE id=?",
                (enabled, status, now, source_id),
            )
            for channel_id, names in channel_ids:
                conn.execute(
                    "INSERT INTO epg_channels(source_id, channel_id, display_names, normalized_names) VALUES(?, ?, ?, ?)",
                    (source_id, channel_id, json.dumps(names), json.dumps([str(n).lower() for n in names])),
                )
            conn.commit()
        finally:
            conn.close()
        return source_id

    async def _logical(self, logical_id='logical-1', *, key='cctv1', name='CCTV1', tvg_id='CCTV1', raw_name='CCTV 1'):
        sub = await self.db.add_subscription(
            f'sub-{logical_id}',
            f'https://playlist.example/{logical_id}.m3u',
        )
        await self.db.add_channels_bulk(sub, [{
            'name': raw_name, 'url': 'https://stream.example/live', 'group_name': 'test',
            'logo_url': '', 'tvg_id': tvg_id, 'tvg_name': name,
        }])
        conn = self._connect()
        try:
            channel_id = conn.execute('SELECT id FROM channels ORDER BY id DESC LIMIT 1').fetchone()['id']
            now = '2026-08-06T00:00:00+00:00'
            conn.execute(
                "INSERT INTO iptv_logical_channels(id, canonical_key, display_name, status, created_at, updated_at) VALUES(?, ?, ?, 'active', ?, ?)",
                (logical_id, key, name, now, now),
            )
            conn.execute(
                "INSERT INTO iptv_logical_channel_members(logical_channel_id, channel_id, membership_reason, membership_confidence, variant_type, created_at, updated_at) VALUES(?, ?, 'test', 100, 'standard', ?, ?)",
                (logical_id, channel_id, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return logical_id

    def _counts(self):
        conn = self._connect()
        try:
            return {
                table: conn.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()['n']
                for table in ('epg_match_shadow_runs', 'epg_match_shadow_decisions', 'epg_match_shadow_candidates', 'iptv_logical_channel_epg_bindings')
            }
        finally:
            conn.close()

    async def test_empty_dataset_is_success_and_persisted(self):
        result = await self.shadow.run_epg_match_shadow()
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['logical_channel_count'], 0)
        self.assertEqual(self._counts()['epg_match_shadow_runs'], 1)

    async def test_matched_persists_composite_target_and_candidates(self):
        source = await self._source('one', [('CCTV1', ('CCTV One',))])
        await self._logical(tvg_id='CCTV1')
        result = await self.shadow.run_epg_match_shadow()
        self.assertEqual(result['matched_count'], 1)
        decision = await self.shadow.get_shadow_decision(result['run_id'], 'logical-1')
        self.assertEqual(decision.selected_identity.as_dict(), {'source_id': source, 'channel_id': 'CCTV1'})
        candidates = await self.shadow.get_shadow_decision_candidates(result['run_id'], 'logical-1')
        self.assertEqual(candidates[0].identity, self.catalog_module.EpgChannelIdentity(source, 'CCTV1'))
        self.assertEqual(candidates[0].rank, 1)

    async def test_preference_snapshot_can_disambiguate_same_tier_and_is_persisted(self):
        source_a = await self._source('source-a', [('CCTV1', ('CCTV1',))])
        source_b = await self._source('source-b', [('CCTV1', ('CCTV1',))])
        await self._logical(tvg_id='CCTV1')
        now = '2026-08-06T00:00:00+00:00'
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO epg_source_preference_evidence(
                    logical_channel_id, origin, epg_source_id, resolution_status,
                    evidence_json, created_at, updated_at
                ) VALUES('logical-1', 'manual', ?, 'manual', '{\"explicit\":true}', ?, ?)""",
                (source_a, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        result = await self.shadow.run_epg_match_shadow()
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['matched_count'], 1)
        self.assertTrue(result['preference_snapshot_fingerprint'])
        decision = await self.shadow.get_shadow_decision(result['run_id'], 'logical-1')
        self.assertEqual(decision.selected_identity, self.catalog_module.EpgChannelIdentity(source_a, 'CCTV1'))
        self.assertIn('preference_applied', decision.reasons)
        self.assertNotEqual(source_a, source_b)

    async def test_ambiguous_unmatched_and_conflict_are_success_not_partial(self):
        await self._source('a', [('CCTV1', ('CCTV1',))])
        await self._source('b', [('CCTV1', ('CCTV1',))])
        await self._logical(tvg_id='CCTV1')
        result = await self.shadow.run_epg_match_shadow()
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['ambiguous_count'], 1)
        await self.shadow.run_epg_match_shadow(stop=asyncio.Event())
        # Replace the logical status to exercise the normal conflict result.
        conn = self._connect()
        try:
            conn.execute("UPDATE iptv_logical_channels SET status='split_conflict' WHERE id='logical-1'")
            conn.commit()
        finally:
            conn.close()
        conflict = await self.shadow.run_epg_match_shadow()
        self.assertEqual(conflict['status'], 'success')
        self.assertEqual(conflict['conflict_count'], 1)

        conn = self._connect()
        try:
            conn.execute("UPDATE channels SET tvg_id='does-not-exist', tvg_name='No Match', name='No Match'")
            conn.execute("UPDATE iptv_logical_channels SET status='active', canonical_key='no-match', display_name='No Match' WHERE id='logical-1'")
            conn.commit()
        finally:
            conn.close()
        unmatched = await self.shadow.run_epg_match_shadow()
        self.assertEqual(unmatched['status'], 'success')
        self.assertEqual(unmatched['unmatched_count'], 1)

    async def test_locked_and_existing_binding_are_preserved_without_writes(self):
        source = await self._source('one', [('CCTV1', ('CCTV1',))])
        await self._logical(tvg_id='CCTV1')
        now = '2026-08-06T00:00:00+00:00'
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO iptv_logical_channel_epg_bindings(logical_channel_id, epg_source_id, epg_channel_id, status, match_type, confidence, locked, origin, created_at, updated_at) VALUES('logical-1', ?, 'CCTV1', 'matched', 'manual', 100, 1, 'manual', ?, ?)",
                (source, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        before = self._counts()
        result = await self.shadow.run_epg_match_shadow()
        decision = await self.shadow.get_shadow_decision(result['run_id'], 'logical-1')
        self.assertEqual(decision.status, 'locked_preserved')
        self.assertEqual(self._counts()['iptv_logical_channel_epg_bindings'], before['iptv_logical_channel_epg_bindings'])

        conn = self._connect()
        try:
            conn.execute('UPDATE iptv_logical_channel_epg_bindings SET locked=0 WHERE logical_channel_id=?', ('logical-1',))
            conn.commit()
        finally:
            conn.close()
        result = await self.shadow.run_epg_match_shadow()
        decision = await self.shadow.get_shadow_decision(result['run_id'], 'logical-1')
        self.assertEqual(decision.status, 'existing_preserved')

    async def test_same_channel_id_across_sources_persists_all_candidates_stably(self):
        first = await self._source('first', [('SAME', ('Same',))])
        second = await self._source('second', [('SAME', ('Same',))])
        await self._logical(tvg_id='SAME')
        result = await self.shadow.run_epg_match_shadow()
        candidates = await self.shadow.get_shadow_decision_candidates(result['run_id'], 'logical-1')
        self.assertEqual([item.identity.source_id for item in candidates], [first, second])
        self.assertEqual([item.rank for item in candidates], [1, 2])

    async def test_snapshot_is_one_input_and_matcher_does_not_reload_catalog(self):
        await self._source('one', [('CCTV1', ('CCTV1',))])
        await self._logical(tvg_id='CCTV1')
        loader = self.shadow.load_epg_match_shadow_snapshot
        with mock.patch.object(self.shadow, 'load_epg_match_shadow_snapshot', wraps=loader) as mocked:
            result = await self.shadow.run_epg_match_shadow()
        self.assertEqual(result['status'], 'success')
        mocked.assert_awaited_once()

    async def test_json_is_bounded_and_does_not_store_transport_or_programme_data(self):
        await self._source('one', [('CCTV1', ('CCTV1',))])
        await self._logical(tvg_id='CCTV1', raw_name='https://token=secret.example/private/programme')
        result = await self.shadow.run_epg_match_shadow()
        conn = self._connect()
        try:
            values = [row[0] for row in conn.execute('SELECT reasons_json FROM epg_match_shadow_decisions')]
            values += [row[0] for row in conn.execute('SELECT evidence_json FROM epg_match_shadow_candidates')]
        finally:
            conn.close()
        self.assertTrue(all(len(value) <= self.shadow.MAX_JSON_LENGTH for value in values))
        joined = '\n'.join(values).lower()
        self.assertNotIn('token=secret', joined)
        self.assertNotIn('programme', joined)
        self.assertNotIn('stream.example', joined)
        self.assertNotIn('source.example', joined)

    async def test_stop_between_channels_persists_cancelled_metadata_without_partial_rows(self):
        await self._source('one', [('A', ('A',)), ('B', ('B',))])
        await self._logical('logical-a', key='a', name='A', tvg_id='A', raw_name='A')
        await self._logical('logical-b', key='b', name='B', tvg_id='B', raw_name='B')
        stop = asyncio.Event()
        calls = 0
        original = self.shadow.match_logical_channel
        def matcher(*args, **kwargs):
            nonlocal calls
            calls += 1
            value = original(*args, **kwargs)
            stop.set()
            return value
        with mock.patch.object(self.shadow, 'match_logical_channel', side_effect=matcher):
            result = await self.shadow.run_epg_match_shadow(stop=stop)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(calls, 1)
        self.assertEqual(self._counts()['epg_match_shadow_decisions'], 0)
        self.assertEqual(self._counts()['epg_match_shadow_candidates'], 0)

    async def test_true_channel_error_is_partial_and_persisted_as_error_decision(self):
        await self._source('one', [('A', ('A',)), ('B', ('B',))])
        await self._logical('logical-a', key='a', name='A', tvg_id='A', raw_name='A')
        await self._logical('logical-b', key='b', name='B', tvg_id='B', raw_name='B')
        original = self.shadow.match_logical_channel
        def matcher(hint, *args, **kwargs):
            if hint.logical_channel_id == 'logical-b':
                raise RuntimeError('synthetic channel failure')
            return original(hint, *args, **kwargs)
        with mock.patch.object(self.shadow, 'match_logical_channel', side_effect=matcher):
            result = await self.shadow.run_epg_match_shadow()
        self.assertEqual(result['status'], 'partial')
        error_decision = await self.shadow.get_shadow_decision(result['run_id'], 'logical-b')
        self.assertEqual(error_decision.status, 'error')
        self.assertIn('synthetic', error_decision.error)

    async def test_all_channel_errors_are_failed_without_partial_decisions(self):
        await self._source('one', [('A', ('A',)), ('B', ('B',))])
        await self._logical('logical-a', key='a', name='A', tvg_id='A', raw_name='A')
        await self._logical('logical-b', key='b', name='B', tvg_id='B', raw_name='B')

        with mock.patch.object(
            self.shadow,
            'match_logical_channel',
            side_effect=RuntimeError('synthetic all-channel failure'),
        ):
            result = await self.shadow.run_epg_match_shadow()

        self.assertEqual(result['status'], 'failed')
        self.assertIn('synthetic all-channel failure', result['error'])
        counts = self._counts()
        self.assertEqual(counts['epg_match_shadow_runs'], 1)
        self.assertEqual(counts['epg_match_shadow_decisions'], 0)
        self.assertEqual(counts['epg_match_shadow_candidates'], 0)

    async def test_persistence_failure_rolls_back_decisions_and_candidates(self):
        await self._source('one', [('A', ('A',))])
        await self._logical(tvg_id='A', raw_name='A')
        original = self.shadow._candidate_records
        with mock.patch.object(self.shadow, '_candidate_records', side_effect=RuntimeError('candidate insert failure')):
            result = await self.shadow.run_epg_match_shadow()
        self.assertEqual(result['status'], 'failed')
        counts = self._counts()
        self.assertEqual(counts['epg_match_shadow_decisions'], 0)
        self.assertEqual(counts['epg_match_shadow_candidates'], 0)
        self.assertEqual(counts['epg_match_shadow_runs'], 1)
        self.assertEqual(original.__name__, '_candidate_records')

    async def test_query_apis_and_latest_completed_run(self):
        source = await self._source('one', [('A', ('A',))])
        await self._logical(tvg_id='A', raw_name='A')
        result = await self.shadow.run_epg_match_shadow()
        self.assertEqual((await self.shadow.get_latest_completed_shadow_run()).run_id, result['run_id'])
        self.assertIsNotNone(await self.shadow.get_shadow_run(result['run_id']))
        self.assertIsNotNone(await self.shadow.get_shadow_decision(result['run_id'], 'logical-1'))
        self.assertEqual((await self.shadow.get_shadow_decision_candidates(result['run_id'], 'logical-1'))[0].identity.source_id, source)

    async def test_no_production_surfaces_are_imported_or_written(self):
        source = open('backend/epg_match_shadow.py', encoding='utf-8').read()
        self.assertNotIn('run_epg_matching', source)
        self.assertNotIn('programme', source.lower())
        self.assertNotIn('automation', source.lower())
        before = self._counts()
        await self.shadow.run_epg_match_shadow()
        after = self._counts()
        self.assertEqual(before['iptv_logical_channel_epg_bindings'], after['iptv_logical_channel_epg_bindings'])


if __name__ == '__main__':
    unittest.main()
