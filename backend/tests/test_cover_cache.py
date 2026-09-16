"""test_cover_cache

Unit tests for core.cover_cache — single-flight, timeout, exception, cancel,
waiter-cancel, semaphore, invalidation API, generation guard.
All tests use in-process fakes; no real network.
"""
import asyncio
import os
import time
import unittest

os.environ.setdefault("WAVEFLOW_PROXY_HANDLE_SECRET", "test-key-32bytes-1337!!!!!")
os.environ.setdefault("WAVEFLOW_MODE", "nas")
os.environ.setdefault("WAVEFLOW_DB_PATH", ":memory:")


class CoverCacheSingleFlightTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from core.cover_cache import CoverCache
        # 默认 5s 超时
        self.cache = CoverCache()

    # ── helpers ──────────────────────────────────────────────────────

    async def _concurrent_get_or_fetch(self, n, canonical_key, adapter_url, delay=0.05):
        call_count = 0

        async def _resolve(_url):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(delay)
            return {"ok": True, "cover_url": f"https://img.example.com/{canonical_key}.jpg", "avatar_url": ""}

        import main as _m
        _orig = getattr(_m, "fetch_adapter_cover_payload", None)
        _m.fetch_adapter_cover_payload = _resolve
        try:
            tasks = [
                self.cache.get_or_fetch(canonical_key, adapter_url, "", canonical_key)
                for _ in range(n)
            ]
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=3.0)
        finally:
            if _orig is not None:
                _m.fetch_adapter_cover_payload = _orig
            else:
                _m.fetch_adapter_cover_payload = None
        return results, call_count

    # ── single-flight tests ──────────────────────────────────────────

    async def test_same_key_single_flight(self):
        results, call_count = await self._concurrent_get_or_fetch(
            30, "channel_a", "adapter://test/a", delay=0.05
        )
        self.assertEqual(call_count, 1)
        self.assertEqual(len(results), 30)

    async def test_same_key_cache_hit_after_fetch(self):
        _, c1 = await self._concurrent_get_or_fetch(5, "channel_b", "adapter://test/b", delay=0.03)
        self.assertEqual(c1, 1)

        results2, c2 = await self._concurrent_get_or_fetch(5, "channel_b", "adapter://test/b", delay=0.03)
        self.assertEqual(c2, 0)
        self.assertEqual(len(results2), 5)

    async def test_different_keys_parallel(self):
        concurrent_count = 0
        max_concurrent = 0

        async def _resolve(_url):
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.05)
            concurrent_count -= 1
            return {"ok": True, "cover_url": "ok", "avatar_url": ""}

        import main as _m
        _orig = getattr(_m, "fetch_adapter_cover_payload", None)
        _m.fetch_adapter_cover_payload = _resolve
        try:
            tasks = [
                self.cache.get_or_fetch(f"ch_{i}", f"adapter://test/{i}", "", f"ch_{i}")
                for i in range(10)
            ]
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)
        finally:
            if _orig is not None:
                _m.fetch_adapter_cover_payload = _orig
            else:
                _m.fetch_adapter_cover_payload = None

        self.assertLessEqual(max_concurrent, 4)
        self.assertGreater(max_concurrent, 1)

    async def test_resolver_exception_wakes_waiters(self):
        call_count = 0

        async def _resolve(_url):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        import main as _m
        _orig = getattr(_m, "fetch_adapter_cover_payload", None)
        _m.fetch_adapter_cover_payload = _resolve
        try:
            tasks = [
                self.cache.get_or_fetch("ch_exc", "adapter://test/exc", "", "ch_exc")
                for _ in range(5)
            ]
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
        finally:
            if _orig is not None:
                _m.fetch_adapter_cover_payload = _orig
            else:
                _m.fetch_adapter_cover_payload = None

        self.assertEqual(call_count, 1)
        self.assertEqual(len(results), 5)
        for r in results:
            self.assertEqual(r.get("cover_url"), "")

    async def test_real_resolver_timeout_wakes_waiters(self):
        """真实 timeout 测试：注入很短超时，resolver 实际超时（非 cancel）。"""
        from core.cover_cache import CoverCache
        # 注入 100ms 超时
        cache = CoverCache(fetch_timeout=0.1)
        call_count = 0

        async def _resolve(_url):
            nonlocal call_count
            call_count += 1
            # 阻塞 1s，远超 100ms 超时
            await asyncio.sleep(1.0)
            return {"ok": True, "cover_url": "too_late", "avatar_url": ""}

        import main as _m
        _orig = getattr(_m, "fetch_adapter_cover_payload", None)
        _m.fetch_adapter_cover_payload = _resolve
        try:
            t0 = time.monotonic()
            tasks = [
                cache.get_or_fetch("ch_to", "adapter://test/to", "", "ch_to")
                for _ in range(3)
            ]
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.5)
            elapsed = time.monotonic() - t0
        finally:
            if _orig is not None:
                _m.fetch_adapter_cover_payload = _orig
            else:
                _m.fetch_adapter_cover_payload = None

        self.assertEqual(call_count, 1)
        self.assertEqual(len(results), 3)
        # 所有 waiter 都返回 empty
        for r in results:
            self.assertEqual(r.get("cover_url"), "")
        # 真实超时大致在 100ms 后返回（容忍调度延迟），不会等到 resolver 1s
        self.assertLess(elapsed, 0.5)
        # negative cache 已写入
        self.assertIn("adapter:adapter://test/to", cache._results)
        # _inflight 已清理
        self.assertNotIn("adapter:adapter://test/to", cache._inflight)

    async def test_owner_cancel_reraises_and_wakes_waiters(self):
        """Owner cancel：必须重新抛出 CancelledError；waiter 被唤醒。"""
        call_count = 0
        started = asyncio.Event()
        blocker = asyncio.Event()

        async def _resolve(_url):
            nonlocal call_count
            call_count += 1
            started.set()
            await blocker.wait()

        import main as _m
        _orig = getattr(_m, "fetch_adapter_cover_payload", None)
        _m.fetch_adapter_cover_payload = _resolve
        try:
            async def _waiter():
                return await self.cache.get_or_fetch("ch_c1", "adapter://test/c1", "", "ch_c1")

            owner_task = asyncio.create_task(_waiter())
            await asyncio.wait_for(started.wait(), timeout=1.0)

            w1 = asyncio.create_task(_waiter())
            w2 = asyncio.create_task(_waiter())
            await asyncio.sleep(0.05)

            owner_task.cancel()
            # owner 必须重新抛出 CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await owner_task

            # waiter 不挂起
            r1, r2 = await asyncio.wait_for(asyncio.gather(w1, w2), timeout=1.0)
            self.assertEqual(r1.get("cover_url"), "")
            self.assertEqual(r2.get("cover_url"), "")
            self.assertEqual(call_count, 1)
            # _inflight 清空
            self.assertEqual(len(self.cache._inflight), 0)
        finally:
            blocker.set()
            if _orig is not None:
                _m.fetch_adapter_cover_payload = _orig
            else:
                _m.fetch_adapter_cover_payload = None

    async def test_owner_cancel_subsequent_request_can_retry(self):
        """Owner cancel 之后，相同 key 后续请求可以重新启动 resolver。"""
        call_count = 0
        first_started = asyncio.Event()
        first_blocker = asyncio.Event()
        second_started = asyncio.Event()

        async def _resolve(_url):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                first_started.set()
                await first_blocker.wait()
                return {"ok": True, "cover_url": "first", "avatar_url": ""}
            else:
                second_started.set()
                return {"ok": True, "cover_url": "second", "avatar_url": ""}

        import main as _m
        _orig = getattr(_m, "fetch_adapter_cover_payload", None)
        _m.fetch_adapter_cover_payload = _resolve
        try:
            owner = asyncio.create_task(
                self.cache.get_or_fetch("ch_retry", "adapter://test/retry", "", "ch_retry")
            )
            await asyncio.wait_for(first_started.wait(), timeout=1.0)
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner

            # 即使 negative cache 写入了，调用方可以 invalidate 后再试
            self.cache.invalidate_cover("adapter://test/retry")

            result = await asyncio.wait_for(
                self.cache.get_or_fetch("ch_retry", "adapter://test/retry", "", "ch_retry"),
                timeout=1.0,
            )
            self.assertEqual(result.get("cover_url"), "second")
            self.assertEqual(call_count, 2)
        finally:
            first_blocker.set()
            if _orig is not None:
                _m.fetch_adapter_cover_payload = _orig
            else:
                _m.fetch_adapter_cover_payload = None

    async def test_waiter_cancel_does_not_break_owner(self):
        call_count = 0
        started = asyncio.Event()
        done = asyncio.Event()

        async def _resolve(_url):
            nonlocal call_count
            call_count += 1
            started.set()
            await done.wait()
            return {"ok": True, "cover_url": "https://ok.example.com/img.jpg", "avatar_url": ""}

        import main as _m
        _orig = getattr(_m, "fetch_adapter_cover_payload", None)
        _m.fetch_adapter_cover_payload = _resolve
        try:
            async def _waiter():
                return await self.cache.get_or_fetch("ch_c2", "adapter://test/c2", "", "ch_c2")

            owner_task = asyncio.create_task(_waiter())
            await asyncio.wait_for(started.wait(), timeout=1.0)

            w2 = asyncio.create_task(_waiter())

            w3 = asyncio.create_task(_waiter())
            await asyncio.sleep(0.02)
            w3.cancel()
            try:
                await w3
            except asyncio.CancelledError:
                pass

            done.set()

            r_owner = await asyncio.wait_for(owner_task, timeout=1.0)
            r_w2 = await asyncio.wait_for(w2, timeout=1.0)
            self.assertEqual(r_owner.get("cover_url"), "https://ok.example.com/img.jpg")
            self.assertEqual(r_w2.get("cover_url"), "https://ok.example.com/img.jpg")
            self.assertEqual(call_count, 1)
        finally:
            done.set()
            if _orig is not None:
                _m.fetch_adapter_cover_payload = _orig
            else:
                _m.fetch_adapter_cover_payload = None

    async def test_inflight_cleaned_after_completion(self):
        results, _ = await self._concurrent_get_or_fetch(
            5, "ch_inflight", "adapter://test/inflight", delay=0.02
        )
        self.assertEqual(len(results), 5)
        self.assertEqual(len(self.cache._inflight), 0)

    async def test_non_adapter_returns_logo_cached(self):
        r1 = await self.cache.get_or_fetch("ch_no_ad", None, "https://logo.example.com/a.png", "频道A")
        self.assertEqual(r1["cover_url"], "https://logo.example.com/a.png")

        r2 = await self.cache.get_or_fetch("ch_no_ad", None, "https://logo.example.com/a.png", "频道A")
        self.assertEqual(r2["cover_url"], "https://logo.example.com/a.png")

    async def test_non_adapter_does_not_hold_semaphore(self):
        held = asyncio.Event()

        async def _slow_resolve(_url):
            await held.wait()
            return {"ok": True, "cover_url": "slow", "avatar_url": ""}

        import main as _m
        _orig = getattr(_m, "fetch_adapter_cover_payload", None)
        _m.fetch_adapter_cover_payload = _slow_resolve
        try:
            slow_tasks = [
                asyncio.create_task(
                    self.cache.get_or_fetch(f"slow_{i}", f"adapter://test/slow_{i}", "", f"slow_{i}")
                )
                for i in range(4)
            ]
            await asyncio.sleep(0.05)

            t0 = time.monotonic()
            r = await asyncio.wait_for(
                self.cache.get_or_fetch("fast_no_ad", None, "https://l.example.com/x.png", "fast"),
                timeout=0.5,
            )
            elapsed = time.monotonic() - t0
            self.assertEqual(r["cover_url"], "https://l.example.com/x.png")
            self.assertLess(elapsed, 0.3)
        finally:
            held.set()
            if _orig is not None:
                _m.fetch_adapter_cover_payload = _orig
            else:
                _m.fetch_adapter_cover_payload = None

    # ── invalidation API tests ───────────────────────────────────────

    async def test_invalidate_index_rebuilds_on_next_get(self):
        """invalidate_index 后下一次 get_channel 重建索引。"""
        # 注入伪索引
        self.cache._index = {"k1": {"canonical_key": "k1", "name": "K1"}}
        self.cache._index_expires = time.time() + 1000

        ch = await self.cache.get_channel("k1")
        self.assertEqual(ch["name"], "K1")

        self.cache.invalidate_index()
        self.assertIsNone(self.cache._index)
        self.assertEqual(self.cache._index_expires, 0.0)

    async def test_invalidate_cover_clears_only_target_key(self):
        """invalidate_cover(key) 只清该 key，不影响其他 key。"""
        # 准备两个 cover 缓存
        await self.cache.get_or_fetch("ka", None, "logoA.png", "A")
        await self.cache.get_or_fetch("kb", None, "logoB.png", "B")
        self.assertIn("noop:ka", self.cache._results)
        self.assertIn("noop:kb", self.cache._results)

        self.cache.invalidate_cover("ka")
        self.assertNotIn("noop:ka", self.cache._results)
        self.assertIn("noop:kb", self.cache._results)

    async def test_invalidate_all_clears_index_and_results(self):
        """invalidate_all 清除 index、results、negative cache，generation 递增。"""
        self.cache._index = {"k1": {"name": "X"}}
        self.cache._index_expires = time.time() + 1000
        await self.cache.get_or_fetch("ka", None, "logo.png", "A")
        gen_before = self.cache._generation

        self.cache.invalidate_all()
        self.assertIsNone(self.cache._index)
        self.assertEqual(len(self.cache._results), 0)
        self.assertEqual(len(self.cache._expires), 0)
        self.assertEqual(self.cache._generation, gen_before + 1)

    async def test_generation_prevents_stale_writeback(self):
        """resolver 进行中 invalidate_all → resolver 完成后旧结果不写缓存。"""
        started = asyncio.Event()
        finish = asyncio.Event()

        async def _resolve(_url):
            started.set()
            await finish.wait()
            return {"ok": True, "cover_url": "stale", "avatar_url": ""}

        import main as _m
        _orig = getattr(_m, "fetch_adapter_cover_payload", None)
        _m.fetch_adapter_cover_payload = _resolve
        try:
            owner = asyncio.create_task(
                self.cache.get_or_fetch("kg", "adapter://test/g", "", "kg")
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)

            # invalidate 在 resolver 完成前
            self.cache.invalidate_all()

            # 释放 resolver
            finish.set()
            result = await asyncio.wait_for(owner, timeout=1.0)

            # resolver 仍然返回值
            self.assertEqual(result.get("cover_url"), "stale")
            # 但 generation 已改，cache 不应写入
            self.assertNotIn("adapter:adapter://test/g", self.cache._results)
        finally:
            finish.set()
            if _orig is not None:
                _m.fetch_adapter_cover_payload = _orig
            else:
                _m.fetch_adapter_cover_payload = None


class CoverCacheBusinessIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """验证业务调用点（订阅刷新、Market 导入）会触发 cover 缓存失效。"""

    async def test_invalidate_all_module_level_function(self):
        """模块级 invalidate_all_covers 调用 → 单例 cache 状态清空。"""
        from core.cover_cache import get_cover_cache, invalidate_all_covers
        cache = get_cover_cache()
        cache._index = {"x": {"name": "x"}}
        cache._index_expires = time.time() + 1000
        await cache.get_or_fetch("test_biz", None, "logo.png", "X")

        gen_before = cache._generation
        invalidate_all_covers()

        self.assertIsNone(cache._index)
        self.assertEqual(len(cache._results), 0)
        self.assertEqual(cache._generation, gen_before + 1)


if __name__ == "__main__":
    unittest.main()
