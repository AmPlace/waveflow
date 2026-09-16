import importlib
import os
import sqlite3
import sys
import tempfile
import unittest


def _clear_modules():
    sys.modules.pop('iptv_channels', None)
    sys.modules.pop('database', None)


class IptvLogicalChannelsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        self.db_path = os.path.join(self.tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_DB_PATH'] = self.db_path
        _clear_modules()
        self.db = importlib.import_module('database')
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

    async def _add_subscription(self, title, channels):
        sub_id = await self.db.add_subscription(title, f'https://example.test/{title}.m3u')
        await self.db.add_channels_bulk(sub_id, channels)
        return sub_id

    @staticmethod
    def _channel(name, url, **overrides):
        return {
            'name': name,
            'url': url,
            'group_name': overrides.pop('group_name', '测试'),
            'logo_url': overrides.pop('logo_url', ''),
            'tvg_id': overrides.pop('tvg_id', ''),
            'tvg_name': overrides.pop('tvg_name', ''),
            **overrides,
        }

    async def _snapshot(self):
        logical = await self.db.get_iptv_logical_channels()
        members = await self.db.get_iptv_logical_channel_members()
        return logical, members

    async def test_schema_is_created_and_initialize_is_idempotent(self):
        await self.db.initialize()
        await self.db.initialize()
        conn = self._connect()
        try:
            tables = {
                row['name']
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            logical_columns = {
                row['name'] for row in conn.execute('PRAGMA table_info(iptv_logical_channels)')
            }
            member_columns = {
                row['name'] for row in conn.execute('PRAGMA table_info(iptv_logical_channel_members)')
            }
        finally:
            conn.close()
        self.assertIn('iptv_logical_channels', tables)
        self.assertIn('iptv_logical_channel_members', tables)
        self.assertTrue({
            'id', 'canonical_key', 'display_name', 'status', 'orphaned_at',
        }.issubset(logical_columns))
        self.assertTrue({
            'logical_channel_id', 'channel_id', 'membership_reason',
            'membership_confidence', 'variant_type',
        }.issubset(member_columns))

    async def test_initial_sync_groups_raw_channels_and_matches_runtime_projection(self):
        await self._add_subscription('one', [
            self._channel('CCTV-1 综合', 'http://one.test/cctv1'),
            self._channel('湖南卫视', 'http://one.test/hunan'),
        ])
        await self._add_subscription('two', [
            self._channel('CCTV1', 'http://two.test/cctv1'),
        ])

        result = await self.iptv_channels.sync_iptv_logical_channels()
        logical, members = await self._snapshot()
        projection = await self.iptv_channels.validate_iptv_logical_channel_projection()

        self.assertEqual(result['raw_channel_count'], 3)
        self.assertEqual(result['target_group_count'], 2)
        self.assertEqual(result['created_logical_count'], 2)
        self.assertEqual(result['created_member_count'], 3)
        self.assertEqual(len(logical), 2)
        self.assertEqual(len(members), 3)
        self.assertEqual(len({row['channel_id'] for row in members}), 3)
        self.assertTrue(all(row['variant_type'] == 'unknown' for row in members))
        self.assertTrue(all(row['id'] != row['canonical_key'] for row in logical))
        self.assertEqual(projection['raw_membership_coverage'], 1.0)
        self.assertEqual(projection['projection_mismatch_count'], 0)
        self.assertEqual(projection['source_count'], 2)

    async def test_repeated_sync_is_idempotent_and_keeps_logical_ids(self):
        await self._add_subscription('one', [
            self._channel('CCTV1', 'http://one.test/cctv1'),
            self._channel('CCTV-1 综合', 'http://one.test/cctv1-b'),
        ])
        first = await self.iptv_channels.sync_iptv_logical_channels()
        first_ids = {row['id'] for row in await self.db.get_iptv_logical_channels()}
        second = await self.iptv_channels.sync_iptv_logical_channels()
        second_ids = {row['id'] for row in await self.db.get_iptv_logical_channels()}

        self.assertEqual(first['created_logical_count'], 1)
        self.assertEqual(second['created_logical_count'], 0)
        self.assertEqual(second['created_member_count'], 0)
        self.assertEqual(first_ids, second_ids)

    async def test_single_member_rename_keeps_logical_id(self):
        sub_id = await self._add_subscription('one', [
            self._channel('测试一台', 'http://one.test/a'),
        ])
        await self.iptv_channels.sync_iptv_logical_channels()
        before = (await self.db.get_iptv_logical_channels())[0]
        conn = self._connect()
        try:
            conn.execute("UPDATE channels SET name='测试新闻台' WHERE subscription_id=?", (sub_id,))
            conn.commit()
        finally:
            conn.close()

        await self.iptv_channels.sync_iptv_logical_channels()
        after = (await self.db.get_iptv_logical_channels())[0]
        self.assertEqual(after['id'], before['id'])
        self.assertNotEqual(after['canonical_key'], before['canonical_key'])
        self.assertEqual(after['display_name'], '测试新闻台')

    async def test_whole_multi_member_group_rename_keeps_logical_id(self):
        sub_one = await self._add_subscription('one', [self._channel('地方测试台', 'http://one.test/a')])
        sub_two = await self._add_subscription('two', [self._channel('地方测试台', 'http://two.test/a')])
        await self.iptv_channels.sync_iptv_logical_channels()
        before = (await self.db.get_iptv_logical_channels())[0]
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE channels SET name='地方新闻台' WHERE subscription_id IN (?, ?)",
                (sub_one, sub_two),
            )
            conn.commit()
        finally:
            conn.close()

        result = await self.iptv_channels.sync_iptv_logical_channels()
        after = (await self.db.get_iptv_logical_channels())[0]
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(result['split_conflicts'], [])
        self.assertEqual(result['merge_conflicts'], [])

    async def test_new_source_joins_existing_logical_channel(self):
        await self._add_subscription('one', [self._channel('湖南卫视', 'http://one.test/a')])
        await self.iptv_channels.sync_iptv_logical_channels()
        logical_id = (await self.db.get_iptv_logical_channels())[0]['id']
        await self._add_subscription('two', [self._channel('湖南卫视', 'http://two.test/a')])

        result = await self.iptv_channels.sync_iptv_logical_channels()
        logical, members = await self._snapshot()
        self.assertEqual([row['id'] for row in logical], [logical_id])
        self.assertEqual(len(members), 2)
        self.assertEqual(result['created_logical_count'], 0)
        self.assertEqual(result['created_member_count'], 1)

    async def test_removed_last_member_marks_logical_orphaned(self):
        sub_id = await self._add_subscription('one', [self._channel('测试台', 'http://one.test/a')])
        await self.iptv_channels.sync_iptv_logical_channels()
        logical_id = (await self.db.get_iptv_logical_channels())[0]['id']
        conn = self._connect()
        try:
            conn.execute('DELETE FROM channels WHERE subscription_id=?', (sub_id,))
            conn.commit()
        finally:
            conn.close()

        result = await self.iptv_channels.sync_iptv_logical_channels()
        logical, members = await self._snapshot()
        self.assertEqual(logical[0]['id'], logical_id)
        self.assertEqual(logical[0]['status'], 'orphaned')
        self.assertEqual(members, [])
        self.assertEqual(result['orphaned_logical_count'], 1)

    async def test_split_conflict_is_reported_without_reassigning_existing_members(self):
        sub_id = await self._add_subscription('one', [
            self._channel('共同测试台', 'http://one.test/a'),
            self._channel('共同测试台', 'http://one.test/b'),
        ])
        await self.iptv_channels.sync_iptv_logical_channels()
        before_members = await self.db.get_iptv_logical_channel_members()
        changed_channel_id = before_members[0]['channel_id']
        conn = self._connect()
        try:
            conn.execute("UPDATE channels SET name='拆分测试台' WHERE id=?", (changed_channel_id,))
            conn.commit()
        finally:
            conn.close()

        result = await self.iptv_channels.sync_iptv_logical_channels()
        logical, members = await self._snapshot()
        self.assertEqual(len(result['split_conflicts']), 1)
        self.assertEqual(logical[0]['status'], 'split_conflict')
        self.assertEqual(
            {(row['logical_channel_id'], row['channel_id']) for row in members},
            {(row['logical_channel_id'], row['channel_id']) for row in before_members},
        )
        self.assertNotIn('http://', repr(result['split_conflicts']))
        self.assertNotIn('token', repr(result['split_conflicts']).lower())

    async def test_merge_conflict_preserves_both_logical_ids(self):
        sub_id = await self._add_subscription('one', [
            self._channel('独立甲台', 'http://one.test/a'),
            self._channel('独立乙台', 'http://one.test/b'),
        ])
        await self.iptv_channels.sync_iptv_logical_channels()
        before_ids = {row['id'] for row in await self.db.get_iptv_logical_channels()}
        conn = self._connect()
        try:
            conn.execute("UPDATE channels SET name='合并测试台' WHERE subscription_id=?", (sub_id,))
            conn.commit()
        finally:
            conn.close()

        result = await self.iptv_channels.sync_iptv_logical_channels()
        logical, members = await self._snapshot()
        self.assertEqual(len(result['merge_conflicts']), 1)
        self.assertEqual({row['id'] for row in logical}, before_ids)
        self.assertTrue(all(row['status'] == 'merge_conflict' for row in logical))
        self.assertEqual(len({row['channel_id'] for row in members}), len(members))

    async def test_shadow_sync_does_not_require_legacy_mapping(self):
        await self._add_subscription('one', [self._channel('测试台', 'http://one.test/a')])
        result = await self.iptv_channels.sync_iptv_logical_channels()
        self.assertEqual(result['projection_mismatch_count'], 0)

    async def test_market_in_place_channel_refresh_keeps_logical_and_source_identity(self):
        package_id = 'test.market.logical'
        channel = self._channel(
            '市场测试台',
            'http://market.test/a',
            market_package_id=package_id,
            market_source_id='primary',
            market_source_item_id='stable-source',
        )
        subscription_id = await self.db.install_market_package_atomic(
            package_id=package_id,
            market_url='https://market.test/index.json',
            title='Market logical test',
            subscription_url='market://test.market.logical',
            channels=[channel],
            installed_version='1.0.0',
        )
        await self.iptv_channels.sync_iptv_logical_channels()
        before_logical = (await self.db.get_iptv_logical_channels())[0]['id']
        before_channel = (await self.db.get_channels(subscription_id))[0]
        source_ids = importlib.import_module('security.source_ids')
        before_source_id = source_ids.source_id_for(before_channel)

        await self.db.install_market_package_atomic(
            package_id=package_id,
            market_url='https://market.test/index.json',
            title='Market logical test',
            subscription_url='market://test.market.logical',
            channels=[channel],
            installed_version='1.1.0',
        )
        await self.iptv_channels.sync_iptv_logical_channels()
        after_channel = (await self.db.get_channels(subscription_id))[0]
        after_logical = (await self.db.get_iptv_logical_channels())[0]['id']

        self.assertEqual(after_channel['id'], before_channel['id'])
        self.assertEqual(after_logical, before_logical)
        self.assertEqual(source_ids.source_id_for(after_channel), before_source_id)

    async def test_low_level_atomic_sync_rolls_back_on_duplicate_membership(self):
        await self._add_subscription('one', [self._channel('测试台', 'http://one.test/a')])
        raw = await self.db.get_aggregated_channels()
        channel_id = raw[0]['id']
        before = await self._snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.sync_iptv_logical_channel_shadow_atomic([
                {'canonical_key': 'one', 'display_name': 'One', 'channel_ids': [channel_id, channel_id]},
            ])
        after = await self._snapshot()
        self.assertEqual(after, before)

    async def test_low_level_sync_rejects_cross_group_duplicate_without_partial_write(self):
        await self._add_subscription('one', [
            self._channel('甲台', 'http://one.test/a'),
            self._channel('乙台', 'http://one.test/b'),
        ])
        raw = await self.db.get_aggregated_channels()
        first_id, second_id = (row['id'] for row in raw)
        before = await self._snapshot()
        with self.assertRaisesRegex(ValueError, '多个 logical group'):
            await self.db.sync_iptv_logical_channel_shadow_atomic([
                {'canonical_key': 'one', 'display_name': 'One', 'channel_ids': [first_id]},
                {'canonical_key': 'two', 'display_name': 'Two', 'channel_ids': [first_id, second_id]},
            ])
        self.assertEqual(await self._snapshot(), before)

    async def test_low_level_empty_projection_detaches_members_and_orphans_logical(self):
        await self._add_subscription('one', [self._channel('测试台', 'http://one.test/a')])
        await self.iptv_channels.sync_iptv_logical_channels()
        logical_id = (await self.db.get_iptv_logical_channels())[0]['id']

        result = await self.db.sync_iptv_logical_channel_shadow_atomic([])
        logical, members = await self._snapshot()
        self.assertEqual(result['removed_member_count'], 1)
        self.assertEqual(members, [])
        self.assertEqual(logical[0]['id'], logical_id)
        self.assertEqual(logical[0]['status'], 'orphaned')


if __name__ == '__main__':
    unittest.main()
