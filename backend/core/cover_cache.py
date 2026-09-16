"""轻量 Cover 缓存：不依赖全量频道聚合的按 key 查询。

核心设计：
1. 频道索引缓存（canonical_key → channel dict），TTL 5min，O(1) 查找。
2. Cover 结果缓存（成功/失败分开 TTL），有界 LRU。
3. 并发 single-flight：asyncio.Future 共享，同一 key 只执行一次 resolver。
4. Adapter cover fetcher 专用 Semaphore（4），防止过载。
5. 不持有任何锁执行远端网络请求。

安全边界：
* 不恢复 raw URL 代理；
* 不绕过 handle 签名/SSRF/Media Access；
* 不把管理员凭证写入 URL。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("cover.cache")

_COVER_CHANNEL_INDEX_TTL = 5 * 60        # 频道索引缓存 5min
_COVER_SUCCESS_TTL = 30 * 60             # 成功封面缓存 30min
_COVER_FAILURE_TTL = 2 * 60              # 失败封面缓存 2min
_COVER_MAX_ENTRIES = 2048                # 最大缓存条目
_COVER_MAX_CONCURRENT_FETCHES = 4        # adapter fetch 最大并发
_COVER_FETCH_TIMEOUT = 5.0               # 单次 cover fetch 超时（秒）


class CoverCache:
    """封面专用缓存：index + result + single-flight + semaphore + generation。"""

    def __init__(self, *, fetch_timeout: float = _COVER_FETCH_TIMEOUT) -> None:
        self._lock = asyncio.Lock()
        # 频道索引缓存（dict[canonical_key, channel] — O(1) 查找）
        self._index: dict[str, dict] | None = None
        self._index_expires: float = 0.0
        # Cover 结果缓存（LRU + TTL）
        self._results: OrderedDict[str, dict] = OrderedDict()
        self._expires: dict[str, float] = {}
        # Single-flight：asyncio.Future 共享
        self._inflight: dict[str, asyncio.Future[dict]] = {}
        # Adapter fetch 并发限制
        self._semaphore = asyncio.Semaphore(_COVER_MAX_CONCURRENT_FETCHES)
        # Generation：invalidate 时递增，resolver 完成后比对，防止旧结果回写
        self._generation: int = 0
        # 单次 fetch 超时（可注入）
        self._fetch_timeout: float = fetch_timeout

    # ── 频道索引 ────────────────────────────────────────────────────────

    async def _build_index(self) -> dict[str, dict]:
        """构建 canonical_key → channel 字典索引。"""
        import main as _m
        channels, _groups = await _m._get_aggregated_iptv_channels()
        return {ch["canonical_key"]: ch for ch in channels if ch.get("canonical_key")}

    async def _get_channel_index(self) -> dict[str, dict]:
        """获取或刷新频道索引（O(1) 查找，5min TTL）。"""
        now = time.time()
        if self._index is not None and now < self._index_expires:
            return self._index
        async with self._lock:
            if self._index is not None and now < self._index_expires:
                return self._index
            self._index = await self._build_index()
            self._index_expires = now + _COVER_CHANNEL_INDEX_TTL
            return self._index

    def invalidate_index(self) -> None:
        """主动失效频道索引（订阅刷新 / Market 导入后调用）。"""
        self._index = None
        self._index_expires = 0.0

    def invalidate_cover(self, canonical_key_or_url: str) -> None:
        """精准失效某个 key 的封面缓存（含成功+失败+noop）。"""
        for prefix in ("adapter:", "noop:"):
            key = f"{prefix}{canonical_key_or_url}"
            self._results.pop(key, None)
            self._expires.pop(key, None)

    def invalidate_all(self) -> None:
        """清除所有 Cover 缓存状态（索引+结果+generation）。

        不清除 ProxyContext、auth session、handle secret 等无关安全状态。
        """
        self._index = None
        self._index_expires = 0.0
        self._results.clear()
        self._expires.clear()
        self._generation += 1
        # 不取消正在进行的 inflight resolver（让其自然完成；
        # 但 resolver 完成后会比对 generation，过期不回写）

    async def get_channel(self, canonical_key: str) -> dict | None:
        """按 canonical_key 查频道（O(1) 内存查找）。"""
        index = await self._get_channel_index()
        return index.get(canonical_key)

    # ── Cover 结果缓存 ──────────────────────────────────────────────────

    def _cached_result(self, key: str) -> dict | None:
        now = time.time()
        expires = self._expires.get(key, 0)
        if expires > now:
            return self._results.get(key)
        # 过期淘汰
        popped = self._results.pop(key, None)
        self._expires.pop(key, None)
        return None

    def _set_result(self, key: str, value: dict, ttl: int) -> None:
        self._results[key] = value
        self._expires[key] = time.time() + ttl
        self._results.move_to_end(key)
        while len(self._results) > _COVER_MAX_ENTRIES:
            self._results.popitem(last=False)

    def _make_empty_result(self) -> dict:
        return {
            "ok": True,
            "adapter": "",
            "cover_url": "",
            "avatar_url": "",
            "title": "",
            "anchor_name": "",
            "is_live": False,
        }

    # ── Single-flight ───────────────────────────────────────────────────

    async def get_or_fetch(
        self,
        canonical_key: str,
        adapter_source_url: str | None,
        logo_url: str,
        channel_name: str,
    ) -> dict:
        """获取 cover 结果（缓存命中 / single-flight 合并 / 新 fetch）。"""
        # 非 adapter：直接兜底（缓存避免重复处理）
        if not adapter_source_url:
            cache_key = f"noop:{canonical_key}"
            cached = self._cached_result(cache_key)
            if cached:
                return cached
            result = {
                "ok": True,
                "adapter": "",
                "cover_url": logo_url or "",
                "avatar_url": "",
                "title": channel_name or "",
                "anchor_name": "",
                "is_live": False,
            }
            self._set_result(cache_key, result, _COVER_SUCCESS_TTL)
            return result

        cache_key = f"adapter:{adapter_source_url}"

        # 1. 结果缓存命中
        cached = self._cached_result(cache_key)
        if cached is not None:
            return cached

        # 2. Single-flight：共享已有 Future
        shared = self._inflight.get(cache_key)
        if shared is not None:
            try:
                return await asyncio.shield(shared)
            except asyncio.CancelledError:
                # waiter 被取消不影响 owner；返回 empty
                return self._make_empty_result()

        # 3. Owner：创建 Future，执行 resolver
        future: asyncio.Future[dict] = asyncio.Future()
        self._inflight[cache_key] = future
        gen_at_start = self._generation
        try:
            result = await self._run_fetch(future, adapter_source_url)
            # Only set_result if future hasn't been resolved (by cancellation or timeout)
            if not future.done():
                future.set_result(result)
            # 仅当 generation 未变时才写缓存（防止 invalidate 后旧结果回写）
            if self._generation == gen_at_start:
                ttl = _COVER_SUCCESS_TTL if result.get("cover_url") or result.get("avatar_url") else _COVER_FAILURE_TTL
                self._set_result(cache_key, result, ttl)
            return result
        except asyncio.CancelledError:
            # Owner was cancelled — set empty result so waiters don't hang
            empty = self._make_empty_result()
            if not future.done():
                future.set_result(empty)
            if self._generation == gen_at_start:
                self._set_result(cache_key, empty, _COVER_FAILURE_TTL)
            raise
        except Exception:
            empty = self._make_empty_result()
            if not future.done():
                future.set_result(empty)
            if self._generation == gen_at_start:
                self._set_result(cache_key, empty, _COVER_FAILURE_TTL)
            return empty
        finally:
            # 仅删除仍指向当前 future 的条目（防止误删后续新请求创建的 future）
            if self._inflight.get(cache_key) is future:
                del self._inflight[cache_key]

    async def _run_fetch(
        self,
        future: asyncio.Future[dict],
        adapter_source_url: str,
    ) -> dict:
        """在 semaphore 内执行实际 fetch，支持超时。"""
        try:
            async with self._semaphore:
                import main as _m
                try:
                    return await asyncio.wait_for(
                        _m.fetch_adapter_cover_payload(adapter_source_url),
                        timeout=self._fetch_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.info("cover fetch timeout for %s", adapter_source_url[:80])
                    return self._make_empty_result()
        except asyncio.CancelledError:
            # semaphore 等待期间被取消：设置 future 避免 waiter 挂
            if not future.done():
                future.set_result(self._make_empty_result())
            raise


# 进程级单例
_cover_cache = CoverCache()


def get_cover_cache() -> CoverCache:
    return _cover_cache


def invalidate_cover_index() -> None:
    """主动失效频道索引（供订阅刷新 / Market 导入后调用）。"""
    _cover_cache.invalidate_index()


def invalidate_cover(canonical_key_or_url: str) -> None:
    """精准失效单个 cover 缓存。"""
    _cover_cache.invalidate_cover(canonical_key_or_url)


def invalidate_all_covers() -> None:
    """清除所有 Cover 缓存（索引+结果），供批量数据变更后调用。"""
    _cover_cache.invalidate_all()
