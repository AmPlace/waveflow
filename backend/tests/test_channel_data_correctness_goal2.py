import asyncio
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest


class ChannelDataCorrectnessGoal2Test(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.previous_db = os.environ.get('WAVEFLOW_DB_PATH')
        os.environ['WAVEFLOW_DB_PATH'] = os.path.join(self.tmpdir.name, 'waveflow.db')
        for name in ('database', 'iptv_channels', 'm3u8_parser'):
            sys.modules.pop(name, None)
        self.db = importlib.import_module('database')
        await self.db.initialize()

    async def asyncTearDown(self):
        if self.previous_db is None:
            os.environ.pop('WAVEFLOW_DB_PATH', None)
        else:
            os.environ['WAVEFLOW_DB_PATH'] = self.previous_db
        for name in ('database', 'iptv_channels', 'm3u8_parser'):
            sys.modules.pop(name, None)
        self.tmpdir.cleanup()

    async def test_name_candidate_preserves_structured_qualifiers(self):
        from m3u8_parser import channel_name_semantics, normalize_channel_name, source_display_label

        semantics = channel_name_semantics('【福建电信】CCTV-5体育高清测试源1080P')
        self.assertEqual(semantics['canonical_candidate'], 'cctv5')
        self.assertEqual(semantics['operator'], '福建电信')
        self.assertEqual(semantics['quality_hint'], '1080P')
        self.assertEqual(semantics['role'], 'test')
        self.assertEqual(normalize_channel_name('福建综合 1080p'), '福建综合')
        self.assertEqual(normalize_channel_name('CCTV5+'), 'cctv5+')
        self.assertEqual(normalize_channel_name('CCTV4欧洲'), 'cctv4欧洲')
        self.assertEqual(normalize_channel_name('CCTV4美洲'), 'cctv4美洲')
        self.assertEqual(
            source_display_label(
                raw_name='CCTV5电信1080P', subscription_title='福建联通综合频道包',
                source_type='hls',
            ),
            '福建联通 · 1080P',
        )

    async def test_url_only_refresh_keeps_source_row_but_changes_revision(self):
        from security.source_ids import source_id_for, source_revision_for

        subscription_id = await self.db.add_subscription('one', 'https://one.example/list.m3u')
        await self.db.add_channels_bulk(subscription_id, [{
            'name': 'CCTV5', 'url': 'https://a.example/live.m3u8',
            'group_name': '体育', 'tvg_id': 'cctv5',
        }])
        before = (await self.db.get_channels(subscription_id))[0]
        generation = await self.db.begin_subscription_refresh(subscription_id)
        await self.db.replace_subscription_channels_atomic(subscription_id, [{
            'name': 'CCTV5', 'url': 'https://b.example/live.m3u8',
            'group_name': '体育', 'tvg_id': 'cctv5',
        }], expected_generation=generation)
        after = (await self.db.get_channels(subscription_id))[0]
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(source_id_for(after), source_id_for(before))
        self.assertNotEqual(source_revision_for(after), source_revision_for(before))

    async def test_older_refresh_cannot_commit_after_newer_generation(self):
        from database import SubscriptionRefreshSuperseded

        subscription_id = await self.db.add_subscription('one', 'https://one.example/list.m3u')
        await self.db.add_channels_bulk(subscription_id, [{
            'name': 'A', 'url': 'https://a.example/1.m3u8', 'group_name': 'G',
        }])
        generation_a = await self.db.begin_subscription_refresh(subscription_id)
        generation_b = await self.db.begin_subscription_refresh(subscription_id)
        self.assertEqual({generation_a, generation_b}, {1, 2})
        await self.db.replace_subscription_channels_atomic(subscription_id, [{
            'name': 'B', 'url': 'https://b.example/2.m3u8', 'group_name': 'G',
        }], expected_generation=max(generation_a, generation_b))
        with self.assertRaises(SubscriptionRefreshSuperseded):
            await self.db.replace_subscription_channels_atomic(subscription_id, [{
                'name': 'A', 'url': 'https://a.example/1-old.m3u8', 'group_name': 'G',
            }], expected_generation=min(generation_a, generation_b))
        rows = await self.db.get_channels(subscription_id)
        self.assertEqual([row['name'] for row in rows], ['B'])

    async def test_weak_name_reconciliation_does_not_pick_one_of_multiple_sources(self):
        subscription_id = await self.db.add_subscription('one', 'https://one.example/list.m3u')
        await self.db.add_channels_bulk(subscription_id, [
            {'name': '新闻综合', 'url': 'https://a.example/live.m3u8', 'group_name': '地方'},
            {'name': '新闻综合', 'url': 'https://b.example/live.m3u8', 'group_name': '地方'},
        ])
        before_ids = {row['id'] for row in await self.db.get_channels(subscription_id)}
        await self.db.replace_subscription_channels_atomic(subscription_id, [{
            'name': '新闻综合', 'url': 'https://c.example/live.m3u8', 'group_name': '地方',
        }])
        after_ids = {row['id'] for row in await self.db.get_channels(subscription_id)}
        self.assertTrue(after_ids.isdisjoint(before_ids))

    async def test_url_only_refresh_preserves_logical_membership_and_epg_binding(self):
        subscription_id = await self.db.add_subscription('one', 'https://one.example/list.m3u')
        await self.db.add_channels_bulk(subscription_id, [{
            'name': 'CCTV5', 'url': 'https://a.example/live.m3u8',
            'group_name': '体育', 'tvg_id': 'cctv5',
        }])
        channels = importlib.import_module('iptv_channels')
        await channels.sync_iptv_logical_channels()
        logical_id = (await self.db.get_iptv_logical_channels())[0]['id']
        epg_source_id = await self.db.add_epg_source('source', 'https://epg.example/one.xml')
        conn = sqlite3.connect(os.environ['WAVEFLOW_DB_PATH'])
        try:
            now = '2026-08-16T00:00:00+00:00'
            conn.execute(
                "INSERT INTO epg_channels(source_id, channel_id, display_names, normalized_names) VALUES(?, 'cctv5', '[]', '[]')",
                (epg_source_id,),
            )
            conn.execute(
                """INSERT INTO iptv_logical_channel_epg_bindings(
                    logical_channel_id, epg_source_id, epg_channel_id,
                    status, match_type, confidence, locked, origin, created_at, updated_at
                ) VALUES(?, ?, 'cctv5', 'matched', 'test', 100, 1, 'manual', ?, ?)""",
                (logical_id, epg_source_id, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        generation = await self.db.begin_subscription_refresh(subscription_id)
        await self.db.replace_subscription_channels_atomic(subscription_id, [{
            'name': 'CCTV5', 'url': 'https://b.example/live.m3u8',
            'group_name': '体育', 'tvg_id': 'cctv5',
        }], expected_generation=generation)
        await channels.sync_iptv_logical_channels()
        self.assertEqual((await self.db.get_iptv_logical_channels())[0]['id'], logical_id)
        conn = sqlite3.connect(os.environ['WAVEFLOW_DB_PATH'])
        try:
            binding = conn.execute(
                'SELECT logical_channel_id FROM iptv_logical_channel_epg_bindings'
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(binding[0], logical_id)

    async def test_export_never_emits_obvious_credential_bearing_upstream_url(self):
        import main
        from starlette.requests import Request

        request = Request({
            'type': 'http', 'method': 'GET', 'path': '/', 'scheme': 'https',
            'query_string': b'', 'headers': [(b'host', b'waveflow.example')],
            'server': ('waveflow.example', 443), 'client': ('127.0.0.1', 1),
        })
        source = {
            'url': 'https://cdn.example/live.m3u8?token=fixture-secret',
            'source_type': 'hls', 'is_working': 1, 'enabled': True,
            'source_id': 'src_fixture', 'canonical_key': 'cctv5',
        }
        self.assertTrue(main._source_has_export_credentials(source))
        urls = main._subscription_urls_for_channel(
            {'canonical_key': 'cctv5', 'urls': [source]},
            'hybrid', request, healthy_only=True,
        )
        self.assertTrue(urls)
        self.assertTrue(all('fixture-secret' not in url for url, _ in urls))


if __name__ == '__main__':
    unittest.main()
