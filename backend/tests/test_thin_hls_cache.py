import asyncio
import os
import time
import unittest
from unittest import mock

import httpx
from fastapi import HTTPException


os.environ.setdefault("WAVEFLOW_MODE", "nas")
os.environ.setdefault("WAVEFLOW_DB_PATH", ":memory:")

import main


def _playlist_response(url: str, *, status_code: int = 200, text: str = "#EXTM3U\n") -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request("GET", url),
        text=text,
    )


class ThinPlaylistCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._THIN_CACHE.clear()
        main._THIN_LOCKS.clear()

    def tearDown(self):
        main._THIN_CACHE.clear()
        main._THIN_LOCKS.clear()

    async def test_cache_key_isolates_query_headers_and_source_revision(self):
        url_a = "https://cdn.example/live/index.m3u8?token=a"
        url_b = "https://cdn.example/live/index.m3u8?token=b"
        headers_a = {"Cookie": "session=a", "Referer": "https://a.example/"}
        headers_b = {"Cookie": "session=b", "Referer": "https://a.example/"}

        self.assertNotEqual(
            main._thin_cache_key(url_a, "src:rev1", headers_a),
            main._thin_cache_key(url_b, "src:rev1", headers_a),
        )
        self.assertNotEqual(
            main._thin_cache_key(url_a, "src:rev1", headers_a),
            main._thin_cache_key(url_a, "src:rev1", headers_b),
        )
        self.assertNotEqual(
            main._thin_cache_key(url_a, "src:rev1", headers_a),
            main._thin_cache_key(url_a, "src:rev2", headers_a),
        )
        self.assertNotEqual(
            main._thin_cache_key(url_a, "src:rev1", headers_a),
            main._thin_cache_key(url_a, "src:rev1", headers_a, {"User-Agent"}),
        )

        key = main._thin_cache_key(url_a, "src:rev1", headers_a)
        self.assertNotIn("session=a", key)
        self.assertNotIn("token=a", key)

    async def test_same_key_twenty_clients_share_one_upstream_fetch(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fetch(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return _playlist_response("https://cdn.example/live/index.m3u8")

        with mock.patch.object(main, "request_with_safe_redirects", new=fetch):
            tasks = [asyncio.create_task(main._thin_playlist_fetch(
                "https://cdn.example/live/index.m3u8?token=a",
                {"Cookie": "session=a"},
                cache_scope="src:rev1",
            )) for _ in range(20)]
            await entered.wait()
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(*tasks)

        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 20)
        self.assertEqual(main._THIN_LOCKS, {})

    async def test_different_keys_fetch_in_parallel(self):
        both_entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        max_active = 0

        async def fetch(_client, _method, url, **_kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_entered.set()
            await release.wait()
            active -= 1
            return _playlist_response(url)

        with mock.patch.object(main, "request_with_safe_redirects", new=fetch):
            first = asyncio.create_task(main._thin_playlist_fetch("https://a.example/live.m3u8"))
            second = asyncio.create_task(main._thin_playlist_fetch("https://b.example/live.m3u8"))
            await asyncio.wait_for(both_entered.wait(), timeout=1)
            release.set()
            await asyncio.gather(first, second)

        self.assertEqual(max_active, 2)
        self.assertEqual(main._THIN_LOCKS, {})

    async def test_cancelled_fetch_releases_lock_entry(self):
        entered = asyncio.Event()

        async def fetch(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        with mock.patch.object(main, "request_with_safe_redirects", new=fetch):
            task = asyncio.create_task(main._thin_playlist_fetch("https://cdn.example/cancel.m3u8"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(main._THIN_LOCKS, {})

    async def test_ten_thousand_distinct_keys_do_not_leave_lock_entries(self):
        async def fetch(_client, _method, url, **_kwargs):
            return _playlist_response(url)

        with mock.patch.object(main, "request_with_safe_redirects", new=fetch):
            for index in range(10_000):
                await main._thin_playlist_fetch(f"https://cdn.example/live/{index}.m3u8")

        self.assertEqual(main._THIN_LOCKS, {})
        self.assertLessEqual(len(main._THIN_CACHE), main._THIN_MAX_ENTRIES)

    async def test_explicit_expired_status_invalidates_cache_without_stale_fallback(self):
        for status_code in (401, 403, 404, 410):
            with self.subTest(status_code=status_code):
                main._THIN_CACHE.clear()
                url = f"https://cdn.example/live/index.m3u8?status={status_code}"
                cache_key = main._thin_cache_key(url, "src:rev1")
                main._THIN_CACHE[cache_key] = ("#EXTM3U\n# stale\n", url, 1.0)

                async def fetch(*_args, **_kwargs):
                    return _playlist_response(url, status_code=status_code, text="denied")

                with mock.patch.object(main, "request_with_safe_redirects", new=fetch), mock.patch.object(
                    main.asyncio, "sleep", new=mock.AsyncMock()
                ):
                    with self.assertRaises(HTTPException):
                        await main._thin_playlist_fetch(url, cache_scope="src:rev1")

                self.assertNotIn(cache_key, main._THIN_CACHE)
        self.assertEqual(main._THIN_LOCKS, {})

    async def test_transient_failure_can_still_use_expired_stale_cache(self):
        url = "https://cdn.example/live/index.m3u8"
        cache_key = main._thin_cache_key(url, "src:rev1")
        main._THIN_CACHE[cache_key] = ("#EXTM3U\n# stale\n", url, time.time() - 1)

        async def fetch(*_args, **_kwargs):
            raise httpx.ConnectTimeout("temporary timeout")

        with mock.patch.object(main, "request_with_safe_redirects", new=fetch), mock.patch.object(
            main.asyncio, "sleep", new=mock.AsyncMock()
        ):
            text, final_url = await main._thin_playlist_fetch(url, cache_scope="src:rev1")

        self.assertEqual(text, "#EXTM3U\n# stale\n")
        self.assertEqual(final_url, url)
        self.assertEqual(main._THIN_LOCKS, {})

    async def test_transient_failure_does_not_use_unbounded_old_stale_cache(self):
        url = "https://cdn.example/live/old.m3u8"
        cache_key = main._thin_cache_key(url, "src:rev1")
        main._THIN_CACHE[cache_key] = (
            "#EXTM3U\n# too old\n",
            url,
            time.time() - main._THIN_STALE_MAX_AGE - 1,
        )

        async def fetch(*_args, **_kwargs):
            raise httpx.ConnectTimeout("temporary timeout")

        with mock.patch.object(main, "request_with_safe_redirects", new=fetch), mock.patch.object(
            main.asyncio, "sleep", new=mock.AsyncMock()
        ):
            with self.assertRaises(HTTPException):
                await main._thin_playlist_fetch(url, cache_scope="src:rev1")

        self.assertEqual(main._THIN_LOCKS, {})


if __name__ == "__main__":
    unittest.main()
