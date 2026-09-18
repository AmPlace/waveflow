"""Core must not know how Huya/Douyu/Kuaishou/Bilibili covers are fetched.

Provider cover semantics live in the Plugins (``tv.visual_metadata``).  Core
keeps only the generic execution surface: the visual cache, the image proxy and
the anti-hotlink host policy.  These are source-level guards, so re-introducing
a Core fallback fails here even if no behavioural test happens to exercise it.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]

#: Names that only ever existed to serve the removed Core cover fallback.
REMOVED_NAMES = (
    "_fetch_bilibili_cover",
    "_fetch_douyu_cover",
    "_fetch_huya_cover",
    "_fetch_kuaishou_cover",
    "_ADAPTER_COVER_FETCHERS",
    "_ADAPTER_CAPABILITIES",
    "adapter_supports",
    "fetch_adapter_cover_payload",
    "_adapter_cover_cache",
    "_adapter_cover_locks",
    "_adapter_cover_empty",
    "invalidate_all_covers",
)

#: Generic execution Core still owns, and must keep.
RETAINED_NAMES = (
    "_COVER_IMG_PROXY_SOURCES",
    "_cover_img_referer_for",
    "_cover_img_proxy_url",
)

SCANNED = ("main.py", "routers/media_proxy.py", "routers/plugins.py")


def _read(relative: str) -> str:
    return (BACKEND / relative).read_text(encoding="utf-8")


class NoCoreCoverFallbackTest(unittest.TestCase):
    def test_no_provider_cover_fetcher_remains_in_core(self):
        for relative in SCANNED:
            source = _read(relative)
            for name in REMOVED_NAMES:
                with self.subTest(file=relative, name=name):
                    self.assertNotIn(
                        name, source,
                        f"{name} is provider cover semantics and belongs in a Plugin",
                    )

    def test_legacy_cover_cache_module_is_gone(self):
        self.assertIsNone(
            importlib.util.find_spec("core.cover_cache"),
            "core.cover_cache was the adapter cover fallback and must not come back",
        )

    def test_legacy_channel_cover_route_is_gone(self):
        source = _read("routers/media_proxy.py")
        self.assertNotIn('"/api/media/channel/{canonical_key}/cover"', source)
        self.assertIn('"/api/media/channel/{channel_key}/visual"', source)

    def test_generic_visual_surface_is_retained(self):
        main_source = _read("main.py")
        for name in RETAINED_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, main_source)
        media_proxy = _read("routers/media_proxy.py")
        for name in ("_normalize_visual_payload", "get_visual_metadata_cache", "empty_visual"):
            with self.subTest(name=name):
                self.assertIn(name, media_proxy)
        self.assertIsNotNone(importlib.util.find_spec("core.visual_metadata"))

    def test_visual_endpoint_never_invents_a_provider_fallback(self):
        """The visual fetch falls back to the stable logo, never to Core code."""
        source = _read("routers/media_proxy.py")
        start = source.index("async def media_channel_visual(")
        body = source[start:source.index("@router.", start + 1)]
        self.assertIn("resolver.visual_metadata(", body)
        self.assertIn("return dict(fallback)", body)


if __name__ == "__main__":
    unittest.main()
