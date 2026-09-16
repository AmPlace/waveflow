import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx


def _clear_modules():
    for name in (
        'automation', 'database', 'epg', 'epg_preference_evidence',
        'epg_source_management', 'epg_source_model', 'epg_source_preference',
        'epg_tasks', 'epg_bindings',
    ):
        sys.modules.pop(name, None)


def xml_payload(channel_id='cctv1', *, valid=True, title='News') -> bytes:
    programme = (
        f'<programme channel="{channel_id}" start="20260806120000 +0800" stop="20260806130000 +0800"><title>{title}</title></programme>'
        if valid else
        f'<programme channel="{channel_id}" start="bad" stop="bad"><title>{title}</title></programme>'
    )
    return (
        '<?xml version="1.0"?><tv>'
        f'<channel id="{channel_id}"><display-name>{channel_id}</display-name></channel>'
        f'{programme}</tv>'
    ).encode()


class EpgRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        os.environ['WAVEFLOW_DB_PATH'] = os.path.join(self.tmpdir.name, 'waveflow.db')
        _clear_modules()
        self.db = importlib.import_module('database')
        self.epg = importlib.import_module('epg')
        self.bindings = importlib.import_module('epg_bindings')
        await self.db.initialize()

    async def asyncTearDown(self):
        if self.old_db_path is None:
            os.environ.pop('WAVEFLOW_DB_PATH', None)
        else:
            os.environ['WAVEFLOW_DB_PATH'] = self.old_db_path
        _clear_modules()
        self.tmpdir.cleanup()

    async def _source(self, name='Source', url='https://example.test/epg.xml', enabled=True):
        source_id = await self.db.add_epg_source(name, url)
        if not enabled:
            await self.db.update_epg_source(source_id, enabled=0)
        return await self.db.get_epg_source(source_id)

    async def _disable_builtin(self):
        builtin = (await self.epg.ensure_default_epg_sources())[0]
        await self.db.update_epg_source(builtin['id'], enabled=0)
        return builtin

    def _client(self, payload: bytes, *, status=200, headers=None):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            status,
            headers=headers,
            content=payload,
        ))
        return httpx.AsyncClient(transport=transport)

    async def _seed_old(self, source):
        await self.db.replace_epg_dataset_atomic(
            source['id'],
            source['revision'],
            [{
                'channel_id': 'old',
                'display_names': '["Old"]',
                'normalized_names': '["old"]',
            }],
            [{
                'channel_id': 'old',
                'start': '2026-08-05T00:00:00+00:00',
                'stop': '2026-08-05T01:00:00+00:00',
                'title': 'Old programme',
                'description': '',
            }],
            stats={
                'channel_count': 1,
                'programme_count': 1,
                'data_start_at': '2026-08-05T00:00:00+00:00',
                'data_end_at': '2026-08-05T01:00:00+00:00',
                'finished_at': '2026-08-05T00:05:00+00:00',
            },
        )
        return await self.db.get_epg_source(source['id'])

    async def test_success_returns_source_contract_and_updates_dataset(self):
        source = await self._source()
        async with self._client(xml_payload()) as client:
            result = await self.epg.refresh_epg_source(source, client)
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['usable_dataset'])
        self.assertFalse(result['stale'])
        self.assertTrue(result['cache_updated'])
        self.assertGreater(result['downloaded_bytes'], 0)
        self.assertEqual(result['channel_count'], 1)
        self.assertEqual(result['programme_count'], 1)
        self.assertEqual(result['skipped_programme_count'], 0)
        self.assertEqual(result['data_start_at'], '2026-08-06T04:00:00+00:00')
        self.assertEqual(result['data_end_at'], '2026-08-06T05:00:00+00:00')
        self.assertEqual(result['source_revision'], source['revision'])
        self.assertNotIn('?', result['requested_url'])

    async def test_empty_or_invalid_dataset_preserves_old_programmes_as_stale(self):
        source = await self._seed_old(await self._source())
        cases = [
            b'<?xml version="1.0"?><tv><channel id="x"><display-name>X</display-name></channel></tv>',
            xml_payload(valid=False),
            (
                '<?xml version="1.0"?><tv><channel id="known"><display-name>Known</display-name></channel>'
                '<programme channel="unknown" start="20260806120000 +0800" stop="20260806130000 +0800"><title>Wrong</title></programme></tv>'
            ).encode(),
        ]
        for payload in cases:
            with self.subTest(payload=payload[:40]):
                async with self._client(payload) as client:
                    result = await self.epg.refresh_epg_source(source, client)
                self.assertEqual(result['status'], 'stale')
                old = await self.db.get_epg_programs(source['id'], 'old')
                self.assertEqual([row['title'] for row in old], ['Old programme'])

    async def test_first_failure_is_failed_and_existing_dataset_failure_is_stale(self):
        fresh = await self._source('Fresh', 'https://example.test/fresh.xml')
        async with self._client(b'broken', status=500) as client:
            failed = await self.epg.refresh_epg_source(fresh, client)
        self.assertEqual(failed['status'], 'failed')
        self.assertFalse(failed['stale'])

        cached = await self._seed_old(await self._source('Cached', 'https://example.test/cached.xml'))
        async with self._client(b'broken', status=500) as client:
            stale = await self.epg.refresh_epg_source(cached, client)
        self.assertEqual(stale['status'], 'stale')
        self.assertTrue(stale['stale'])
        self.assertEqual((await self.db.get_epg_source(cached['id']))['last_success_at'], cached['last_success_at'])

    async def test_timeout_and_size_failures_preserve_cached_dataset(self):
        source = await self._seed_old(await self._source())

        def timeout_handler(request):
            raise httpx.ReadTimeout('remote timeout', request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
            timeout_result = await self.epg.refresh_epg_source(source, client)
        self.assertEqual(timeout_result['status'], 'stale')

        async with self._client(xml_payload()) as client:
            with mock.patch.object(
                self.epg,
                'download_xmltv',
                side_effect=self.epg.EpgRefreshError('EPG 解压大小超过限制'),
            ):
                size_result = await self.epg.refresh_epg_source(source, client)
        self.assertEqual(size_result['status'], 'stale')
        self.assertEqual(
            [row['title'] for row in await self.db.get_epg_programs(source['id'], 'old')],
            ['Old programme'],
        )

    async def test_partial_invalid_programmes_commit_valid_subset(self):
        source = await self._source()
        payload = (
            '<?xml version="1.0"?><tv><channel id="cctv1"><display-name>CCTV1</display-name></channel>'
            '<programme channel="cctv1" start="bad" stop="20260806130000 +0800"><title>Bad</title></programme>'
            '<programme channel="cctv1" start="20260806130000 +0800" stop="20260806140000 +0800"><title>Good</title></programme>'
            '</tv>'
        ).encode()
        async with self._client(payload) as client:
            result = await self.epg.refresh_epg_source(source, client)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['skipped_programme_count'], 1)
        self.assertEqual(
            [row['title'] for row in await self.db.get_epg_programs(source['id'], 'cctv1')],
            ['Good'],
        )

    async def test_malformed_xml_preserves_cache_and_sanitizes_urls(self):
        source = await self._source(url='https://user:secret@example.test/epg.xml?token=secret')
        source = await self._seed_old(source)
        async with self._client(b'<tv><channel>') as client:
            result = await self.epg.refresh_epg_source(source, client)
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(result['requested_url'], 'https://example.test/epg.xml')
        self.assertEqual(result['current_url'], 'https://example.test/epg.xml')
        self.assertNotIn('secret', result['error'])
        self.assertNotIn('secret', result['requested_url'])
        self.assertNotIn('secret', result['current_url'])
        self.assertNotIn(self.tmpdir.name, result['error'])

    async def test_source_results_are_independent_snapshots(self):
        source = await self._source()
        async with self._client(xml_payload(title='First')) as client:
            first = await self.epg.refresh_epg_source(source, client)
        first['status'] = 'tampered'
        async with self._client(xml_payload(title='Second')) as client:
            second = await self.epg.refresh_epg_source(source, client)
        self.assertEqual(second['status'], 'success')
        self.assertEqual(
            [row['title'] for row in await self.db.get_epg_programs(source['id'], 'cctv1')],
            ['Second'],
        )

    async def test_cancelled_source_records_cancelled_without_success(self):
        source = await self._source()
        stop_event = asyncio.Event()
        stop_event.set()
        async with self._client(xml_payload()) as client:
            with self.assertRaises(asyncio.CancelledError):
                await self.epg.refresh_epg_source(source, client, stop_event=stop_event)
        stored = await self.db.get_epg_source(source['id'])
        self.assertEqual(stored['last_status'], 'cancelled')
        self.assertEqual(stored['last_success_at'], '')

    async def test_failed_refresh_keeps_programme_and_batch_queries_readable(self):
        source = await self._source()
        now = datetime.now(timezone.utc)
        await self.db.replace_epg_dataset_atomic(
            source['id'],
            source['revision'],
            [{
                'channel_id': 'current',
                'display_names': '["Current"]',
                'normalized_names': '["current"]',
            }],
            [{
                'channel_id': 'current',
                'start': (now - timedelta(minutes=10)).isoformat(),
                'stop': (now + timedelta(minutes=50)).isoformat(),
                'title': 'Still available',
                'description': '',
            }],
            stats={
                'channel_count': 1,
                'programme_count': 1,
                'data_start_at': (now - timedelta(minutes=10)).isoformat(),
                'data_end_at': (now + timedelta(minutes=50)).isoformat(),
                'finished_at': now.isoformat(),
            },
        )
        conn = self.db._connect()
        try:
            conn.execute(
                """
                INSERT INTO iptv_logical_channels(
                    id, canonical_key, display_name, status, created_at, updated_at
                ) VALUES(?, ?, ?, 'active', ?, ?)
                """,
                (
                    'logical-current',
                    'canonical-current',
                    'Current',
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        await self.bindings.create_matched_epg_binding(
            'logical-current',
            source['id'],
            'current',
            match_type='exact',
            confidence=100,
            origin='automatic',
        )
        async with self._client(b'failed', status=500) as client:
            result = await self.epg.refresh_epg_source(source, client)
        self.assertEqual(result['status'], 'stale')
        programmes = await self.db.get_epg_programs(source['id'], 'current')
        batch = await self.db.batch_get_current_programs(['canonical-current'])
        self.assertEqual([row['title'] for row in programmes], ['Still available'])
        self.assertEqual(batch['canonical-current']['current']['title'], 'Still available')

    async def test_disabled_source_does_not_send_network_request(self):
        source = await self._source(enabled=False)
        client = mock.AsyncMock()
        result = await self.epg.refresh_epg_source(source, client)
        self.assertEqual(result['status'], 'disabled')
        self.assertFalse(result['usable_dataset'])
        client.stream.assert_not_called()

    async def test_full_refresh_skips_disabled_builtin_and_custom_without_request(self):
        builtin = await self._disable_builtin()
        source = await self._source('Disabled', 'https://example.test/disabled.xml', enabled=False)
        client = mock.AsyncMock()
        with mock.patch(
            'epg_maintenance.run_epg_binding_maintenance',
            return_value={'status': 'success'},
        ) as maintenance:
            result = await self.epg.refresh_epg_sources(client)
        self.assertEqual(result['refresh_status'], 'failed')
        self.assertEqual(result['source_results'], [])
        sources = await self.db.get_epg_sources()
        self.assertEqual({item['id'] for item in sources}, {builtin['id'], source['id']})
        self.assertTrue(all(not item['enabled'] for item in sources))
        client.stream.assert_not_called()

    async def test_url_change_delete_and_disable_discard_late_result(self):
        mutations = ('url', 'delete', 'disable')
        for index, mutation in enumerate(mutations):
            source = await self._source(
                f'Source-{mutation}',
                f'https://example.test/{index}.xml',
            )
            real_download = self.epg.download_xmltv

            async def mutate_then_download(client, url, **kwargs):
                download = await real_download(client, url, **kwargs)
                if mutation == 'url':
                    await self.db.update_epg_source(source['id'], url=f'https://example.test/{index}-new.xml')
                elif mutation == 'delete':
                    await self.db.delete_epg_source(source['id'])
                else:
                    await self.db.update_epg_source(source['id'], enabled=0)
                return download

            async with self._client(xml_payload()) as client:
                with mock.patch.object(
                    self.epg,
                    'download_xmltv',
                    side_effect=mutate_then_download,
                ):
                    result = await self.epg.refresh_epg_source(source, client)
            self.assertEqual(result['status'], 'revision_discarded')
            self.assertFalse(result['cache_updated'])

    async def test_same_source_refreshes_are_serial_but_different_sources_can_overlap(self):
        source_a = await self._source('A', 'https://example.test/a.xml')
        source_b = await self._source('B', 'https://example.test/b.xml')
        active = 0
        maximum = 0
        release = asyncio.Event()
        entered = asyncio.Event()
        real_download = self.epg.download_xmltv

        async def delayed_download(client, url, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            await release.wait()
            try:
                return await real_download(client, url, **kwargs)
            finally:
                active -= 1

        async with self._client(xml_payload()) as client:
            with mock.patch.object(
                self.epg,
                'download_xmltv',
                side_effect=delayed_download,
            ):
                same_one = asyncio.create_task(self.epg.refresh_epg_source(source_a, client))
                await entered.wait()
                same_two = asyncio.create_task(self.epg.refresh_epg_source(source_a, client))
                await asyncio.sleep(0)
                self.assertEqual(maximum, 1)
                release.set()
                await asyncio.gather(same_one, same_two)

        active = 0
        maximum = 0
        release = asyncio.Event()
        entered_count = 0
        both_entered = asyncio.Event()

        async def parallel_download(client, url, **kwargs):
            nonlocal active, maximum, entered_count
            active += 1
            entered_count += 1
            maximum = max(maximum, active)
            if entered_count == 2:
                both_entered.set()
            await release.wait()
            try:
                return await real_download(client, url, **kwargs)
            finally:
                active -= 1

        async with self._client(xml_payload()) as client:
            with mock.patch.object(
                self.epg,
                'download_xmltv',
                side_effect=parallel_download,
            ):
                one = asyncio.create_task(self.epg.refresh_epg_source(await self.db.get_epg_source(source_a['id']), client))
                two = asyncio.create_task(self.epg.refresh_epg_source(source_b, client))
                await both_entered.wait()
                self.assertEqual(maximum, 2)
                release.set()
                await asyncio.gather(one, two)

    async def test_full_refresh_reports_partial_and_matching_boundaries(self):
        await self._disable_builtin()
        await self._source('Good', 'https://example.test/good.xml')
        await self._source('Bad', 'https://example.test/bad.xml')

        def handler(request):
            if request.url.path.endswith('good.xml'):
                return httpx.Response(200, content=xml_payload())
            return httpx.Response(500, content=b'failed')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with mock.patch(
                'epg_maintenance.run_epg_binding_maintenance',
                return_value={'status': 'success'},
            ) as maintenance:
                result = await self.epg.refresh_epg_sources(client)
        self.assertEqual(result['refresh_status'], 'partial')
        self.assertEqual(result['source_result_counts']['success'], 1)
        self.assertEqual(result['source_result_counts']['failed'], 1)
        self.assertEqual(result['channel_count'], 1)
        self.assertEqual(result['programme_count'], 1)
        maintenance.assert_awaited_once_with(sync_logical=True, trigger='epg_refresh')

    async def test_all_failed_does_not_run_matching(self):
        await self._disable_builtin()
        await self._source()
        async with self._client(b'failed', status=500) as client:
            with mock.patch(
                'epg_maintenance.run_epg_binding_maintenance',
                return_value={'status': 'success'},
            ) as maintenance:
                result = await self.epg.refresh_epg_sources(client)
        self.assertEqual(result['refresh_status'], 'failed')
        maintenance.assert_not_awaited()

    async def test_matching_failure_returns_partial_after_dataset_commit(self):
        await self._disable_builtin()
        source = await self._source()
        async with self._client(xml_payload()) as client:
            with mock.patch(
                'epg_maintenance.run_epg_binding_maintenance',
                side_effect=RuntimeError('maintenance failed'),
            ):
                result = await self.epg.refresh_epg_sources(client)
        self.assertEqual(result['refresh_status'], 'partial')
        self.assertEqual(result['binding_maintenance'], None)
        self.assertIn('EPG binding maintenance', result['error'])
        self.assertEqual(len(await self.db.get_epg_programs(source['id'], 'cctv1')), 1)

    async def test_maintenance_failure_result_does_not_change_refresh_status(self):
        await self._disable_builtin()
        source = await self._source()
        async with self._client(xml_payload()) as client:
            with mock.patch(
                'epg_maintenance.run_epg_binding_maintenance',
                return_value={
                    'status': 'failed',
                    'error': 'temporary maintenance failure',
                },
            ) as maintenance:
                result = await self.epg.refresh_epg_sources(client)

        self.assertEqual(result['refresh_status'], 'partial')
        self.assertEqual(result['binding_maintenance']['status'], 'failed')
        self.assertIn('temporary maintenance failure', result['error'])
        self.assertEqual(len(await self.db.get_epg_programs(source['id'], 'cctv1')), 1)
        maintenance.assert_awaited_once_with(sync_logical=True, trigger='epg_refresh')

    async def test_maintenance_failure_is_retried_on_next_refresh_cycle(self):
        await self._disable_builtin()
        source = await self._source()
        maintenance_results = [
            {'status': 'failed', 'error': 'temporary maintenance failure'},
            {'status': 'success', 'error': ''},
        ]
        async with self._client(xml_payload()) as client:
            with mock.patch(
                'epg_maintenance.run_epg_binding_maintenance',
                side_effect=maintenance_results,
            ) as maintenance:
                first = await self.epg.refresh_epg_sources(client)
                second = await self.epg.refresh_epg_sources(client)

        self.assertEqual(first['refresh_status'], 'partial')
        self.assertEqual(second['refresh_status'], 'success')
        self.assertEqual(maintenance.await_count, 2)
        self.assertEqual(
            (await self.db.get_epg_source(source['id']))['last_status'],
            'success',
        )
        self.assertEqual(
            len(await self.db.get_epg_programs(source['id'], 'cctv1')),
            1,
        )

    async def test_stop_event_cancels_before_next_source(self):
        await self._disable_builtin()
        first = await self._source('First', 'https://example.test/first.xml')
        await self._source('Second', 'https://example.test/second.xml')
        stop_event = asyncio.Event()
        real_refresh = self.epg.refresh_epg_source

        async def stop_after_first(source, client, **kwargs):
            result = await real_refresh(source, client, **kwargs)
            if source['id'] == first['id']:
                stop_event.set()
            return result

        async with self._client(xml_payload()) as client:
            with mock.patch.object(
                self.epg,
                'refresh_epg_source',
                side_effect=stop_after_first,
            ) as refresh_one:
                with self.assertRaises(asyncio.CancelledError):
                    await self.epg.refresh_epg_sources(client, stop_event=stop_event)
        self.assertEqual(refresh_one.await_count, 1)

    def test_production_periodic_refresh_is_owned_only_by_automation(self):
        with open(os.path.join(os.path.dirname(self.epg.__file__), 'main.py'), encoding='utf-8') as fh:
            main_source = fh.read()
        self.assertNotIn('_epg_refresh_loop', main_source)
        self.assertNotIn('asyncio.create_task(_epg.refresh_epg_sources(http_client))', main_source)
        self.assertIn('create_production_automation_service(http_client)', main_source)
        self.assertIn('_run_manual_epg_refresh', main_source)


if __name__ == '__main__':
    unittest.main()
