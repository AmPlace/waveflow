import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


def _clear_modules():
    for name in ('epg_catalog', 'iptv_channels', 'database'):
        sys.modules.pop(name, None)


class EpgCatalogTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        self.db_path = os.path.join(self.tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_DB_PATH'] = self.db_path
        _clear_modules()
        self.db = importlib.import_module('database')
        self.catalog_module = importlib.import_module('epg_catalog')
        self.iptv_channels = importlib.import_module('iptv_channels')
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
    def _channel(channel_id, *display_names):
        return {
            'channel_id': channel_id,
            'display_names': json.dumps(display_names, ensure_ascii=False),
            'normalized_names': json.dumps(
                [name.lower() for name in display_names], ensure_ascii=False
            ),
        }

    @staticmethod
    def _stats():
        return {
            'channel_count': 1,
            'programme_count': 0,
            'data_start_at': '2026-08-06T00:00:00+00:00',
            'data_end_at': '2026-08-06T01:00:00+00:00',
            'finished_at': '2026-08-06T00:05:00+00:00',
        }

    async def _seed_source(self, name, url, channel_id='CCTV1', *display_names):
        source_id = await self.db.add_epg_source(name, url)
        source = await self.db.get_epg_source(source_id)
        await self.db.replace_epg_dataset_atomic(
            source_id,
            source['revision'],
            [self._channel(channel_id, *(display_names or (channel_id,)))],
            [],
            stats=self._stats(),
        )
        return source_id

    async def _add_subscription(self, title, channels):
        sub_id = await self.db.add_subscription(title, f'https://example.test/{title}.m3u')
        await self.db.add_channels_bulk(sub_id, channels)
        return sub_id

    @staticmethod
    def _raw_channel(name, url, tvg_id='', tvg_name=''):
        return {
            'name': name,
            'url': url,
            'group_name': '测试',
            'logo_url': '',
            'tvg_id': tvg_id,
            'tvg_name': tvg_name,
        }

    async def test_identity_is_composite_hashable_comparable_and_serializable(self):
        identity = self.catalog_module.EpgChannelIdentity(2, 'CCTV1')
        same = self.catalog_module.EpgChannelIdentity(2, 'CCTV1')
        other_source = self.catalog_module.EpgChannelIdentity(3, 'CCTV1')
        self.assertEqual(identity, same)
        self.assertEqual(hash(identity), hash(same))
        self.assertLess(identity, other_source)
        self.assertEqual(identity.as_dict(), {'source_id': 2, 'channel_id': 'CCTV1'})
        self.assertNotEqual(identity, self.catalog_module.EpgChannelIdentity(2, 'CCTV2'))

    async def test_two_sources_same_raw_id_are_retained_as_multimap_and_safe_metadata(self):
        source_a = await self._seed_source(
            'Source A', 'https://secret-a.example/epg.xml', 'CCTV1', '央视综合'
        )
        source_b = await self._seed_source(
            'Source B', 'https://secret-b.example/epg.xml', 'CCTV1', 'CCTV One International'
        )
        catalog = await self.catalog_module.build_epg_channel_catalog()

        identities = catalog.lookup_raw_channel_id('CCTV1')
        self.assertEqual(
            identities,
            (
                self.catalog_module.EpgChannelIdentity(source_a, 'CCTV1'),
                self.catalog_module.EpgChannelIdentity(source_b, 'CCTV1'),
            ),
        )
        self.assertEqual(catalog.get_by_identity(source_a, 'CCTV1').display_names, ('央视综合',))
        self.assertEqual(
            catalog.get_by_identity(source_b, 'CCTV1').display_names,
            ('CCTV One International',),
        )
        self.assertEqual(catalog.lookup_exact_display_name('央视综合'), identities[:1])
        self.assertEqual(catalog.lookup_exact_display_name('CCTV One International'), identities[1:])
        self.assertEqual(catalog.diagnostics.cross_source_collision_count, 1)
        self.assertEqual(catalog.diagnostics.duplicate_raw_channel_id_count, 1)
        self.assertEqual(catalog.diagnostics.duplicate_normalized_channel_id_count, 1)
        self.assertNotIn('url', catalog.get_by_identity(source_a, 'CCTV1').as_dict())
        self.assertNotIn('secret-a.example', repr(catalog.diagnostic_dict()))
        self.assertNotIn('secret-b.example', repr(catalog.diagnostic_dict()))

    async def test_raw_normalized_and_display_indexes_are_multimaps_and_order_independent(self):
        rows = [
            {
                'source_id': 20,
                'source_name': 'B',
                'source_enabled': 1,
                'source_status': 'success',
                'channel_id': 'CCTV1',
                'display_names': '["央视综合"]',
                'normalized_names': '["cctv1"]',
            },
            {
                'source_id': 10,
                'source_name': 'A',
                'source_enabled': 1,
                'source_status': 'success',
                'channel_id': 'CCTV1',
                'display_names': '["央视综合"]',
                'normalized_names': '["cctv1"]',
            },
        ]
        first = self.catalog_module.build_epg_channel_catalog_from_rows(rows)
        second = self.catalog_module.build_epg_channel_catalog_from_rows(reversed(rows))
        expected = (
            self.catalog_module.EpgChannelIdentity(10, 'CCTV1'),
            self.catalog_module.EpgChannelIdentity(20, 'CCTV1'),
        )
        self.assertEqual(first.entries, second.entries)
        self.assertEqual(first.lookup_raw_channel_id('CCTV1'), expected)
        self.assertEqual(first.lookup_normalized_channel_id('cctv1'), expected)
        self.assertEqual(first.lookup_exact_display_name('央视综合'), expected)
        self.assertEqual(first.lookup_normalized_display_name('cctv1'), expected)
        self.assertEqual(first.diagnostics.duplicate_display_name_count, 1)

    async def test_exact_identity_lookup_and_missing_identity_never_guess_other_source(self):
        source_a = await self._seed_source('A', 'https://a.example/epg.xml')
        source_b = await self._seed_source('B', 'https://b.example/epg.xml')
        self.assertEqual(
            (await self.db.get_epg_channel_catalog_row(source_a, 'CCTV1'))['source_id'],
            source_a,
        )
        self.assertTrue(await self.db.epg_channel_identity_exists(source_b, 'CCTV1'))
        self.assertFalse(await self.db.epg_channel_identity_exists(99999, 'CCTV1'))
        self.assertIsNone(await self.db.get_epg_channel_catalog_row(99999, 'CCTV1'))
        catalog = await self.catalog_module.build_epg_channel_catalog()
        self.assertIsNone(catalog.get_by_identity(99999, 'CCTV1'))
        self.assertEqual(catalog.get_by_identity(source_a, 'CCTV1').source_name, 'A')
        self.assertEqual(catalog.get_by_identity(source_b, 'CCTV1').source_name, 'B')

    async def test_delete_one_source_does_not_remove_other_source_identity(self):
        source_a = await self._seed_source('A', 'https://a.example/epg.xml')
        source_b = await self._seed_source('B', 'https://b.example/epg.xml')
        await self.db.delete_epg_source(source_a)
        catalog = await self.catalog_module.build_epg_channel_catalog()
        self.assertIsNone(catalog.get_by_identity(source_a, 'CCTV1'))
        self.assertIsNotNone(catalog.get_by_identity(source_b, 'CCTV1'))
        self.assertEqual(catalog.lookup_raw_channel_id('CCTV1'), (
            self.catalog_module.EpgChannelIdentity(source_b, 'CCTV1'),
        ))

    async def test_disabled_stale_and_failed_statuses_are_retained_and_usable_filter_is_explicit(self):
        disabled = await self._seed_source('Disabled', 'https://disabled.example/epg.xml')
        stale = await self._seed_source('Stale', 'https://stale.example/epg.xml', 'STALE', 'Stale')
        failed = await self._seed_source('Failed', 'https://failed.example/epg.xml', 'FAILED', 'Failed')
        await self.db.update_epg_source(disabled, enabled=0)
        stale_source = await self.db.get_epg_source(stale)
        failed_source = await self.db.get_epg_source(failed)
        self.assertTrue(await self.db.record_epg_source_refresh_failure(
            stale, stale_source['revision'], status='stale', attempted_at='2026-08-06T01:00:00+00:00', error='timeout'
        ))
        self.assertTrue(await self.db.record_epg_source_refresh_failure(
            failed, failed_source['revision'], status='failed', attempted_at='2026-08-06T01:00:00+00:00', error='parse'
        ))

        all_rows = await self.catalog_module.build_epg_channel_catalog()
        self.assertEqual(len(all_rows.entries), 3)
        self.assertEqual(all_rows.diagnostics.disabled_source_channel_count, 1)
        self.assertEqual(all_rows.diagnostics.stale_source_channel_count, 1)
        self.assertEqual({entry.source_status for entry in all_rows.entries}, {'disabled', 'stale', 'failed'})
        without_disabled = await self.catalog_module.build_epg_channel_catalog(include_disabled=False)
        self.assertEqual(len(without_disabled.entries), 2)
        current_success = await self.catalog_module.build_epg_channel_catalog(current_success_only=True)
        self.assertEqual(current_success.entries, ())

    async def test_atomic_refresh_and_stale_keep_current_dataset_but_revision_discard_does_not_create_identity(self):
        source_id = await self._seed_source('Source', 'https://source.example/epg.xml', 'OLD', 'Old')
        initial = await self.catalog_module.build_epg_channel_catalog()
        self.assertIsNotNone(initial.get_by_identity(source_id, 'OLD'))
        source = await self.db.get_epg_source(source_id)
        committed = await self.db.replace_epg_dataset_atomic(
            source_id,
            source['revision'],
            [self._channel('NEW', 'New')],
            [],
            stats=self._stats(),
        )
        self.assertTrue(committed['committed'])
        refreshed = await self.catalog_module.build_epg_channel_catalog()
        self.assertIsNone(refreshed.get_by_identity(source_id, 'OLD'))
        self.assertIsNotNone(refreshed.get_by_identity(source_id, 'NEW'))

        current = await self.db.get_epg_source(source_id)
        self.assertTrue(await self.db.record_epg_source_refresh_failure(
            source_id, current['revision'], status='stale', attempted_at='2026-08-06T02:00:00+00:00', error='timeout'
        ))
        stale_catalog = await self.catalog_module.build_epg_channel_catalog()
        self.assertEqual(stale_catalog.get_by_identity(source_id, 'NEW').source_status, 'stale')

        revised = await self.db.update_epg_source(source_id, url='https://source.example/revised.xml')
        discarded = await self.db.replace_epg_dataset_atomic(
            source_id,
            revised['revision'] - 1,
            [self._channel('NEVER_COMMITTED', 'Never')],
            [],
            stats=self._stats(),
        )
        self.assertFalse(discarded['committed'])
        after_discard = await self.catalog_module.build_epg_channel_catalog()
        self.assertIsNone(after_discard.get_by_identity(source_id, 'NEVER_COMMITTED'))
        self.assertIsNotNone(after_discard.get_by_identity(source_id, 'NEW'))

    async def test_catalog_snapshots_are_independent_and_do_not_mutate_mapping(self):
        source_id = await self._seed_source('Source', 'https://source.example/epg.xml')
        first = await self.catalog_module.build_epg_channel_catalog()
        second = await self.catalog_module.build_epg_channel_catalog()
        self.assertIsNot(first, second)
        self.assertIsNot(first.entries, second.entries)
        self.assertIsNot(first.by_identity, second.by_identity)
        self.assertEqual(first.entries, second.entries)
        self.assertEqual(first.get_by_identity(source_id, 'CCTV1').as_dict(), second.get_by_identity(source_id, 'CCTV1').as_dict())
        with self.assertRaises(TypeError):
            first.by_identity[self.catalog_module.EpgChannelIdentity(source_id, 'x')] = None

    async def test_logical_hints_aggregate_tvg_ids_and_never_include_urls(self):
        await self._add_subscription('hint', [
            self._raw_channel('测试台', 'http://secret-one.test/live', 'TVG-A', '测试台'),
            self._raw_channel('测试台', 'http://secret-two.test/live', 'TVG-B', '测试台国际'),
        ])
        await self.iptv_channels.sync_iptv_logical_channels()
        hints = await self.iptv_channels.get_iptv_logical_channel_hints()
        self.assertEqual(len(hints), 1)
        hint = hints[0]
        self.assertEqual(hint['raw_tvg_ids'], ['TVG-A', 'TVG-B'])
        self.assertEqual(hint['raw_tvg_names'], ['测试台', '测试台国际'])
        self.assertEqual(len(hint['member_channel_ids']), 2)
        self.assertTrue(hint['has_conflicting_tvg_ids'])
        self.assertFalse(hint['has_membership_conflict'])
        self.assertNotIn('url', hint)
        self.assertNotIn('secret-one.test', repr(hint))
        self.assertNotIn('secret-two.test', repr(hint))

    async def test_logical_conflict_hint_is_not_a_binding(self):
        await self._add_subscription('conflict', [
            self._raw_channel('冲突台', 'http://secret.test/a', 'A'),
            self._raw_channel('冲突台', 'http://secret.test/b', 'B'),
        ])
        await self.iptv_channels.sync_iptv_logical_channels()
        logical_id = (await self.db.get_iptv_logical_channels())[0]['id']
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE iptv_logical_channels SET status='split_conflict' WHERE id=?",
                (logical_id,),
            )
            conn.commit()
        finally:
            conn.close()
        hints = await self.iptv_channels.get_iptv_logical_channel_hints(logical_id)
        self.assertEqual(hints[0]['status'], 'split_conflict')
        self.assertTrue(hints[0]['has_membership_conflict'])
        self.assertNotIn('epg_source_id', hints[0])
        self.assertNotIn('epg_channel_id', hints[0])
    async def test_catalog_does_not_switch_production_matcher_paths(self):
        await self.catalog_module.build_epg_channel_catalog()
        backend_dir = Path(__file__).resolve().parents[1]
        epg_source = (backend_dir / 'epg.py').read_text(encoding='utf-8')
        main_source = (backend_dir / 'main.py').read_text(encoding='utf-8')
        self.assertNotIn('epg_catalog', epg_source)
        # Read-only management catalog search may legitimately be routed from
        # main.  The production matcher boundary is that main does not import
        # or call the EPG-2B catalog domain directly.
        self.assertNotIn('import epg_catalog', main_source)
        self.assertNotIn('from epg_catalog', main_source)
        self.assertNotIn('channel_epg_bindings', epg_source + main_source)


if __name__ == '__main__':
    unittest.main()
