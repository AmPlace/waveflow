"""短期 Proxy Context Registry。

用途：adapter 解析后产生的「临时 HTTP Header」（custom_ua / referer / cookie 等）
不能写进签名 handle 的 payload（cookie 明文落 URL 即便 base64 也违反规范），
也不应频繁写 SQLite。改为存进进程内有界 LRU+TTL，handle 仅携带 ctx_id。

约束：

* TTL 与 handle TTL 解耦：context 通常匹配 handle 寿命的最大值。
* 容量上限：超过则 LRU 淘汰，避免 adapter 频繁切频道时无界增长。
* 重启即丢：客户端会重新打 ``/api/media/channel/{id}/playlist.m3u8`` 入口拿新 ctx + handle。
* 永远不持久化敏感 header。

对外暴露 ``put`` / ``get``，``get`` 命中 expired 视为「不存在」。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib


# Context TTL 必须 ≥ 任意 handle TTL。最大 handle TTL = image (24h)，chunk = 6h。
# 这里取 26h，确保 chunk handle 在快到期之前 ctx 都还在。
_DEFAULT_TTL = 26 * 60 * 60
_DEFAULT_MAX_ENTRIES = 2048


@dataclass
class ProxyContext:
    """进程内的临时上游请求上下文。

    存的字段都「可以丢」。一旦丢了客户端会重新走入口接口拿新的，不影响业务。
    """
    custom_ua: str = ""
    referer: str = ""
    cookie: str = ""
    no_ua: bool = False
    upstream_url: str = ""
    source_type: str = ""    # adapter 解析得出，hls/mpegts/http_flv/rtsp...
    source_id: str = ""      # source_id；调试 + 风控用
    source_revision: str = ""  # 当前源配置指纹；仅用于缓存/ctx 失效
    expires_at: float = 0.0
    extras: dict = field(default_factory=dict)


class ProxyContextRegistry:
    def __init__(self, *, max_entries: int = _DEFAULT_MAX_ENTRIES, default_ttl: int = _DEFAULT_TTL) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[str, ProxyContext] = OrderedDict()
        # fingerprint -> ctx_id；用于让"内容相同"的 ProxyContext 在同一进程内
        # 复用同一个 ctx_id，避免下游 cache_key（含 ctx_id）随每次请求抖动。
        self._fingerprints: dict[str, str] = {}
        self._max = max_entries
        self._default_ttl = default_ttl

    def _now(self) -> float:
        return time.time()

    def _purge_expired_locked(self) -> None:
        now = self._now()
        # 一次最多扫尾部 16 条；避免被恶意打满后 put 时阻塞过久。
        scan_budget = 16
        for key in list(self._items.keys())[:scan_budget]:
            ctx = self._items.get(key)
            if ctx and ctx.expires_at <= now:
                self._items.pop(key, None)
                # fingerprint 反向索引也跟着清理
                fp = self._fp_for_locked(ctx)
                if fp and self._fingerprints.get(fp) == key:
                    self._fingerprints.pop(fp, None)

    @staticmethod
    def _fp_for_locked(ctx: "ProxyContext") -> str:
        """根据 ctx 的可识别字段算一个稳定 fingerprint。

        只覆盖会影响上游请求语义的字段（UA/Referer/Cookie/no_ua/upstream_url/
        source_type/source_id/source_revision）。``expires_at`` / ``extras`` 不参与，否则永远不命中。
        """
        h = hashlib.sha256()
        for v in (
            ctx.custom_ua or "",
            ctx.referer or "",
            ctx.cookie or "",
            "1" if ctx.no_ua else "0",
            ctx.upstream_url or "",
            ctx.source_type or "",
            ctx.source_id or "",
            ctx.source_revision or "",
        ):
            h.update(v.encode("utf-8", errors="replace"))
            h.update(b"\x1f")  # 字段分隔符，避免拼接歧义
        return h.hexdigest()

    def put(self, ctx: ProxyContext, *, ttl: int | None = None) -> str:
        """写入并返回不透明 ctx_id。

        若已经存在「内容等价」的 ProxyContext（按 fingerprint 比较），复用其
        ctx_id 并刷新过期时间。这让 playlist 的 cache_key（含 ctx_id）在
        同一频道短时间内的请求间稳定，下游 cache 才能真正命中。
        """
        effective_ttl = ttl if ttl is not None else self._default_ttl
        with self._lock:
            self._purge_expired_locked()
            fp = self._fp_for_locked(ctx)
            existing_id = self._fingerprints.get(fp)
            if existing_id:
                existing = self._items.get(existing_id)
                if existing and existing.expires_at > self._now():
                    # 复用：刷新 ttl + LRU 位置即可，原 ctx 字段保持不变。
                    existing.expires_at = self._now() + effective_ttl
                    self._items.move_to_end(existing_id)
                    return existing_id
                # 反向索引指向了一个已被淘汰/过期的条目，清掉重来。
                self._fingerprints.pop(fp, None)

            ctx_id = secrets.token_urlsafe(18)
            ctx.expires_at = self._now() + effective_ttl
            self._items[ctx_id] = ctx
            self._items.move_to_end(ctx_id)
            self._fingerprints[fp] = ctx_id
            while len(self._items) > self._max:
                evicted_id, evicted_ctx = self._items.popitem(last=False)
                ev_fp = self._fp_for_locked(evicted_ctx)
                if self._fingerprints.get(ev_fp) == evicted_id:
                    self._fingerprints.pop(ev_fp, None)
        return ctx_id

    def get(self, ctx_id: str) -> ProxyContext | None:
        if not ctx_id:
            return None
        with self._lock:
            ctx = self._items.get(ctx_id)
            if not ctx:
                return None
            if ctx.expires_at <= self._now():
                self._items.pop(ctx_id, None)
                return None
            self._items.move_to_end(ctx_id)
            return ctx

    def drop(self, ctx_id: str) -> bool:
        if not ctx_id:
            return False
        with self._lock:
            ctx = self._items.pop(ctx_id, None)
            if ctx is None:
                return False
            fp = self._fp_for_locked(ctx)
            if self._fingerprints.get(fp) == ctx_id:
                self._fingerprints.pop(fp, None)
            return True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._fingerprints.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# 进程级单例。测试用 ``reset_for_tests`` 清空。
_registry = ProxyContextRegistry()


def get_registry() -> ProxyContextRegistry:
    return _registry


def reset_for_tests() -> None:
    _registry.clear()
