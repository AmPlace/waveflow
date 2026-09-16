import asyncio
import os
import sys
import tempfile
import types
import unittest


class SourceIdRefreshTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ['WAVEFLOW_DB_PATH'] = os.path.join(self._tmpdir.name, 'waveflow.db')
        os.environ['WAVEFLOW_PROXY_HANDLE_SECRET'] = 'source-id-refresh-test-secret-32-bytes'
        for module_name in list(sys.modules):
            if module_name == 'database' or module_name.startswith('security.source_ids'):
                del sys.modules[module_name]

    def tearDown(self):
        os.environ.pop('WAVEFLOW_DB_PATH', None)
        os.environ.pop('WAVEFLOW_PROXY_HANDLE_SECRET', None)
        self._tmpdir.cleanup()
        for module_name in list(sys.modules):
            if module_name == 'database' or module_name.startswith('security.source_ids'):
                del sys.modules[module_name]

    def test_subscription_refresh_preserves_surviving_source_ids(self):
        import database as db
        from security.source_ids import source_id_for

        async def run():
            await db.initialize()
            subscription_id = await db.add_subscription(
                title='refresh', url='https://example.test/list.m3u', channel_count=3,
            )
            initial = [
                {
                    'name': '同一频道', 'url': 'https://same.example/live.m3u8',
                    'source_type': 'hls', 'referer': 'https://a.example/',
                },
                {
                    'name': '同一频道', 'url': 'https://same.example/live.m3u8',
                    'source_type': 'hls', 'referer': 'https://b.example/',
                },
                {
                    'name': 'Adapter旧源', 'url': 'fjtv://fjzh',
                    'source_type': 'adapter', 'custom_ua': 'WaveFlow/1',
                },
            ]
            await db.add_channels_bulk(subscription_id, initial)
            before_rows = await db.get_channels(subscription_id)
            before = {
                (row['url'], row['referer'], row['custom_ua']): (row['id'], source_id_for(row))
                for row in before_rows
            }

            conn = db._connect()
            conn.execute(
                """
                UPDATE channels SET
                    probe_status='online',
                    is_working=1,
                    requires_headers=1,
                    proxy_required_hint=1,
                    adapter_title='测速解析标题',
                    youtube_video_id='probe-video-id'
                WHERE subscription_id=? AND url=?
                """,
                (subscription_id, 'fjtv://fjzh'),
            )
            conn.commit()
            conn.close()

            refreshed = [
                {
                    'name': 'Adapter新名称', 'url': 'fjtv://fjzh',
                    'source_type': 'adapter', 'custom_ua': 'WaveFlow/1',
                },
                {
                    'name': '同一频道改名', 'url': 'https://same.example/live.m3u8',
                    'source_type': 'hls', 'referer': 'https://a.example/',
                },
                {
                    'name': '新增频道', 'url': 'https://new.example/live.m3u8',
                    'source_type': 'hls',
                },
            ]
            await db.add_channels_bulk(subscription_id, refreshed)
            after_rows = await db.get_channels(subscription_id)
            after = {
                (row['url'], row['referer'], row['custom_ua']): (row['id'], source_id_for(row), row)
                for row in after_rows
            }

            adapter_key = ('fjtv://fjzh', '', 'WaveFlow/1')
            first_header_key = ('https://same.example/live.m3u8', 'https://a.example/', '')
            removed_header_key = ('https://same.example/live.m3u8', 'https://b.example/', '')
            new_key = ('https://new.example/live.m3u8', '', '')

            self.assertEqual(after[adapter_key][:2], before[adapter_key])
            self.assertEqual(after[first_header_key][:2], before[first_header_key])
            self.assertNotIn(removed_header_key, after)
            self.assertNotIn(after[new_key][1], {value[1] for value in before.values()})
            self.assertEqual(after[adapter_key][2]['probe_status'], 'online')
            self.assertEqual(after[adapter_key][2]['is_working'], 1)
            self.assertEqual(after[adapter_key][2]['requires_headers'], 1)
            self.assertEqual(after[adapter_key][2]['proxy_required_hint'], 1)
            self.assertEqual(after[adapter_key][2]['adapter_title'], '测速解析标题')
            self.assertEqual(after[adapter_key][2]['youtube_video_id'], 'probe-video-id')

            surviving_ids = {source_id_for(row) for row in after_rows}
            self.assertIn(before[adapter_key][1], surviving_ids)
            self.assertIn(before[first_header_key][1], surviving_ids)
            self.assertNotIn(before[removed_header_key][1], surviving_ids)

            from routers import media_proxy
            from adapters import AdapterResolveError
            from security.dependencies import MediaAccessContext

            adapter_source = {**after[adapter_key][2], 'source_id': before[adapter_key][1], 'enabled': True}
            hls_source = {**after[first_header_key][2], 'source_id': before[first_header_key][1], 'enabled': True}
            aggregated = [
                {'canonical_key': 'Adapter新名称', 'urls': [adapter_source]},
                {'canonical_key': '同一频道改名', 'urls': [hls_source]},
            ]
            old_main = sys.modules.get('main')
            old_serve = media_proxy._serve_iptv_source_playlist
            old_assert_safe_target = media_proxy.assert_safe_target_url
            selected = []

            async def fake_resolve(_url, _client):
                return {
                    'url': 'https://resolved.example/live.m3u8',
                    'source_type': 'hls',
                    'direct_playable': True,
                    'requires_proxy': False,
                }

            async def fake_serve(source, _canonical_key, _access):
                selected.append(source['source_id'])
                return types.SimpleNamespace(status_code=200)

            async def allow_test_target(*_args, **_kwargs):
                return None

            fake_main = types.SimpleNamespace(
                _get_aggregated_iptv_channels=lambda: asyncio.sleep(0, result=(aggregated, [])),
                _sorted_sources=lambda sources: list(sources),
                _source_type=lambda source: source.get('source_type') or 'hls',
                http_client=object(),
                AdapterResolveError=AdapterResolveError,
            )
            fake_main.app = types.SimpleNamespace(
                state=types.SimpleNamespace(provider_resolver=types.SimpleNamespace(resolve=fake_resolve)),
            )
            try:
                sys.modules['main'] = fake_main
                media_proxy._serve_iptv_source_playlist = fake_serve
                media_proxy.assert_safe_target_url = allow_test_target
                playlist_response = await media_proxy._serve_iptv_channel_playlist(
                    '同一频道改名',
                    types.SimpleNamespace(headers={}),
                    MediaAccessContext(source='anonymous'),
                    source_id=before[first_header_key][1],
                )
                resolve_response = await media_proxy.media_channel_source_resolve(
                    'Adapter新名称',
                    types.SimpleNamespace(headers={}),
                    source_id=before[adapter_key][1],
                    access=MediaAccessContext(source='anonymous'),
                )
            finally:
                media_proxy._serve_iptv_source_playlist = old_serve
                media_proxy.assert_safe_target_url = old_assert_safe_target
                if old_main is not None:
                    sys.modules['main'] = old_main
                else:
                    sys.modules.pop('main', None)

            self.assertEqual(playlist_response.status_code, 200)
            self.assertEqual(selected, [before[first_header_key][1]])
            self.assertEqual(resolve_response['source_id'], before[adapter_key][1])

            snapshot = [(row['id'], row['name'], row['url']) for row in after_rows]
            with self.assertRaises(Exception):
                await db.add_channels_bulk(subscription_id, [
                    refreshed[0],
                    {'name': None, 'url': 'https://invalid.example/live.m3u8'},
                ])
            rows_after_failure = await db.get_channels(subscription_id)
            self.assertEqual(
                [(row['id'], row['name'], row['url']) for row in rows_after_failure],
                snapshot,
            )

        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
