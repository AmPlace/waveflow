import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock


def _clear_modules():
    for name in ('iptv_logical_gc', 'iptv_channels', 'database'):
        sys.modules.pop(name, None)


class IptvLogicalGcTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        self.db_path = os.path.join(self.tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_DB_PATH'] = self.db_path
        _clear_modules()
        self.db = importlib.import_module('database')
        self.channels = importlib.import_module('iptv_channels')
        self.gc = importlib.import_module('iptv_logical_gc')
        await self.db.initialize()
        self.now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

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

    def _logical(self, logical_id, *, status='orphaned', orphaned_at=None):
        created_at = (self.now - timedelta(days=60)).isoformat()
        if orphaned_at is None and status == 'orphaned':
            orphaned_at = (self.now - timedelta(days=31)).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO iptv_logical_channels(
                    id, canonical_key, display_name, status,
                    orphaned_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    logical_id, logical_id, logical_id, status,
                    orphaned_at, created_at, created_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def _source(self):
        source_id = await self.db.add_epg_source(
            'GC source', 'https://epg.example/gc.xml',
        )
        await self.db.replace_epg_channels(source_id, [{
            'channel_id': 'epg-1',
            'display_names': '["EPG 1"]',
            'normalized_names': '["epg1"]',
        }])
        return source_id

    def _binding(self, logical_id, source_id, *, origin='automatic', locked=0):
        timestamp = self.now.isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO iptv_logical_channel_epg_bindings(
                    logical_channel_id, epg_source_id, epg_channel_id,
                    status, match_type, confidence, locked, origin,
                    created_at, updated_at
                ) VALUES(?, ?, 'epg-1', 'matched', 'exact_tvg_id', 100,
                         ?, ?, ?, ?)
                """,
                (logical_id, source_id, locked, origin, timestamp, timestamp),
            )
            conn.commit()
        finally:
            conn.close()

    def _exists(self, logical_id):
        conn = self._connect()
        try:
            return conn.execute(
                'SELECT 1 FROM iptv_logical_channels WHERE id=?',
                (logical_id,),
            ).fetchone() is not None
        finally:
            conn.close()

    def _table_count(self, table):
        conn = self._connect()
        try:
            return int(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        finally:
            conn.close()

    async def _collect(self, **overrides):
        return await self.gc.garbage_collect_iptv_logical_channels(
            now=self.now,
            **overrides,
        )

    async def test_active_and_conflict_states_are_never_candidates(self):
        self._logical('active', status='active', orphaned_at=None)
        self._logical('split', status='split_conflict', orphaned_at=None)
        self._logical('merge', status='merge_conflict', orphaned_at=None)

        result = await self._collect(retention_days=0)

        self.assertEqual(result['deleted_count'], 0)
        self.assertEqual(result['retained_conflict_count'], 2)
        self.assertTrue(all(self._exists(value) for value in ('active', 'split', 'merge')))

    async def test_recent_orphan_is_retained(self):
        self._logical(
            'recent',
            orphaned_at=(self.now - timedelta(days=29)).isoformat(),
        )
        result = await self._collect()
        self.assertEqual(result['too_recent_count'], 1)
        self.assertEqual(result['deleted_count'], 0)
        self.assertTrue(self._exists('recent'))

    async def test_expired_unreferenced_orphan_is_deleted(self):
        self._logical('expired')
        result = await self._collect()
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(result['deleted_count'], 1)
        self.assertFalse(self._exists('expired'))

    async def test_unlocked_derived_bindings_are_deleted_with_orphan(self):
        source_id = await self._source()
        for origin in ('automatic', 'legacy_migrated'):
            with self.subTest(origin=origin):
                logical_id = f'derived-{origin}'
                self._logical(logical_id)
                self._binding(logical_id, source_id, origin=origin)

        result = await self._collect()

        self.assertEqual(result['deleted_count'], 2)
        self.assertEqual(result['deleted_binding_count'], 2)
        self.assertEqual(self._table_count('iptv_logical_channel_epg_bindings'), 0)

    async def test_manual_and_locked_bindings_block_gc(self):
        source_id = await self._source()
        self._logical('manual')
        self._binding('manual', source_id, origin='manual')
        self._logical('locked')
        self._binding('locked', source_id, origin='automatic', locked=1)

        result = await self._collect()

        self.assertEqual(result['deleted_count'], 0)
        self.assertEqual(result['retained_manual_count'], 1)
        self.assertEqual(result['retained_locked_count'], 1)
        self.assertTrue(self._exists('manual'))
        self.assertTrue(self._exists('locked'))

    async def test_no_epg_blocks_but_automatic_policy_does_not(self):
        self._logical('no-epg')
        self._logical('automatic-policy')
        conn = self._connect()
        try:
            for logical_id, mode in (
                ('no-epg', 'no_epg'),
                ('automatic-policy', 'automatic'),
            ):
                conn.execute(
                    """
                    INSERT INTO iptv_logical_channel_epg_policies(
                        logical_channel_id, mode, created_at, updated_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (logical_id, mode, self.now.isoformat(), self.now.isoformat()),
                )
            conn.commit()
        finally:
            conn.close()

        result = await self._collect()

        self.assertEqual(result['retained_policy_count'], 1)
        self.assertEqual(result['deleted_count'], 1)
        self.assertTrue(self._exists('no-epg'))
        self.assertFalse(self._exists('automatic-policy'))

    async def test_manual_preference_blocks_and_derived_evidence_is_cleaned(self):
        source_id = await self._source()
        self._logical('manual-preference')
        self._logical('derived-preference')
        conn = self._connect()
        try:
            now = self.now.isoformat()
            conn.execute(
                """
                INSERT INTO epg_source_preference_evidence(
                    logical_channel_id, origin, epg_source_id,
                    resolution_status, evidence_json, created_at, updated_at
                ) VALUES('manual-preference', 'manual', ?, 'manual',
                         '{"explicit":true}', ?, ?)
                """,
                (source_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO epg_source_preference_evidence(
                    logical_channel_id, origin, epg_source_id,
                    hint_fingerprint, resolution_status, evidence_json,
                    created_at, updated_at
                ) VALUES('derived-preference', 'market', ?, 'derived-1',
                         'resolved', '{}', ?, ?)
                """,
                (source_id, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        result = await self._collect()

        self.assertEqual(result['retained_manual_count'], 1)
        self.assertEqual(result['deleted_count'], 1)
        self.assertEqual(result['deleted_preference_count'], 1)
        self.assertTrue(self._exists('manual-preference'))
        self.assertFalse(self._exists('derived-preference'))

    async def test_old_schema_upgrade_adds_one_idempotent_orphan_baseline(self):
        conn = self._connect()
        try:
            conn.execute('PRAGMA foreign_keys=OFF')
            for table in (
                'iptv_logical_channel_epg_bindings',
                'iptv_logical_channel_epg_policies',
                'iptv_logical_channel_members',
                'iptv_logical_channels',
            ):
                conn.execute(f'DROP TABLE {table}')
            conn.execute(
                """
                CREATE TABLE iptv_logical_channels (
                    id TEXT PRIMARY KEY,
                    canonical_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO iptv_logical_channels(
                    id, canonical_key, display_name, status, created_at, updated_at
                ) VALUES
                    ('legacy-orphan', 'legacy-orphan', 'Legacy orphan', 'orphaned', ?, ?),
                    ('legacy-active', 'legacy-active', 'Legacy active', 'active', ?, ?)
                """,
                (
                    self.now.isoformat(), self.now.isoformat(),
                    self.now.isoformat(), self.now.isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        await self.db.initialize()
        conn = self._connect()
        try:
            columns = {
                row['name']
                for row in conn.execute('PRAGMA table_info(iptv_logical_channels)')
            }
            first = conn.execute(
                "SELECT orphaned_at FROM iptv_logical_channels WHERE id='legacy-orphan'"
            ).fetchone()['orphaned_at']
            active = conn.execute(
                "SELECT orphaned_at FROM iptv_logical_channels WHERE id='legacy-active'"
            ).fetchone()['orphaned_at']
        finally:
            conn.close()
        await self.db.initialize()
        conn = self._connect()
        try:
            second = conn.execute(
                "SELECT orphaned_at FROM iptv_logical_channels WHERE id='legacy-orphan'"
            ).fetchone()['orphaned_at']
        finally:
            conn.close()
        self.assertIn('orphaned_at', columns)
        self.assertTrue(first)
        self.assertIsNone(active)
        self.assertEqual(second, first)

    async def test_continuing_orphan_preserves_timestamp_and_reactivation_clears_it(self):
        sub_id = await self.db.add_subscription('lifecycle', 'https://example.test/lifecycle.m3u')
        channel = {'name': 'Lifecycle', 'url': 'https://stream.test/lifecycle.m3u8'}
        await self.db.add_channels_bulk(sub_id, [channel])
        await self.channels.sync_iptv_logical_channels()
        logical_id = (await self.db.get_iptv_logical_channels())[0]['id']
        conn = self._connect()
        try:
            conn.execute('DELETE FROM channels WHERE subscription_id=?', (sub_id,))
            conn.commit()
        finally:
            conn.close()
        await self.channels.sync_iptv_logical_channels()
        first = (await self.db.get_iptv_logical_channels())[0]['orphaned_at']
        await self.channels.sync_iptv_logical_channels()
        second = (await self.db.get_iptv_logical_channels())[0]['orphaned_at']
        self.assertEqual(second, first)

        await self.db.add_channels_bulk(sub_id, [channel])
        raw_id = (await self.db.get_channels(sub_id))[0]['id']
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO iptv_logical_channel_members(
                    logical_channel_id, channel_id, created_at, updated_at
                ) VALUES(?, ?, ?, ?)
                """,
                (logical_id, raw_id, self.now.isoformat(), self.now.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        await self.channels.sync_iptv_logical_channels()
        row = next(
            item for item in await self.db.get_iptv_logical_channels()
            if item['id'] == logical_id
        )
        self.assertEqual(row['status'], 'active')
        self.assertIsNone(row['orphaned_at'])

    async def test_complete_disappearance_reappearance_gets_new_identity(self):
        sub_id = await self.db.add_subscription('return', 'https://example.test/return.m3u')
        channel = {'name': 'Return', 'url': 'https://stream.test/return.m3u8'}
        await self.db.add_channels_bulk(sub_id, [channel])
        await self.channels.sync_iptv_logical_channels()
        old_id = (await self.db.get_iptv_logical_channels())[0]['id']

        await self.db.add_channels_bulk(sub_id, [])
        await self.channels.sync_iptv_logical_channels()
        await self.db.add_channels_bulk(sub_id, [channel])
        await self.channels.sync_iptv_logical_channels()
        rows = await self.db.get_iptv_logical_channels()
        active_id = next(row['id'] for row in rows if row['status'] == 'active')
        self.assertNotEqual(active_id, old_id)
        self.assertEqual(next(row['status'] for row in rows if row['id'] == old_id), 'orphaned')

    async def test_subscription_delete_orphans_before_retention_gc(self):
        sub_id = await self.db.add_subscription(
            'deleted-subscription', 'https://example.test/deleted.m3u',
        )
        await self.db.add_channels_bulk(sub_id, [{
            'name': 'Deleted subscription channel',
            'url': 'https://stream.test/deleted.m3u8',
        }])
        await self.channels.sync_iptv_logical_channels()
        logical_id = (await self.db.get_iptv_logical_channels())[0]['id']

        await self.db.delete_subscription(sub_id)
        await self.channels.sync_iptv_logical_channels()
        result = await self._collect()
        logical = (await self.db.get_iptv_logical_channels())[0]

        self.assertEqual(logical['id'], logical_id)
        self.assertEqual(logical['status'], 'orphaned')
        self.assertTrue(logical['orphaned_at'])
        self.assertEqual(result['deleted_count'], 0)

    async def test_market_update_preserves_identity_and_uninstall_only_orphans(self):
        package_id = 'gc-market-package'
        channel = {
            'name': 'GC Market',
            'url': 'https://stream.test/market.m3u8',
            'market_package_id': package_id,
            'market_source_id': 'primary',
            'market_source_item_id': 'stable-item',
        }
        await self.db.install_market_package_atomic(
            package_id=package_id,
            market_url='https://market.test/index.json',
            title='GC Market',
            subscription_url='market://gc-market-package',
            channels=[channel],
            installed_version='1.0.0',
        )
        await self.channels.sync_iptv_logical_channels()
        logical_id = (await self.db.get_iptv_logical_channels())[0]['id']

        await self.db.install_market_package_atomic(
            package_id=package_id,
            market_url='https://market.test/index.json',
            title='GC Market',
            subscription_url='market://gc-market-package',
            channels=[channel],
            installed_version='1.1.0',
        )
        await self.channels.sync_iptv_logical_channels()
        updated = await self.db.get_iptv_logical_channels()
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0]['id'], logical_id)
        self.assertEqual(updated[0]['status'], 'active')

        self.assertTrue(await self.db.uninstall_market_package_atomic(package_id))
        await self.channels.sync_iptv_logical_channels()
        result = await self._collect()
        orphan = (await self.db.get_iptv_logical_channels())[0]
        self.assertEqual(orphan['id'], logical_id)
        self.assertEqual(orphan['status'], 'orphaned')
        self.assertTrue(orphan['orphaned_at'])
        self.assertEqual(result['deleted_count'], 0)

    async def test_revalidation_prevents_delete_after_member_reappears(self):
        self._logical('revalidated')
        sub_id = await self.db.add_subscription('race', 'https://example.test/race.m3u')
        await self.db.add_channels_bulk(sub_id, [{
            'name': 'Race', 'url': 'https://stream.test/race.m3u8',
        }])
        raw_id = (await self.db.get_channels(sub_id))[0]['id']
        original = self.gc._scan_gc_candidates_sync

        def scan_then_reappear(**kwargs):
            snapshot = original(**kwargs)
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO iptv_logical_channel_members(
                        logical_channel_id, channel_id, created_at, updated_at
                    ) VALUES('revalidated', ?, ?, ?)
                    """,
                    (raw_id, self.now.isoformat(), self.now.isoformat()),
                )
                conn.commit()
            finally:
                conn.close()
            return snapshot

        with mock.patch.object(
            self.gc, '_scan_gc_candidates_sync', side_effect=scan_then_reappear,
        ):
            result = await self._collect()

        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(result['deleted_count'], 0)
        self.assertEqual(result['revalidated_out_count'], 1)
        self.assertTrue(self._exists('revalidated'))

    async def test_bounded_batches_and_repeated_gc_are_idempotent(self):
        for index in range(5):
            self._logical(f'bounded-{index}')

        first = await self._collect(batch_size=2)
        second = await self._collect(batch_size=2)
        third = await self._collect(batch_size=2)
        fourth = await self._collect(batch_size=2)

        self.assertEqual([first['deleted_count'], second['deleted_count'], third['deleted_count']], [2, 2, 1])
        self.assertTrue(first['has_more'])
        self.assertFalse(third['has_more'])
        self.assertEqual(fourth['deleted_count'], 0)

    async def test_shadow_details_are_pruned_but_run_summary_remains(self):
        self._logical('shadowed')
        conn = self._connect()
        try:
            now = self.now.isoformat()
            conn.execute(
                """
                INSERT INTO epg_match_shadow_runs(
                    run_id, status, started_at, finished_at,
                    logical_channel_count, candidate_count
                ) VALUES('run-gc', 'success', ?, ?, 1, 1)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO epg_match_shadow_decisions(
                    run_id, logical_channel_id, status, candidate_count
                ) VALUES('run-gc', 'shadowed', 'matched', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO epg_match_shadow_candidates(
                    run_id, logical_channel_id, rank,
                    epg_source_id, epg_channel_id
                ) VALUES('run-gc', 'shadowed', 1, 1, 'epg-1')
                """
            )
            conn.commit()
        finally:
            conn.close()

        result = await self._collect()

        self.assertEqual(result['deleted_shadow_decision_count'], 1)
        self.assertEqual(result['deleted_shadow_candidate_count'], 1)
        self.assertEqual(self._table_count('epg_match_shadow_runs'), 1)
        self.assertEqual(self._table_count('epg_match_shadow_decisions'), 0)
        self.assertEqual(self._table_count('epg_match_shadow_candidates'), 0)

    async def test_gc_does_not_require_legacy_mapping(self):
        self._logical('logical-map')
        await self._collect()
        self.assertEqual(self._table_count('iptv_logical_channels'), 0)

    async def test_batch_failure_rolls_back_logical_and_derived_cleanup(self):
        source_id = await self._source()
        for logical_id in ('rollback-a', 'rollback-b'):
            self._logical(logical_id)
            self._binding(logical_id, source_id, origin='automatic')
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TRIGGER fail_second_logical_delete
                BEFORE DELETE ON iptv_logical_channels
                WHEN OLD.id='rollback-b'
                BEGIN SELECT RAISE(ABORT, 'forced gc failure'); END
                """
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            await self._collect()

        self.assertTrue(self._exists('rollback-a'))
        self.assertTrue(self._exists('rollback-b'))
        self.assertEqual(self._table_count('iptv_logical_channel_epg_bindings'), 2)


if __name__ == '__main__':
    unittest.main()
