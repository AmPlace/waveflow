"""Bounded source-scoped cache for optional TV visual metadata.

The cache is intentionally the only Core TTL owner for dynamic provider
visuals.  It is not persisted and it never uses a logical channel key as an
identity, because one logical channel can contain sources from different
providers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("visual.metadata")

_MAX_ENTRIES = 2048
_MAX_CONCURRENT_FETCHES = 4
_FETCH_TIMEOUT = 6.0
_FAILURE_TTL = 30


def empty_visual(*, source_id: str = "", source_revision: str = "", logo_url: str = "", title: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "source_id": source_id,
        "source_revision": source_revision,
        "avatar_url": "",
        "stable_cover_url": "",
        "dynamic_cover_url": "",
        "cover_url": "",
        "is_live": False,
        "title": title,
        "owner_name": "",
        "ttl_seconds": _FAILURE_TTL,
        "cover_role": "live",
        "fallback_logo_url": logo_url,
    }


class VisualMetadataCache:
    def __init__(self, *, fetch_timeout: float = _FETCH_TIMEOUT) -> None:
        self._results: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._expires: dict[tuple[str, str], float] = {}
        self._inflight: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)
        self._generation = 0
        self._fetch_timeout = fetch_timeout

    @staticmethod
    def _key(source_id: str, source_revision: str) -> tuple[str, str]:
        # Keep the two identity components structurally separate.  A string
        # concatenation would make crafted values containing ':' ambiguous.
        return source_id.strip(), source_revision.strip()

    def _get(self, key: str) -> dict[str, Any] | None:
        expires = self._expires.get(key, 0.0)
        if expires <= time.time():
            self._expires.pop(key, None)
            self._results.pop(key, None)
            return None
        value = self._results.get(key)
        if value is not None:
            self._results.move_to_end(key)
            return dict(value)
        return None

    def _put(self, key: str, value: dict[str, Any], ttl: int) -> None:
        if ttl < 1:
            ttl = _FAILURE_TTL
        self._results[key] = dict(value)
        self._expires[key] = time.time() + ttl
        self._results.move_to_end(key)
        while len(self._results) > _MAX_ENTRIES:
            old_key, _ = self._results.popitem(last=False)
            self._expires.pop(old_key, None)

    async def get_or_fetch(
        self,
        *,
        source_id: str,
        source_revision: str,
        fallback: dict[str, Any],
        fetch: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        key = self._key(source_id, source_revision)
        cached = self._get(key)
        if cached is not None:
            return cached

        shared = self._inflight.get(key)
        if shared is not None:
            try:
                return await asyncio.shield(shared)
            except asyncio.CancelledError:
                return dict(fallback)

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        generation = self._generation
        try:
            async with self._semaphore:
                try:
                    value = await asyncio.wait_for(fetch(), timeout=self._fetch_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.info("TV visual metadata fetch failed source=%s: %s", source_id, exc)
                    value = dict(fallback)
            if not isinstance(value, dict):
                value = dict(fallback)
            value.setdefault("source_id", source_id)
            value.setdefault("source_revision", source_revision)
            ttl = int(value.get("ttl_seconds") or _FAILURE_TTL)
            if self._generation == generation:
                has_visual = any(
                    value.get(field)
                    for field in ("avatar_url", "stable_cover_url", "dynamic_cover_url", "cover_url")
                )
                self._put(key, value, ttl if has_visual else _FAILURE_TTL)
            if not future.done():
                future.set_result(dict(value))
            return value
        except asyncio.CancelledError:
            if not future.done():
                future.set_result(dict(fallback))
            raise
        finally:
            if self._inflight.get(key) is future:
                self._inflight.pop(key, None)

    def invalidate_source(self, source_id: str, source_revision: str = "") -> None:
        source = source_id.strip()
        for key in list(self._results):
            if key[0] == source:
                self._results.pop(key, None)
                self._expires.pop(key, None)
        if source_revision:
            key = self._key(source_id, source_revision)
            self._results.pop(key, None)
            self._expires.pop(key, None)

    def invalidate_all(self) -> None:
        self._results.clear()
        self._expires.clear()
        self._generation += 1


_visual_metadata_cache = VisualMetadataCache()


def get_visual_metadata_cache() -> VisualMetadataCache:
    return _visual_metadata_cache
