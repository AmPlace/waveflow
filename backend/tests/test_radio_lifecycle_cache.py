import asyncio
import unittest
from unittest.mock import patch

import database
from plugin_runtime import PluginError
from radio_core import RadioCatalogBridge, RadioResolver
from tests import test_radio_core_bridge as bridge_tests


class RadioLifecycleCacheTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = bridge_tests.RadioCoreBridgeTest("test_catalog_persistence_resolve_and_last_good")
        await self.fixture.asyncSetUp()
        self.runtime = self.fixture.runtime
        self.instance = self.fixture.instance
        catalog = await self.runtime.request(self.instance, "radio.catalog", {})
        await RadioCatalogBridge().refresh(
            "org.waveflow/synthetic", catalog, owned_schemes={"synthetic"}, now=100,
        )
        station = (await database.list_radio_stations(now_unix=101))[0]
        self.station_id = station["station_id"]
        self.source_id = station["sources"][0]["source_id"]
        self.resolver = RadioResolver(runtime=self.runtime, clock=lambda: 101)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def resolve(self):
        return await self.resolver.resolve_source(self.source_id, station_id=self.station_id)

    async def test_disabled_owner_cannot_reuse_warm_descriptor(self):
        await self.resolve()
        await self.runtime.disable(self.instance)
        with self.assertRaises(PluginError):
            await self.resolve()

    async def test_unhealthy_owner_cannot_reuse_warm_descriptor(self):
        await self.resolve()
        self.runtime.registry.mark_unhealthy(self.instance)
        with self.assertRaises(PluginError):
            await self.resolve()

    async def test_uninstalled_owner_cannot_reuse_warm_descriptor(self):
        await self.resolve()
        await self.runtime.uninstall(self.instance)
        with self.assertRaises(PluginError):
            await self.resolve()

    async def test_restarted_process_does_not_reuse_prior_process_descriptor(self):
        await self.resolve()
        previous = self.instance.process
        await self.runtime.disable(self.instance)
        await self.runtime.enable(self.instance)
        self.assertIsNot(self.instance.process, previous)
        with patch.object(self.runtime, "request", wraps=self.runtime.request) as request:
            await self.resolve()
            self.assertEqual(request.await_count, 1)

    async def test_programme_cache_requires_active_owner_even_after_restart(self):
        source = await database.get_radio_station_source(self.source_id, now_unix=101)
        await database.upsert_radio_programme_snapshot(
            source, {"revision": "fixture", "programmes": []}, updated_at_unix=101, ttl_seconds=60,
        )
        await self.resolver.resolve_programme(self.source_id, station_id=self.station_id)
        await self.runtime.disable(self.instance)
        for resolver in (self.resolver, RadioResolver(runtime=None, clock=lambda: 101)):
            with self.subTest(runtime=resolver.runtime is not None):
                with self.assertRaises(PluginError):
                    await resolver.resolve_programme(self.source_id, station_id=self.station_id)

    async def test_resolve_completed_after_disable_is_rejected(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed_request(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {
                "descriptor_version": "1.0", "transport": "audio_http",
                "url": "https://example.invalid/fixture.mp3", "headers": {},
                "credential_refs": [], "ttl_seconds": 60, "expires_at": None,
                "volatile_url": False, "requires_proxy": False, "warnings": [],
            }

        with patch.object(self.runtime, "request", delayed_request):
            task = asyncio.create_task(self.resolve())
            await started.wait()
            await self.runtime.disable(self.instance)
            release.set()
            with self.assertRaises(PluginError):
                await task


if __name__ == "__main__":
    unittest.main()
