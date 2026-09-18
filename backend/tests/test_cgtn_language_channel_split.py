"""CGTN language services must stay distinct channels.

The alias table used to map every CGTN language variant onto the main channel,
so CGTN西语 (Spanish) and CGTN阿语 (Arabic) collapsed into the English CGTN even
though CGTN English/Russian/French and the English spellings stayed separate.
"""
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest

import m3u8_parser


def _canonical(name):
    return m3u8_parser.channel_name_semantics(name)['canonical_candidate']


class CgtnAliasContractTest(unittest.TestCase):
    def test_main_channel_stays_itself(self):
        self.assertEqual(_canonical('CGTN'), 'cgtn')
        self.assertEqual(_canonical('CGTN (1080p)'), 'cgtn')

    def test_spanish_variants_share_one_identity(self):
        values = {_canonical(n) for n in ('CGTN西语', 'CGTN西班牙', 'CGTN西班牙语')}
        self.assertEqual(len(values), 1)
        self.assertNotEqual(values, {'cgtn'})

    def test_arabic_variants_share_one_identity(self):
        values = {_canonical(n) for n in ('CGTN阿语', 'CGTN阿拉伯语')}
        self.assertEqual(len(values), 1)
        self.assertNotEqual(values, {'cgtn'})

    def test_language_channels_are_distinct_from_each_other(self):
        spanish = _canonical('CGTN西语')
        arabic = _canonical('CGTN阿语')
        main = _canonical('CGTN')
        self.assertEqual(len({spanish, arabic, main}), 3)

    def test_other_language_channels_are_untouched(self):
        # These were already distinct and must stay that way.
        self.assertEqual(_canonical('CGTN英语'), 'cgtn英语')
        self.assertEqual(_canonical('CGTN俄语'), 'cgtn俄语')
        self.assertEqual(_canonical('CGTN法语'), 'cgtn法语')

    def test_english_spellings_are_untouched(self):
        # The English-named services were never merged; the fix must not change
        # them, which is what makes the contract consistent across languages.
        self.assertEqual(_canonical('CGTN Spanish'), 'cgtnspanish')
        self.assertEqual(_canonical('CGTN French'), 'cgtnfrench')


class CgtnLogicalSplitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        self.db_path = os.path.join(self.tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_DB_PATH'] = self.db_path
        for mod in ('database', 'iptv_channels', 'm3u8_parser', 'alias'):
            sys.modules.pop(mod, None)
        self.db = importlib.import_module('database')
        self.iptv_channels = importlib.import_module('iptv_channels')
        await self.db.initialize()

    async def asyncTearDown(self):
        if self.old_db_path is None:
            os.environ.pop('WAVEFLOW_DB_PATH', None)
        else:
            os.environ['WAVEFLOW_DB_PATH'] = self.old_db_path
        for mod in ('database', 'iptv_channels', 'm3u8_parser', 'alias'):
            sys.modules.pop(mod, None)
        self.tmpdir.cleanup()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def _add_channels(self, names):
        sub_id = await self.db.add_subscription('t', 'https://example.test/t.m3u')
        await self.db.add_channels_bulk(sub_id, [
            {'name': name, 'url': f'https://example.test/{index}.m3u8'}
            for index, name in enumerate(names)
        ])

    def _groups(self):
        conn = self._conn()
        rows = conn.execute(
            'SELECT lc.canonical_key AS key, GROUP_CONCAT(c.name) AS names '
            'FROM iptv_logical_channels lc '
            'LEFT JOIN iptv_logical_channel_members m ON m.logical_channel_id = lc.id '
            'LEFT JOIN channels c ON c.id = m.channel_id '
            'GROUP BY lc.id ORDER BY lc.canonical_key'
        ).fetchall()
        conn.close()
        return [(r['key'], r['names'] or '') for r in rows]

    async def test_grouping_produces_three_channels(self):
        await self._add_channels(['CGTN', 'CGTN西语', 'CGTN阿语'])
        await self.iptv_channels.sync_iptv_logical_channels()
        self.assertEqual(self._groups(), [
            ('cgtn', 'CGTN'),
            ('cgtn西班牙语', 'CGTN西语'),
            ('cgtn阿拉伯语', 'CGTN阿语'),
        ])

    async def test_synonyms_collapse_into_one_channel_each(self):
        await self._add_channels([
            'CGTN西语', 'CGTN西班牙语', 'CGTN阿语', 'CGTN阿拉伯语',
        ])
        await self.iptv_channels.sync_iptv_logical_channels()
        groups = [(key, sorted(names.split(','))) for key, names in self._groups()]
        self.assertEqual(len(groups), 2)
        by_key = dict(groups)
        self.assertEqual(by_key['cgtn西班牙语'], ['CGTN西班牙语', 'CGTN西语'])
        self.assertEqual(by_key['cgtn阿拉伯语'], ['CGTN阿拉伯语', 'CGTN阿语'])

    async def _seed_historical_wrong_state(self):
        """One merged 'cgtn' row owning all three members, plus its EPG binding."""
        await self._add_channels(['CGTN', 'CGTN西语', 'CGTN阿语'])
        conn = self._conn()
        conn.execute('DELETE FROM iptv_logical_channel_members')
        conn.execute('DELETE FROM iptv_logical_channels')
        conn.execute(
            'INSERT INTO iptv_logical_channels '
            '(id, canonical_key, display_name, status, created_at, updated_at) '
            "VALUES ('lc_old','cgtn','CGTN','active','2026-01-01','2026-01-01')")
        for (channel_id,) in conn.execute('SELECT id FROM channels').fetchall():
            conn.execute(
                'INSERT INTO iptv_logical_channel_members '
                '(logical_channel_id, channel_id, membership_reason, membership_confidence,'
                " created_at, updated_at) VALUES ('lc_old',?,'stable_identity',100,"
                "'2026-01-01','2026-01-01')", (channel_id,))
        conn.execute(
            'INSERT INTO iptv_logical_channel_epg_bindings '
            '(logical_channel_id, epg_source_id, epg_channel_id, status, match_type,'
            " confidence, locked, origin, created_at, updated_at) VALUES "
            "('lc_old',1,'epg-cgtn-main','matched','exact_tvg_name',95,0,'automatic',"
            "'2026-01-01','2026-01-01')")
        conn.commit()
        conn.close()

    async def test_sync_alone_cannot_split(self):
        # Documented on purpose: the sync records a split_conflict and refuses,
        # which is why the split migration exists.
        await self._seed_historical_wrong_state()
        result = await self.iptv_channels.sync_iptv_logical_channels()
        self.assertTrue(result['split_conflicts'])
        self.assertEqual(len([g for g in self._groups() if g[1]]), 1)

    async def test_split_migration_repairs_the_historical_state(self):
        await self._seed_historical_wrong_state()
        result = await self.db.split_logical_channels_by_canonical()
        self.assertEqual(result['split_logical_count'], 1)
        self.assertEqual(result['moved_member_count'], 2)
        self.assertEqual(self._groups(), [
            ('cgtn', 'CGTN'),
            ('cgtn西班牙语', 'CGTN西语'),
            ('cgtn阿拉伯语', 'CGTN阿语'),
        ])

    async def test_logical_binding_is_not_copied_to_language_channels(self):
        await self._seed_historical_wrong_state()
        await self.db.split_logical_channels_by_canonical()
        conn = self._conn()
        rows = conn.execute(
            'SELECT lc.canonical_key AS key, b.epg_channel_id AS epg '
            'FROM iptv_logical_channel_epg_bindings b '
            'JOIN iptv_logical_channels lc ON lc.id = b.logical_channel_id'
        ).fetchall()
        orphans = conn.execute(
            'SELECT COUNT(*) FROM iptv_logical_channel_members m '
            'WHERE m.logical_channel_id NOT IN (SELECT id FROM iptv_logical_channels)'
        ).fetchone()[0]
        conn.close()
        self.assertEqual([(r['key'], r['epg']) for r in rows],
                         [('cgtn', 'epg-cgtn-main')])
        self.assertEqual(orphans, 0)

    async def test_split_is_idempotent(self):
        await self._seed_historical_wrong_state()
        first = await self.db.split_logical_channels_by_canonical()
        self.assertEqual(first['split_logical_count'], 1)
        second = await self.db.split_logical_channels_by_canonical()
        self.assertEqual(second['split_logical_count'], 0)
        self.assertEqual(second['moved_member_count'], 0)

    async def test_already_correct_state_is_untouched(self):
        await self._add_channels(['CGTN', 'CGTN西语', 'CGTN阿语'])
        await self.iptv_channels.sync_iptv_logical_channels()
        result = await self.db.split_logical_channels_by_canonical()
        self.assertEqual(result['split_logical_count'], 0)
        self.assertEqual(len([g for g in self._groups() if g[1]]), 3)


if __name__ == '__main__':
    unittest.main()
