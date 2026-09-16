import asyncio
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

from adapters import AdapterResolveError
from routers import media_proxy
from security.dependencies import MediaAccessContext
from security.proxy_context import ProxyContext, get_registry, reset_for_tests
from security.source_ids import source_revision_for


def _source(url="huya://31421", *, source_id="src_huya"):
    return {
        "url": url,
        "source_type": "adapter",
        "enabled": True,
        "source_id": source_id,
    }


def _fake_main(get_channels, resolve):
    module = types.SimpleNamespace(
        _get_aggregated_iptv_channels=get_channels,
        _source_type=lambda source: source.get("source_type") or "hls",
        http_client=object(),
        AdapterResolveError=AdapterResolveError,
    )
    module.app = types.SimpleNamespace(
        state=types.SimpleNamespace(provider_resolver=types.SimpleNamespace(resolve=resolve)),
    )
    return module


class MediaResolveRevisionTest(unittest.TestCase):
    def setUp(self):
        self._had_main = "main" in sys.modules
        self._previous_main = sys.modules.get("main")

    def tearDown(self):
        if self._had_main:
            sys.modules["main"] = self._previous_main
        else:
            sys.modules.pop("main", None)
        reset_for_tests()

    def test_expected_revision_mismatch_is_rejected_before_provider_call(self):
        source = _source()
        provider_calls = []

        async def get_channels():
            return ([{"canonical_key": "频道", "urls": [source]}], [])

        async def resolve(*args, **kwargs):
            provider_calls.append((args, kwargs))
            return {"url": "https://cdn.example/live.m3u8", "source_type": "hls"}

        sys.modules["main"] = _fake_main(get_channels, resolve)
        with self.assertRaises(media_proxy.HTTPException) as raised:
            asyncio.run(media_proxy.media_channel_source_resolve(
                "频道",
                types.SimpleNamespace(headers={}),
                source_id=source["source_id"],
                expected_source_revision="revision-from-an-old-snapshot",
                access=MediaAccessContext(source="anonymous"),
            ))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "SOURCE_REVISION_STALE")
        self.assertEqual(provider_calls, [])

    def test_media_handle_import_order_does_not_create_secret_database_cycle(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        completed = subprocess.run(
            [sys.executable, "-c", "import security.proxy_handles; import database"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_revision_change_during_provider_resolve_discards_late_result(self):
        original = _source()
        changed = _source("huya://31422")
        revisions = [original, changed]
        provider_calls = []

        async def get_channels():
            current = revisions.pop(0) if revisions else changed
            return ([{"canonical_key": "频道", "urls": [current]}], [])

        async def resolve(*args, **kwargs):
            provider_calls.append((args, kwargs))
            return {
                "url": "https://cdn.example/old-result.m3u8",
                "source_type": "hls",
                "direct_playable": True,
            }

        sys.modules["main"] = _fake_main(get_channels, resolve)
        with mock.patch.object(media_proxy, "assert_safe_target_url", new=mock.AsyncMock()):
            with self.assertRaises(media_proxy.HTTPException) as raised:
                asyncio.run(media_proxy.media_channel_source_resolve(
                    "频道",
                    types.SimpleNamespace(headers={}),
                    source_id=original["source_id"],
                    expected_source_revision=source_revision_for(original),
                    access=MediaAccessContext(source="anonymous"),
                ))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "SOURCE_REVISION_STALE")
        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(provider_calls[0][1]["source_id"], original["source_id"])
        self.assertEqual(provider_calls[0][1]["source_revision"], source_revision_for(original))

    def test_source_removed_during_provider_resolve_discards_late_result(self):
        original = _source()
        snapshots = [
            [{"canonical_key": "频道", "urls": [original]}],
            [{"canonical_key": "频道", "urls": []}],
        ]

        async def get_channels():
            return (snapshots.pop(0) if snapshots else snapshots[-1], [])

        async def resolve(*args, **kwargs):
            return {"url": "https://cdn.example/removed.m3u8", "source_type": "hls"}

        sys.modules["main"] = _fake_main(get_channels, resolve)
        with self.assertRaises(media_proxy.HTTPException) as raised:
            asyncio.run(media_proxy.media_channel_source_resolve(
                "频道",
                types.SimpleNamespace(headers={}),
                source_id=original["source_id"],
                expected_source_revision=source_revision_for(original),
                access=MediaAccessContext(source="anonymous"),
            ))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "SOURCE_REVISION_STALE")

    def test_source_disabled_during_provider_resolve_discards_late_result(self):
        original = _source()
        disabled = _source()
        disabled["enabled"] = False
        snapshots = [
            [{"canonical_key": "频道", "urls": [original]}],
            [{"canonical_key": "频道", "urls": [disabled]}],
        ]

        async def get_channels():
            return (snapshots.pop(0) if snapshots else [{"canonical_key": "频道", "urls": [disabled]}], [])

        async def resolve(*args, **kwargs):
            return {"url": "https://cdn.example/disabled.m3u8", "source_type": "hls"}

        sys.modules["main"] = _fake_main(get_channels, resolve)
        with self.assertRaises(media_proxy.HTTPException) as raised:
            asyncio.run(media_proxy.media_channel_source_resolve(
                "频道",
                types.SimpleNamespace(headers={}),
                source_id=original["source_id"],
                expected_source_revision=source_revision_for(original),
                access=MediaAccessContext(source="anonymous"),
            ))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "SOURCE_REVISION_STALE")

    def test_resolve_response_is_bound_to_source_revision_and_proxy_url(self):
        source = _source()
        revision = source_revision_for(source)

        async def get_channels():
            return ([{"canonical_key": "频道", "urls": [source]}], [])

        async def resolve(*args, **kwargs):
            return {
                "url": "https://cdn.example/live.m3u8",
                "source_type": "hls",
                "direct_playable": True,
            }

        sys.modules["main"] = _fake_main(get_channels, resolve)
        with mock.patch.object(media_proxy, "assert_safe_target_url", new=mock.AsyncMock()):
            payload = asyncio.run(media_proxy.media_channel_source_resolve(
                "频道",
                types.SimpleNamespace(headers={}),
                source_id=source["source_id"],
                expected_source_revision=revision,
                access=MediaAccessContext(source="anonymous"),
            ))

        self.assertEqual(payload["source_revision"], revision)
        self.assertIn("expected_source_revision=", payload["proxy_url"])
        self.assertIn(revision, payload["proxy_url"])

    def test_revision_bound_hls_candidate_always_gets_proxy_context(self):
        source = {"url": "https://cdn.example/live.m3u8", "source_type": "hls"}
        captured = {}

        async def serve_playlist_by_source(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(status_code=200)

        fake_main = types.SimpleNamespace(serve_iptv_playlist_by_source=serve_playlist_by_source)
        sys.modules["main"] = fake_main
        revision = "revision-for-context"
        response = asyncio.run(media_proxy._serve_resolved_source_playlist(
            resolved_url=source["url"],
            resolved_st="hls",
            custom_ua="",
            referer="",
            cookie="",
            no_ua=False,
            canonical_key="频道",
            source_id="src_hls",
            source_revision=revision,
            access=MediaAccessContext(source="anonymous"),
        ))

        self.assertEqual(response.status_code, 200)
        ctx = get_registry().get(captured["ctx_id"])
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.source_id, "src_hls")
        self.assertEqual(ctx.source_revision, revision)

    def test_revision_bound_proxy_context_is_rejected_after_source_update(self):
        original = {"url": "https://cdn.example/old.m3u8", "source_type": "hls", "source_id": "src_hls"}
        current = {**original, "url": "https://cdn.example/new.m3u8"}
        context_id = get_registry().put(ProxyContext(
            source_id=original["source_id"],
            source_revision=source_revision_for(original),
        ))
        context = get_registry().get(context_id)

        async def get_channels():
            return ([{"canonical_key": "频道", "urls": [current]}], [])

        sys.modules["main"] = _fake_main(get_channels, lambda *_args, **_kwargs: None)
        with self.assertRaises(media_proxy.HTTPException) as raised:
            asyncio.run(media_proxy._validate_iptv_proxy_context(context))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "SOURCE_REVISION_STALE")


if __name__ == "__main__":
    unittest.main()
