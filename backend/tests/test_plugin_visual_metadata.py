from __future__ import annotations

import asyncio
import unittest

from plugin_runtime import validate_visual_metadata
from plugin_runtime.errors import PluginError
from waveflow_plugin_sdk import PluginApplication, VisualMetadata


class PluginVisualContractTest(unittest.TestCase):
    def test_visual_metadata_is_bounded_and_data_only(self):
        valid = VisualMetadata(
            avatar_url="https://img.example/avatar.jpg",
            stable_cover_url="https://img.example/room.jpg",
            dynamic_cover_url="https://img.example/live.jpg",
            is_live=True, title="Room", owner_name="Owner", ttl_seconds=300,
        ).as_contract()
        self.assertEqual(validate_visual_metadata(valid)["cover_role"], "live")
        bad = dict(valid, avatar_url="file:///tmp/private")
        with self.assertRaises(PluginError):
            validate_visual_metadata(bad)
        with self.assertRaises(PluginError):
            validate_visual_metadata(dict(valid, ttl_seconds=0))
        with self.assertRaises(PluginError):
            validate_visual_metadata(dict(valid, extra={"ownership": "plugin"}))

    def test_sdk_hello_publishes_visual_contract_separately(self):
        class Provider:
            def visual_metadata(self, _reference, _context):
                return VisualMetadata(ttl_seconds=60)

        app = PluginApplication(identity="org.waveflow/fixture", version="1.0.0")
        app.register_tv("fixture", Provider()).register_tv_visual("fixture", Provider())
        hello = app.hello()
        self.assertEqual(
            next(item for item in hello["provider_contracts"] if item["contract"] == "tv_visual_provider"),
            {"contract": "tv_visual_provider", "contract_version": "1.0", "features": ["metadata"], "schemes": ["fixture"]},
        )
        self.assertIn("tv.visual_metadata", hello["capabilities"])


class SourceScopedVisualCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_source_single_flight_and_revision_isolated(self):
        from core.visual_metadata import VisualMetadataCache, empty_visual

        cache = VisualMetadataCache(fetch_timeout=1)
        calls = []

        async def fetch():
            calls.append("fetch")
            await asyncio.sleep(0.01)
            return {"avatar_url": "https://img.example/a.jpg", "cover_url": "", "ttl_seconds": 60}

        fallback = empty_visual(source_id="src_a", source_revision="rev_a")
        results = await asyncio.gather(*[
            cache.get_or_fetch(source_id="src_a", source_revision="rev_a", fallback=fallback, fetch=fetch)
            for _ in range(10)
        ])
        self.assertEqual(len(calls), 1)
        self.assertEqual({item["avatar_url"] for item in results}, {"https://img.example/a.jpg"})

        await cache.get_or_fetch(
            source_id="src_a", source_revision="rev_b", fallback=empty_visual(source_id="src_a", source_revision="rev_b"), fetch=fetch,
        )
        self.assertEqual(len(calls), 2)

    async def test_failure_returns_fallback_and_cancellation_does_not_leak(self):
        from core.visual_metadata import VisualMetadataCache, empty_visual

        cache = VisualMetadataCache(fetch_timeout=0.05)
        fallback = empty_visual(source_id="src_fail", source_revision="rev")

        async def fail():
            await asyncio.sleep(1)

        result = await cache.get_or_fetch(source_id="src_fail", source_revision="rev", fallback=fallback, fetch=fail)
        self.assertEqual(result["source_id"], "src_fail")
        self.assertFalse(cache._inflight)

    async def test_source_id_and_revision_are_structurally_scoped(self):
        from core.visual_metadata import VisualMetadataCache, empty_visual

        cache = VisualMetadataCache(fetch_timeout=1)
        calls = []

        async def fetch():
            calls.append("fetch")
            return {"avatar_url": "https://img.example/a.jpg", "ttl_seconds": 60}

        await cache.get_or_fetch(
            source_id="a:b", source_revision="c", fallback=empty_visual(), fetch=fetch,
        )
        await cache.get_or_fetch(
            source_id="a", source_revision="b:c", fallback=empty_visual(), fetch=fetch,
        )
        self.assertEqual(len(calls), 2)
