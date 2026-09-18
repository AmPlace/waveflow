"""Empty qualifier containers must never split one channel into two identities.

m3u8_parser used to strip a recognised qualifier (1080p, 720p, ...) but leave its
container behind, so "France 24 English" and "France 24 English (1080p)" produced
two canonical keys: ``france24english`` and ``france24english()``.  These tests
lock the fix and the migration that repairs installs synced before it.
"""
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest

import m3u8_parser


class ChannelNameEmptyContainerTest(unittest.TestCase):
    def _canonical(self, name):
        return m3u8_parser.channel_name_semantics(name)['canonical_candidate']

    def test_recognised_and_unrecognised_qualifiers_agree(self):
        # 360p/240p are not known qualities and were stripped whole; 1080p was
        # not.  Both spellings must still land on one canonical key.
        for suffix in ('(1080p)', '(720p)', '(576p)', '(480p)', '(2160p)', '(360p)', '(240p)'):
            self.assertEqual(
                self._canonical(f'France 24 English {suffix}'), 'france24english',
                f'suffix {suffix} disagrees',
            )

    def test_bracket_flavours(self):
        for suffix in ('(1080p)', '[1080p]', '{1080p}', '（1080p）', '【1080p】'):
            self.assertEqual(self._canonical(f'Hebei TV {suffix}'), 'hebeitv', suffix)

    def test_whitespace_only_container_is_dropped(self):
        self.assertEqual(self._canonical('Foo (   )'), 'foo')
        self.assertEqual(self._canonical('Foo []{}'), 'foo')

    def test_multiple_qualifiers(self):
        self.assertEqual(self._canonical('Foo (1080p) [HD]'), 'foo')
        self.assertEqual(self._canonical('Foo (720p) 线路1'), 'foo')
        self.assertEqual(self._canonical('Foo (1080p) (备用)'), 'foo')

    def test_cjk_names_are_untouched(self):
        self.assertEqual(self._canonical('湖南卫视 (1080p)'), '湖南卫视')
        self.assertEqual(self._canonical('深圳卫视 (1080p)'), '深圳卫视')
        self.assertEqual(self._canonical('浙江卫视4K'), '浙江卫视')
        self.assertEqual(self._canonical('浙江卫视 50 FPS'), '浙江卫视')

    def test_semantic_suffixes_survive(self):
        # CCTV5+ must keep its plus; stripping it would merge two channels.
        self.assertEqual(self._canonical('CCTV5+'), 'cctv5+')
        self.assertEqual(self._canonical('CCTV5+ (1080p)'), 'cctv5+')
        self.assertEqual(self._canonical('CCTV-1 综合高清'), 'cctv1')

    def test_alias_lookup_still_reachable_through_a_qualifier(self):
        # The container is now removed before the alias table is consulted, so a
        # qualified spelling reaches the same alias as the clean one.
        clean = self._canonical('TVB Jade')
        self.assertEqual(self._canonical('TVB Jade [1080p]'), clean)
        self.assertEqual(self._canonical('TVB Jade (1080p)'), clean)

    def test_non_empty_brackets_keep_their_content(self):
        # Content that is not a qualifier is meaning, not noise.
        self.assertIn('news', self._canonical('XXX (News)'))
        self.assertIn('少儿', self._canonical('XXX（少儿）'))
        self.assertIn('sports', self._canonical('Channel (Sports)'))
        self.assertIn('live', self._canonical('TV [Live]'))


class EmptyBracketMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        self.db_path = os.path.join(self.tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_DB_PATH'] = self.db_path
        for mod in ('database', 'iptv_channels'):
            sys.modules.pop(mod, None)
        self.db = importlib.import_module('database')
        await self.db.initialize()

    async def asyncTearDown(self):
        if self.old_db_path is None:
            os.environ.pop('WAVEFLOW_DB_PATH', None)
        else:
            os.environ['WAVEFLOW_DB_PATH'] = self.old_db_path
        for mod in ('database', 'iptv_channels'):
            sys.modules.pop(mod, None)
        self.tmpdir.cleanup()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def _seed(self, keys, members=None, epg=None, logo=None, locked=0):
        """Create logical rows; return {key: id}."""
        ids = {}
        conn = self._conn()
        now = '2026-01-01T00:00:00+00:00'
        for index, key in enumerate(keys):
            logical_id = f'lc_{index}'
            conn.execute(
                'INSERT INTO iptv_logical_channels '
                '(id, canonical_key, display_name, status, created_at, updated_at) '
                'VALUES (?,?,?,?,?,?)',
                (logical_id, key, key, 'active', now, now),
            )
            ids[key] = logical_id
        for key, channel_ids in (members or {}).items():
            for channel_id in channel_ids:
                conn.execute(
                    'INSERT INTO iptv_logical_channel_members '
                    '(logical_channel_id, channel_id, membership_reason, membership_confidence, '
                    ' created_at, updated_at) VALUES (?,?,?,?,?,?)',
                    (ids[key], channel_id, 'test', 100, now, now),
                )
        for key, epg_channel in (epg or {}).items():
            conn.execute(
                'INSERT INTO iptv_logical_channel_epg_bindings '
                '(logical_channel_id, epg_source_id, epg_channel_id, status, match_type, '
                ' confidence, locked, origin, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (ids[key], 1, epg_channel, 'matched', 'exact_tvg_name', 95,
                 # ``locked`` applies to the damaged row, which is the one a user
                 # would have pinned before the identity was split.
                 locked if key.endswith(('()', '[]', '{}')) else 0, 'automatic', now, now),
            )
        for key, asset in (logo or {}).items():
            conn.execute(
                'INSERT INTO logical_channel_logo_bindings '
                '(logical_channel_id, package_id, asset_id, binding_type, match_type, '
                ' match_key, priority, package_version, enabled, lifecycle_state, created_at, updated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (ids[key], 'pkg', asset, 'logo_pack', 'exact', key, 10, '1', 1, 'active', now, now),
            )
        conn.commit()
        conn.close()
        return ids

    def _keys(self):
        conn = self._conn()
        rows = [r[0] for r in conn.execute('SELECT canonical_key FROM iptv_logical_channels')]
        conn.close()
        return rows

    async def test_rename_in_place_when_no_clean_row_exists(self):
        await self._seed(['foo()'], members={'foo()': [1, 2]})
        result = await self.db.migrate_empty_bracket_logical_channels()
        self.assertEqual(result['renamed_in_place_count'], 1)
        self.assertEqual(result['merged_count'], 0)
        self.assertEqual(self._keys(), ['foo'])
        conn = self._conn()
        members = conn.execute(
            'SELECT channel_id FROM iptv_logical_channel_members ORDER BY channel_id'
        ).fetchall()
        conn.close()
        self.assertEqual([m[0] for m in members], [1, 2])

    async def test_merge_into_existing_clean_row(self):
        await self._seed(['foo', 'foo()'], members={'foo': [1], 'foo()': [2]})
        result = await self.db.migrate_empty_bracket_logical_channels()
        self.assertEqual(result['merged_count'], 1)
        self.assertEqual(self._keys(), ['foo'])
        conn = self._conn()
        members = conn.execute(
            'SELECT channel_id FROM iptv_logical_channel_members ORDER BY channel_id'
        ).fetchall()
        conn.close()
        self.assertEqual([m[0] for m in members], [1, 2], 'members must be deduplicated and kept')

    async def test_all_members_are_preserved_across_the_merge(self):
        # iptv_logical_channel_members.channel_id is globally UNIQUE, so the two
        # rows can never share a member; the merge must keep every one of them.
        await self._seed(['foo', 'foo()'], members={'foo': [1, 2], 'foo()': [3, 4]})
        await self.db.migrate_empty_bracket_logical_channels()
        conn = self._conn()
        members = conn.execute(
            'SELECT channel_id FROM iptv_logical_channel_members ORDER BY channel_id'
        ).fetchall()
        logical = conn.execute(
            'SELECT COUNT(*) FROM iptv_logical_channel_members '
            'WHERE logical_channel_id NOT IN (SELECT id FROM iptv_logical_channels)'
        ).fetchone()[0]
        conn.close()
        self.assertEqual([m[0] for m in members], [1, 2, 3, 4])
        self.assertEqual(logical, 0, 'no member may be left pointing at a deleted row')

    async def test_epg_binding_is_preserved(self):
        await self._seed(['foo', 'foo()'], members={'foo': [1], 'foo()': [2]},
                         epg={'foo()': 'epg-9'})
        await self.db.migrate_empty_bracket_logical_channels()
        conn = self._conn()
        row = conn.execute(
            'SELECT epg_channel_id FROM iptv_logical_channel_epg_bindings'
        ).fetchone()
        count = conn.execute(
            'SELECT COUNT(*) FROM iptv_logical_channel_epg_bindings'
        ).fetchone()[0]
        conn.close()
        self.assertEqual(row[0], 'epg-9')
        self.assertEqual(count, 1)

    async def test_logo_binding_is_preserved(self):
        await self._seed(['foo', 'foo()'], members={'foo': [1], 'foo()': [2]},
                         logo={'foo()': 'asset-1'})
        await self.db.migrate_empty_bracket_logical_channels()
        conn = self._conn()
        row = conn.execute('SELECT asset_id FROM logical_channel_logo_bindings').fetchone()
        conn.close()
        self.assertEqual(row[0], 'asset-1')

    async def test_locked_binding_wins_over_the_clean_row(self):
        await self._seed(['foo', 'foo()'], members={'foo': [1], 'foo()': [2]},
                         epg={'foo': 'epg-clean', 'foo()': 'epg-locked'}, locked=1)
        await self.db.migrate_empty_bracket_logical_channels()
        conn = self._conn()
        row = conn.execute(
            'SELECT epg_channel_id FROM iptv_logical_channel_epg_bindings'
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 'epg-locked', 'a user lock must survive the merge')

    async def test_migration_is_idempotent(self):
        await self._seed(['foo', 'foo()'], members={'foo': [1], 'foo()': [2]})
        first = await self.db.migrate_empty_bracket_logical_channels()
        self.assertEqual(first['merged_count'], 1)
        second = await self.db.migrate_empty_bracket_logical_channels()
        self.assertEqual(second['merged_count'], 0)
        self.assertEqual(second['renamed_in_place_count'], 0)
        self.assertEqual(second['logical_rows_removed_count'], 0)
        self.assertEqual(self._keys(), ['foo'])

    async def test_clean_database_is_untouched(self):
        await self._seed(['foo', 'bar'], members={'foo': [1], 'bar': [2]})
        result = await self.db.migrate_empty_bracket_logical_channels()
        self.assertEqual(result['merged_count'], 0)
        self.assertEqual(result['renamed_in_place_count'], 0)
        self.assertEqual(sorted(self._keys()), ['bar', 'foo'])


if __name__ == '__main__':
    unittest.main()
