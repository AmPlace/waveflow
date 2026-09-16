import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest


def _clear_modules():
    for name in ('epg_bindings', 'epg_catalog', 'iptv_channels', 'database'):
        sys.modules.pop(name, None)


class EpgBindingReconciliationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        self.db_path = os.path.join(self.tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_DB_PATH'] = self.db_path
        _clear_modules()
        self.db = importlib.import_module('database')
        self.iptv = importlib.import_module('iptv_channels')
        self.bindings = importlib.import_module('epg_bindings')
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

    @staticmethod
    def _channel(name, url):
        return {
            'name': name, 'url': url, 'logo_url': '', 'group_name': 'test',
            'tvg_id': name, 'tvg_name': name,
        }

    async def _subscription(self, title, channels):
        sub_id = await self.db.add_subscription(title, f'https://playlist.example/{title}.m3u')
        await self.db.add_channels_bulk(sub_id, channels)
        return sub_id

    async def _source(self, name='source', channel_id='CCTV1'):
        source_id = await self.db.add_epg_source(name, f'https://epg.example/{name}.xml')
        conn = self._connect()
        try:
            now = '2026-08-06T00:00:00+00:00'
            conn.execute(
                "UPDATE epg_sources SET enabled=1, last_status='success', revision=1, updated_at=? WHERE id=?",
                (now, source_id),
            )
            conn.execute(
                "INSERT INTO epg_channels(source_id, channel_id, display_names, normalized_names) VALUES(?, ?, ?, ?)",
                (source_id, channel_id, json.dumps([channel_id]), json.dumps([channel_id.lower()])),
            )
            conn.commit()
        finally:
            conn.close()
        return source_id

    async def _sync(self):
        return await self.iptv.sync_iptv_logical_channels()

    async def _logical_ids(self):
        conn = self._connect()
        try:
            return [row['id'] for row in conn.execute('SELECT id FROM iptv_logical_channels ORDER BY id')]
        finally:
            conn.close()

    async def _binding(self, logical_id, source_id, channel_id='CCTV1', **kwargs):
        return await self.bindings.create_matched_epg_binding(
            logical_id, source_id, channel_id, match_type='exact', confidence=90, **kwargs
        )

    def _binding_snapshot(self):
        conn = self._connect()
        try:
            return [tuple(row) for row in conn.execute(
                'SELECT logical_channel_id, epg_source_id, epg_channel_id, status, locked, origin, shadow_run_id FROM iptv_logical_channel_epg_bindings ORDER BY logical_channel_id'
            ).fetchall()]
        finally:
            conn.close()

    async def test_rename_and_member_changes_keep_binding_metadata(self):
        sub = await self._subscription('rename', [self._channel('Original', 'https://stream.example/a')])
        await self._sync()
        logical_id = (await self._logical_ids())[0]
        source = await self._source()
        await self._binding(logical_id, source, locked=True, origin='automatic', shadow_run_id='shadow-1')

        conn = self._connect()
        try:
            conn.execute("UPDATE channels SET name='Renamed', tvg_id='Renamed', tvg_name='Renamed' WHERE subscription_id=?", (sub,))
            conn.commit()
        finally:
            conn.close()
        await self._sync()
        self.assertEqual((await self._logical_ids()), [logical_id])
        row = self._binding_snapshot()[0]
        self.assertEqual(row[0], logical_id)
        self.assertEqual(row[3:], ('matched', 1, 'automatic', 'shadow-1'))
        preview = await self.bindings.preview_epg_binding_reconciliation()
        self.assertEqual(preview['counts']['unchanged'], 1)

        sub2 = await self._subscription('member-add', [self._channel('Renamed', 'https://stream.example/b')])
        await self._sync()
        conn = self._connect()
        try:
            count = conn.execute('SELECT COUNT(*) FROM iptv_logical_channel_members WHERE logical_channel_id=?', (logical_id,)).fetchone()[0]
            self.assertEqual(count, 2)
            conn.execute("DELETE FROM channels WHERE subscription_id=?", (sub2,))
            conn.commit()
        finally:
            conn.close()
        await self._sync()
        self.assertEqual(self._binding_snapshot()[0][0], logical_id)

    async def test_orphan_retains_binding_and_forbids_new_automatic_binding(self):
        sub = await self._subscription('orphan', [self._channel('Orphan', 'https://stream.example/orphan')])
        await self._sync()
        logical_id = (await self._logical_ids())[0]
        source = await self._source('orphan-source', 'CCTV1')
        await self._binding(logical_id, source)
        conn = self._connect()
        try:
            conn.execute('DELETE FROM channels WHERE subscription_id=?', (sub,))
            conn.commit()
        finally:
            conn.close()
        await self._sync()
        preview = await self.bindings.preview_epg_binding_reconciliation()
        self.assertEqual(preview['counts']['logical_orphan'], 1)
        self.assertEqual(len(self._binding_snapshot()), 1)
        with self.assertRaisesRegex(ValueError, 'orphaned'):
            await self.bindings.create_matched_epg_binding(
                logical_id, source, 'CCTV1', origin='automatic'
            )

    async def test_automatic_binding_requires_strictly_active_logical_channel(self):
        await self._subscription('automatic-statuses', [
            self._channel('Orphan Status', 'https://stream.example/orphan'),
            self._channel('Split Status', 'https://stream.example/split'),
            self._channel('Merge Status', 'https://stream.example/merge'),
        ])
        await self._sync()
        conn = self._connect()
        try:
            logical_rows = conn.execute(
                'SELECT id, canonical_key FROM iptv_logical_channels ORDER BY canonical_key'
            ).fetchall()
        finally:
            conn.close()
        logical_ids = {
            row['canonical_key']: row['id']
            for row in logical_rows
        }
        source = await self._source('automatic-status-source')
        statuses = {
            logical_ids['orphanstatus']: ('orphaned', 'orphaned'),
            logical_ids['splitstatus']: ('split_conflict', 'conflict'),
            logical_ids['mergestatus']: ('merge_conflict', 'conflict'),
        }
        conn = self._connect()
        try:
            for logical_id, (status, _) in statuses.items():
                conn.execute(
                    'UPDATE iptv_logical_channels SET status=? WHERE id=?',
                    (status, logical_id),
                )
            conn.commit()
        finally:
            conn.close()

        for logical_id, (_, error_fragment) in statuses.items():
            with self.assertRaisesRegex(ValueError, error_fragment):
                await self.bindings.create_matched_epg_binding(
                    logical_id, source, 'CCTV1', origin='automatic'
                )
        self.assertEqual(self._binding_snapshot(), [])

    async def test_split_never_inherits_even_locked_binding(self):
        sub = await self._subscription('split', [
            self._channel('Same Name', 'https://stream.example/a'),
            self._channel('Same Name', 'https://stream.example/b'),
        ])
        await self._sync()
        logical_id = (await self._logical_ids())[0]
        source = await self._source('split-source')
        await self._binding(logical_id, source, locked=True, origin='legacy_migrated')
        conn = self._connect()
        try:
            channel_id = conn.execute('SELECT id FROM channels WHERE subscription_id=? ORDER BY id LIMIT 1', (sub,)).fetchone()['id']
            conn.execute("UPDATE channels SET name='Split Child', tvg_id='Split Child', tvg_name='Split Child' WHERE id=?", (channel_id,))
            conn.commit()
        finally:
            conn.close()
        result = await self._sync()
        self.assertEqual(len(result['split_conflicts']), 1)
        preview = await self.bindings.preview_epg_binding_reconciliation(result)
        self.assertEqual(preview['counts']['split_no_inherit'], 1)
        self.assertEqual(len(self._binding_snapshot()), 1)
        self.assertEqual(preview['items'][0]['inheritance'], 'none')

    async def _prepare_merge(self, *, bindings=(), locked=()):
        await self._subscription('merge-a', [self._channel('Merge A', 'https://stream.example/a')])
        await self._subscription('merge-b', [self._channel('Merge B', 'https://stream.example/b')])
        await self._sync()
        logical_ids = await self._logical_ids()
        source_ids = []
        for index, logical_id in enumerate(logical_ids):
            source_ids.append(await self._source(f'merge-source-{index}', f'EPG-{index}'))
        for index in bindings:
            await self._binding(logical_ids[index], source_ids[index], f'EPG-{index}', locked=index in locked)
        conn = self._connect()
        try:
            conn.execute("UPDATE channels SET name='Merged', tvg_id='Merged', tvg_name='Merged'")
            conn.commit()
        finally:
            conn.close()
        result = await self._sync()
        return result, logical_ids, source_ids

    async def test_inferred_merge_groups_are_independent_without_sync_result(self):
        subscriptions = {
            'alpha-a': await self._subscription('alpha-a', [self._channel('Alpha One', 'https://stream.example/alpha-a')]),
            'alpha-b': await self._subscription('alpha-b', [self._channel('Alpha Two', 'https://stream.example/alpha-b')]),
            'beta-a': await self._subscription('beta-a', [self._channel('Beta One', 'https://stream.example/beta-a')]),
            'beta-b': await self._subscription('beta-b', [self._channel('Beta Two', 'https://stream.example/beta-b')]),
        }
        await self._sync()
        conn = self._connect()
        try:
            logical_rows = conn.execute(
                'SELECT id, canonical_key FROM iptv_logical_channels ORDER BY canonical_key'
            ).fetchall()
            logical_ids = {row['canonical_key']: row['id'] for row in logical_rows}
            conn.execute(
                "UPDATE channels SET name='Merged Alpha', tvg_id='Merged Alpha', tvg_name='Merged Alpha' WHERE subscription_id IN (?, ?)",
                (subscriptions['alpha-a'], subscriptions['alpha-b']),
            )
            conn.execute(
                "UPDATE channels SET name='Merged Beta', tvg_id='Merged Beta', tvg_name='Merged Beta' WHERE subscription_id IN (?, ?)",
                (subscriptions['beta-a'], subscriptions['beta-b']),
            )
            conn.commit()
        finally:
            conn.close()

        alpha_a = logical_ids['alphaone']
        alpha_b = logical_ids['alphatwo']
        beta_a = logical_ids['betaone']
        beta_b = logical_ids['betatwo']
        alpha_source_a = await self._source('inferred-alpha-a', 'EPG-ALPHA-A')
        alpha_source_b = await self._source('inferred-alpha-b', 'EPG-ALPHA-B')
        beta_source = await self._source('inferred-beta', 'EPG-BETA')
        await self._binding(alpha_a, alpha_source_a, 'EPG-ALPHA-A')
        await self._binding(alpha_b, alpha_source_b, 'EPG-ALPHA-B')
        await self._binding(beta_a, beta_source, 'EPG-BETA')
        await self._binding(beta_b, beta_source, 'EPG-BETA')

        result = await self._sync()
        self.assertEqual(len(result['merge_conflicts']), 2)

        preview = await self.bindings.preview_epg_binding_reconciliation()
        merge_items = [item for item in preview['items'] if item.get('inferred')]
        self.assertEqual(preview['counts']['merge_conflicting_targets'], 1)
        self.assertEqual(preview['counts']['merge_same_target'], 1)
        self.assertEqual(len(merge_items), 2)
        by_ids = {frozenset(item['logical_channel_ids']): item for item in merge_items}
        self.assertEqual(
            by_ids[frozenset((alpha_a, alpha_b))]['category'],
            'merge_conflicting_targets',
        )
        self.assertEqual(
            by_ids[frozenset((beta_a, beta_b))]['category'],
            'merge_same_target',
        )
        self.assertEqual(
            by_ids[frozenset((alpha_a, alpha_b))]['logical_channel_ids'],
            sorted((alpha_a, alpha_b)),
        )
        self.assertEqual(
            by_ids[frozenset((beta_a, beta_b))]['logical_channel_ids'],
            sorted((beta_a, beta_b)),
        )

    async def test_merge_without_binding_is_reported(self):
        result, _, _ = await self._prepare_merge()
        preview = await self.bindings.preview_epg_binding_reconciliation(result)
        self.assertEqual(preview['counts']['merge_no_binding'], 1)
        self.assertEqual(preview['counts']['merge_single_binding'], 0)

    async def test_merge_single_binding_is_conservative(self):
        result, logical_ids, source_ids = await self._prepare_merge(bindings=(0,))
        preview = await self.bindings.preview_epg_binding_reconciliation(result)
        self.assertEqual(preview['counts']['merge_single_binding'], 1)
        self.assertEqual(preview['items'][0]['automatic_action'], 'none')
        self.assertEqual(self._binding_snapshot()[0][0], logical_ids[0])
        self.assertEqual(self._binding_snapshot()[0][1], source_ids[0])

    async def test_merge_same_target_is_safe_suggestion_but_not_apply(self):
        result, logical_ids, source_ids = await self._prepare_merge(bindings=(0, 1))
        # Both bindings point to the same composite target; this is only a
        # preview suggestion, not an instruction to rewrite either old ID.
        conn = self._connect()
        try:
            conn.execute('UPDATE iptv_logical_channel_epg_bindings SET epg_source_id=?, epg_channel_id=?', (source_ids[0], 'EPG-0'))
            conn.commit()
        finally:
            conn.close()
        before = self._binding_snapshot()
        preview = await self.bindings.preview_epg_binding_reconciliation(result)
        self.assertEqual(preview['counts']['merge_same_target'], 1)
        self.assertTrue(preview['items'][0]['safe_same_target'])
        self.assertEqual(self._binding_snapshot(), before)

    async def test_merge_different_target_is_conflict(self):
        result, _, _ = await self._prepare_merge(bindings=(0, 1))
        preview = await self.bindings.preview_epg_binding_reconciliation(result)
        self.assertEqual(preview['counts']['merge_conflicting_targets'], 1)

    async def test_merge_locked_different_target_is_locked_conflict(self):
        result, _, _ = await self._prepare_merge(bindings=(0, 1), locked=(1,))
        preview = await self.bindings.preview_epg_binding_reconciliation(result)
        self.assertEqual(preview['counts']['locked_conflict'], 1)

    async def test_preview_order_is_stable_and_read_only(self):
        result, _, _ = await self._prepare_merge(bindings=(0, 1))
        before = self._binding_snapshot()
        reverse = dict(result)
        reverse['merge_conflicts'] = [dict(result['merge_conflicts'][0], logical_channel_ids=list(reversed(result['merge_conflicts'][0]['logical_channel_ids'])))]
        first = await self.bindings.preview_epg_binding_reconciliation(result)
        second = await self.bindings.preview_epg_binding_reconciliation(reverse)
        self.assertEqual(first, second)
        self.assertEqual(self._binding_snapshot(), before)


if __name__ == '__main__':
    unittest.main()
