from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from plugin_runtime import validate_manifest, validate_visual_metadata
from plugin_runtime.errors import PluginError
from waveflow_plugin_sdk import PluginApplication, ResolveContext, TVReference, VisualMetadata


ROOT = Path(__file__).parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PluginVisualContractTest(unittest.TestCase):
    def test_bundled_visual_contracts_declare_only_supported_schemes(self):
        expected = {
            "huya": {"huya"},
            "youtube": {"youtube"},
            "kuaishou": {"kuaishou"},
            "streamget-providers": {"bilibili", "douyu"},
        }
        for plugin_id, schemes in expected.items():
            with self.subTest(plugin_id=plugin_id):
                manifest = validate_manifest(json.loads(
                    (ROOT / "bundled_plugins" / plugin_id / "manifest.json").read_text()
                ))
                visual = next(c for c in manifest.provider_contracts if c.contract == "tv_visual_provider")
                self.assertEqual(visual.schemes, frozenset(schemes))
                self.assertIn("tv.visual_metadata", manifest.capabilities)

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


class VisualMetadataProviderMappingTest(unittest.TestCase):
    def _context(self):
        return ResolveContext("visual-fixture", 9_999_999_999_999, {}, None)

    def test_huya_maps_stable_room_cover_separately_from_live_screenshot(self):
        module = _load("huya_visual_fixture", ROOT / "bundled_plugins/huya/plugin.py")

        class Response:
            def read(self, _size):
                return json.dumps({"data": {"liveData": {
                    "avatar180": "https://img.example/avatar.jpg", "screenshot": "https://img.example/live.jpg",
                    "liveStatus": "ON", "introduction": "show", "nick": "host",
                }, "profileInfo": {"roomCover": "https://anchorpost.msstatic.com/room.jpg"}}}).encode()

            def __enter__(self): return self
            def __exit__(self, *_args): return None

        with mock.patch.object(module, "urlopen", return_value=Response()):
            visual = module.Provider().visual_metadata(TVReference("huya", "123"), self._context())
        self.assertEqual((visual.avatar_url, visual.stable_cover_url, visual.dynamic_cover_url,
                          visual.cover_url, visual.is_live, visual.cover_role),
                         ("https://img.example/avatar.jpg", "https://anchorpost.msstatic.com/room.jpg",
                          "https://img.example/live.jpg", "https://img.example/live.jpg", True, "live"))

    def test_huya_offline_still_returns_avatar_but_cover_policy_is_live(self):
        module = _load("huya_offline_visual_fixture", ROOT / "bundled_plugins/huya/plugin.py")

        class Response:
            def read(self, _size):
                return json.dumps({"data": {"liveStatus": "OFF", "liveData": {
                    "avatar180": "https://img.example/avatar.jpg", "screenshot": "https://img.example/old.jpg",
                }, "profileInfo": {"roomCover": "https://anchorpost.msstatic.com/stable-room.jpg"}}}).encode()

            def __enter__(self): return self
            def __exit__(self, *_args): return None

        with mock.patch.object(module, "urlopen", return_value=Response()):
            visual = module.Provider().visual_metadata(TVReference("huya", "123"), self._context())
        self.assertFalse(visual.is_live)
        self.assertEqual(visual.cover_role, "live")
        self.assertEqual(visual.avatar_url, "https://img.example/avatar.jpg")
        self.assertEqual(visual.stable_cover_url, "https://anchorpost.msstatic.com/stable-room.jpg")
        self.assertEqual(visual.dynamic_cover_url, "https://img.example/old.jpg")

    def test_youtube_thumbnail_is_generic_content_visual(self):
        module = _load("youtube_visual_fixture", ROOT / "bundled_plugins/youtube/plugin.py")
        visual = module.Provider().visual_metadata(
            TVReference("youtube", "dQw4w9WgXcQ"), self._context(),
        )
        self.assertEqual(visual.cover_url, "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg")
        self.assertEqual(visual.cover_role, "content")


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
