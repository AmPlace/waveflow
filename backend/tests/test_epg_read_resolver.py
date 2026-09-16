import asyncio
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock


def _clear_modules():
    for name in ('database', 'epg_read_resolver', 'epg_bindings', 'main', 'iptv_channels'):
        sys.modules.pop(name, None)


class EpgReadResolverTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        self.db_path = os.path.join(self.tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_DB_PATH'] = self.db_path
        _clear_modules()
        self.db = importlib.import_module('database')
        self.resolver = importlib.import_module('epg_read_resolver')
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

    async def _source(self, channel_id='CCTV1', status='success', enabled=1):
        source_id = await self.db.add_epg_source('Source', f'https://example.test/{channel_id}.xml')
        source = await self.db.get_epg_source(source_id)
        await self.db.replace_epg_dataset_atomic(
            source_id,
            source['revision'],
            [{'channel_id': channel_id, 'display_names': '["Channel"]', 'normalized_names': '["channel"]'}],
            [],
            stats={
                'channel_count': 1,
                'programme_count': 0,
                'data_start_at': '',
                'data_end_at': '',
                'finished_at': '2026-08-06T00:00:00+00:00',
            },
        )
        conn = self._connect()
        try:
            conn.execute('UPDATE epg_sources SET last_status=?, enabled=? WHERE id=?', (status, enabled, source_id))
            conn.commit()
        finally:
            conn.close()
        return source_id

    @staticmethod
    def _logical(logical_id, key='demo', status='active'):
        now = '2026-08-06T00:00:00+00:00'
        return {'id': logical_id, 'canonical_key': key, 'display_name': key, 'status': status,
                'created_at': now, 'updated_at': now}

    @staticmethod
    def _binding(logical_id, source_id, channel_id='CCTV1', **extra):
        return {
            'logical_channel_id': logical_id,
            'epg_source_id': source_id,
            'epg_channel_id': channel_id,
            'status': 'matched',
            **extra,
        }

    @staticmethod
    def _target(source_id, channel_id='CCTV1', status='success', enabled=1):
        return {'source_id': source_id, 'channel_id': channel_id,
                'source_status': status, 'source_enabled': enabled}

    async def test_bound_logical_uses_composite_target(self):
        result = self.resolver.resolve_epg_read_snapshot(
            'demo',
            logical_rows=[self._logical('logical-1')],
            binding_rows=[self._binding('logical-1', 2, 'same')],
            target_rows=[self._target(2, 'same')],
        )
        self.assertEqual(result.comparison_status, 'bound')
        self.assertEqual(result.effective_source, 'logical')
        self.assertEqual(result.effective_target.identity.source_id, 2)
        self.assertEqual(result.effective_target.identity.channel_id, 'same')

    async def test_no_epg_returns_no_target(self):
        result = self.resolver.resolve_epg_read_snapshot(
            'demo', logical_rows=[self._logical('logical-1')],
            binding_rows=[self._binding('logical-1', 1)], target_rows=[self._target(1)],
            policy_rows=[{'logical_channel_id': 'logical-1', 'mode': 'no_epg'}],
        )
        self.assertEqual(result.comparison_status, 'not_applicable')
        self.assertIsNone(result.effective_target)

    async def test_active_first_ignores_orphan_history(self):
        result = self.resolver.resolve_epg_read_snapshot(
            'demo',
            logical_rows=[self._logical('active'), self._logical('old', status='orphaned')],
            binding_rows=[self._binding('active', 1)], target_rows=[self._target(1)],
        )
        self.assertEqual(result.logical_channel_id, 'active')
        self.assertEqual(result.comparison_status, 'bound')

    async def test_multiple_active_is_ambiguous(self):
        result = self.resolver.resolve_epg_read_snapshot(
            'demo',
            logical_rows=[self._logical('one'), self._logical('two'), self._logical('old', status='orphaned')],
            binding_rows=[], target_rows=[],
        )
        self.assertEqual(result.comparison_status, 'logical_ambiguous')
        self.assertIsNone(result.effective_target)

    async def test_orphan_only_is_historical_conflict(self):
        result = self.resolver.resolve_epg_read_snapshot(
            'demo', logical_rows=[self._logical('old', status='orphaned')],
            binding_rows=[], target_rows=[],
        )
        self.assertEqual(result.comparison_status, 'logical_conflict')
        self.assertIsNone(result.effective_target)

    async def test_missing_logical_and_orphan_target_are_safe(self):
        missing = self.resolver.resolve_epg_read_snapshot(
            'missing', logical_rows=[], binding_rows=[], target_rows=[],
        )
        self.assertEqual(missing.comparison_status, 'logical_missing')
        orphan = self.resolver.resolve_epg_read_snapshot(
            'demo', logical_rows=[self._logical('logical-1')],
            binding_rows=[self._binding('logical-1', 9, 'gone')], target_rows=[],
        )
        self.assertEqual(orphan.comparison_status, 'shadow_orphan_target')
        self.assertIsNone(orphan.effective_target)

    async def test_existing_binding_reads_stale_failed_disabled_dataset(self):
        for status, enabled in (('stale', 1), ('failed', 1), ('disabled', 0)):
            result = self.resolver.resolve_epg_read_snapshot(
                'demo', logical_rows=[self._logical('logical-1')],
                binding_rows=[self._binding('logical-1', 1)],
                target_rows=[self._target(1, status=status, enabled=enabled)],
            )
            self.assertEqual(result.effective_source, 'logical')
            self.assertTrue(result.effective_target.readable)

    async def test_batch_snapshot_loaders_are_constant(self):
        bindings = importlib.import_module('epg_bindings')
        calls = {'logical': 0, 'catalog': 0, 'bindings': 0, 'policies': 0}
        original = {
            'logical': self.db.get_iptv_logical_channels,
            'catalog': self.db.list_epg_channel_catalog_rows,
            'bindings': bindings.list_epg_bindings,
            'policies': bindings.list_epg_binding_policies,
        }

        async def count(name, fn, *args):
            calls[name] += 1
            return await fn(*args)

        with (
            mock.patch.object(self.db, 'get_iptv_logical_channels', lambda *a: count('logical', original['logical'], *a)),
            mock.patch.object(self.db, 'list_epg_channel_catalog_rows', lambda *a: count('catalog', original['catalog'], *a)),
            mock.patch.object(bindings, 'list_epg_bindings', lambda *a: count('bindings', original['bindings'], *a)),
            mock.patch.object(bindings, 'list_epg_binding_policies', lambda *a: count('policies', original['policies'], *a)),
        ):
            await self.resolver.resolve_epg_read_many(['one', 'two'])
            first = dict(calls)
            await self.resolver.resolve_epg_read_many([f'key-{i}' for i in range(100)])
        self.assertEqual(first, {'logical': 1, 'catalog': 1, 'bindings': 1, 'policies': 1})
        self.assertEqual(calls, {'logical': 2, 'catalog': 2, 'bindings': 2, 'policies': 2})

    async def test_runtime_schema_has_no_legacy_table(self):
        conn = self._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='channel_epg_map'").fetchone()[0]
            self.assertEqual(count, 0)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            conn.close()

    async def test_resolver_exception_is_fail_closed(self):
        main = importlib.import_module('main')
        with mock.patch.object(self.resolver, 'resolve_epg_read', side_effect=RuntimeError('resolver failed')):
            result = await main.get_epg_programs('missing', date=datetime.now(timezone.utc).date().isoformat(), tz='UTC')
        self.assertNotIn('epg_source_id', result)
        self.assertNotIn('epg_channel_id', result)


if __name__ == '__main__':
    unittest.main()
