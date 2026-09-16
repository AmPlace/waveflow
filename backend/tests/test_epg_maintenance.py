import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock


def _clear_modules():
    for name in (
        'epg_maintenance', 'epg_bindings', 'epg_match_shadow',
        'iptv_logical_gc', 'iptv_channels', 'database',
    ):
        sys.modules.pop(name, None)


class EpgMaintenanceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get('WAVEFLOW_DB_PATH')
        os.environ['WAVEFLOW_DB_PATH'] = os.path.join(self.tmpdir.name, 'waveflow.db')
        _clear_modules()
        self.db = importlib.import_module('database')
        self.maintenance = importlib.import_module('epg_maintenance')
        await self.db.initialize()

    async def asyncTearDown(self):
        if self.old_db_path is None:
            os.environ.pop('WAVEFLOW_DB_PATH', None)
        else:
            os.environ['WAVEFLOW_DB_PATH'] = self.old_db_path
        _clear_modules()
        self.tmpdir.cleanup()

    async def test_success_runs_projection_gc_shadow_and_apply(self):
        calls = []
        sync = mock.AsyncMock(side_effect=lambda: calls.append('sync') or {'projection_mismatch_count': 0})
        gc = mock.AsyncMock(side_effect=lambda: calls.append('gc') or {'deleted_count': 0})
        shadow = mock.AsyncMock(side_effect=lambda: calls.append('shadow') or {'run_id': 'run-1', 'status': 'success'})
        apply = mock.AsyncMock(side_effect=lambda *_: calls.append('apply') or {'applied_count': 2})
        with mock.patch.object(self.maintenance.iptv_channels, 'sync_iptv_logical_channels', sync), \
             mock.patch.object(self.maintenance.iptv_logical_gc, 'garbage_collect_iptv_logical_channels', gc), \
             mock.patch.object(self.maintenance.epg_match_shadow, 'run_epg_match_shadow', shadow), \
             mock.patch.object(self.maintenance.epg_match_shadow, 'apply_epg_match_shadow_run', apply):
            result = await self.maintenance.run_epg_binding_maintenance(trigger='test')

        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['apply']['applied_count'], 2)
        sync.assert_awaited_once_with()
        gc.assert_awaited_once_with()
        shadow.assert_awaited_once_with()
        apply.assert_awaited_once_with('run-1')
        self.assertEqual(calls, ['sync', 'gc', 'shadow', 'apply'])

    async def test_maintenance_failure_isolated_and_can_retry(self):
        sync = mock.AsyncMock(side_effect=[RuntimeError('temporary'), {'projection_mismatch_count': 0}])
        gc = mock.AsyncMock(return_value={'deleted_count': 0})
        shadow = mock.AsyncMock(return_value={'run_id': 'run-2', 'status': 'success'})
        apply = mock.AsyncMock(return_value={'applied_count': 0})
        with mock.patch.object(self.maintenance.iptv_channels, 'sync_iptv_logical_channels', sync), \
             mock.patch.object(self.maintenance.iptv_logical_gc, 'garbage_collect_iptv_logical_channels', gc), \
             mock.patch.object(self.maintenance.epg_match_shadow, 'run_epg_match_shadow', shadow), \
             mock.patch.object(self.maintenance.epg_match_shadow, 'apply_epg_match_shadow_run', apply):
            failed = await self.maintenance.run_epg_binding_maintenance(trigger='failure')
            retried = await self.maintenance.run_epg_binding_maintenance(trigger='retry')

        self.assertEqual(failed['status'], 'failed')
        self.assertIn('RuntimeError', failed['error'])
        self.assertEqual(retried['status'], 'success')
        self.assertEqual(retried['apply']['applied_count'], 0)
        self.assertEqual(sync.await_count, 2)
        shadow.assert_awaited_once_with()
        apply.assert_awaited_once_with('run-2')

    async def test_non_success_shadow_run_is_not_applied(self):
        shadow = mock.AsyncMock(return_value={'run_id': 'run-partial', 'status': 'partial'})
        gc = mock.AsyncMock(return_value={'deleted_count': 0})
        apply = mock.AsyncMock()
        with mock.patch.object(self.maintenance.iptv_channels, 'sync_iptv_logical_channels', mock.AsyncMock()), \
             mock.patch.object(self.maintenance.iptv_logical_gc, 'garbage_collect_iptv_logical_channels', gc), \
             mock.patch.object(self.maintenance.epg_match_shadow, 'run_epg_match_shadow', shadow), \
             mock.patch.object(self.maintenance.epg_match_shadow, 'apply_epg_match_shadow_run', apply):
            result = await self.maintenance.run_epg_binding_maintenance()

        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['apply']['skipped'])
        apply.assert_not_awaited()

    async def test_runs_are_serialized(self):
        active = 0
        maximum = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def sync():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            await release.wait()
            active -= 1
            return {'projection_mismatch_count': 0}

        common = {
            'sync_iptv_logical_channels': sync,
            'run_epg_match_shadow': mock.AsyncMock(return_value={'run_id': 'run', 'status': 'partial'}),
        }
        with mock.patch.object(self.maintenance.iptv_channels, 'sync_iptv_logical_channels', side_effect=common['sync_iptv_logical_channels']), \
             mock.patch.object(self.maintenance.epg_match_shadow, 'run_epg_match_shadow', common['run_epg_match_shadow']):
            first = asyncio.create_task(self.maintenance.run_epg_binding_maintenance(trigger='one'))
            await entered.wait()
            second = asyncio.create_task(self.maintenance.run_epg_binding_maintenance(trigger='two'))
            await asyncio.sleep(0)
            self.assertEqual(maximum, 1)
            release.set()
            await asyncio.gather(first, second)

        self.assertEqual(maximum, 1)

    async def test_production_refresh_module_no_longer_calls_legacy_matcher(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'epg.py'), encoding='utf-8') as handle:
            source = handle.read()
        refresh_body = source[source.index('async def refresh_epg_sources'):]
        self.assertNotIn('await run_epg_matching()', refresh_body)

    async def test_normal_production_modules_do_not_reference_legacy_matcher(self):
        for relative in ('epg.py', 'main.py', 'market.py'):
            with self.subTest(relative=relative):
                with open(os.path.join(os.path.dirname(__file__), '..', relative), encoding='utf-8') as handle:
                    source = handle.read()
                self.assertNotIn('run_epg_matching', source)

    async def test_failed_maintenance_is_reported_without_raising(self):
        sync = mock.AsyncMock(return_value={'projection_mismatch_count': 0})
        shadow = mock.AsyncMock(return_value={'run_id': 'run-failed', 'status': 'success'})
        apply = mock.AsyncMock(return_value={'rollback': True, 'error': 'write failed'})
        with mock.patch.object(self.maintenance.iptv_channels, 'sync_iptv_logical_channels', sync), \
             mock.patch.object(self.maintenance.epg_match_shadow, 'run_epg_match_shadow', shadow), \
             mock.patch.object(self.maintenance.epg_match_shadow, 'apply_epg_match_shadow_run', apply):
            result = await self.maintenance.run_epg_binding_maintenance(trigger='apply-failure')

        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['apply']['rollback'])
        self.assertEqual(result['error'], 'write failed')

    async def test_subscription_lifecycle_uses_maintenance_hook(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'main.py'), encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn("await _run_channel_binding_maintenance('subscription_add')", source)
        self.assertIn("await _run_channel_binding_maintenance('subscription_refresh')", source)
        self.assertIn("await _run_channel_binding_maintenance('subscription_delete')", source)

    async def test_market_lifecycle_uses_maintenance_hook(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'market.py'), encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn("await _run_epg_binding_maintenance('market_install')", source)
        self.assertIn("await _run_epg_binding_maintenance('market_uninstall')", source)
        self.assertIn('async def _run_epg_binding_maintenance', source)
