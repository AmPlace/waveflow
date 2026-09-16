from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass
from typing import Any

from plugin_runtime import PluginError


CATALOG_ACTIVE = "active"
CATALOG_STALE = "stale"
CATALOG_EXPIRED = "expired"
CATALOG_DEFAULT_STALE_GRACE_SECONDS = 300.0


def catalog_identity(plugin_identity: str, external_id: str) -> str:
    """Return the stable Core identity for one Plugin-discovered item."""
    return f"{plugin_identity}::{external_id}"


@dataclass
class DynamicCatalogEntry:
    identity: str
    plugin_identity: str
    external_id: str
    name: str
    reference: str
    kind: str
    group: str | None
    logo: str | None
    starts_at: int | None
    ends_at: int | None
    metadata: dict[str, Any]
    ttl_seconds: int
    discovered_at: float
    expires_at: float
    state: str = CATALOG_ACTIVE
    stale_since: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "plugin_identity": self.plugin_identity,
            "external_id": self.external_id,
            "name": self.name,
            "reference": self.reference,
            "kind": self.kind,
            "group": self.group,
            "logo": self.logo,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "metadata": copy.deepcopy(self.metadata),
            "ttl_seconds": self.ttl_seconds,
            "discovered_at": self.discovered_at,
            "expires_at": self.expires_at,
            "state": self.state,
            "stale_since": self.stale_since,
        }


class DynamicChannelCatalog:
    """Core-owned ephemeral projection of a Plugin ChannelCatalogProvider.

    This projection deliberately has no database handle.  A caller may persist
    the returned normalized projection through a Core-owned repository later,
    but a Plugin response never receives a Core DB object or mutation path.
    Refresh scheduling is also external: Automation calls ``refresh_plugin``;
    this class does not create a scheduler per Plugin.
    """

    def __init__(self, *, stale_grace_seconds: float = CATALOG_DEFAULT_STALE_GRACE_SECONDS):
        if stale_grace_seconds < 0 or stale_grace_seconds > 86400:
            raise ValueError("stale_grace_seconds is outside the allowed range")
        self.stale_grace_seconds = float(stale_grace_seconds)
        self._entries: dict[str, DynamicCatalogEntry] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._refresh_failures: dict[str, dict[str, Any]] = {}

    def _lock_for(self, plugin_identity: str) -> asyncio.Lock:
        return self._refresh_locks.setdefault(plugin_identity, asyncio.Lock())

    def _expire(self, now: float) -> None:
        for entry in self._entries.values():
            if entry.ends_at is not None and entry.ends_at <= now:
                entry.state = CATALOG_EXPIRED
                continue
            if entry.state == CATALOG_ACTIVE and now >= entry.expires_at:
                entry.state = CATALOG_STALE
                entry.stale_since = entry.stale_since or entry.expires_at
            if (entry.state == CATALOG_STALE and entry.stale_since is not None
                    and now >= entry.stale_since + self.stale_grace_seconds):
                entry.state = CATALOG_EXPIRED

    def apply(self, plugin_identity: str, items: list[dict[str, Any]], *, now: float | None = None) -> dict[str, Any]:
        """Atomically publish one validated discovery result."""
        now = time.time() if now is None else float(now)
        incoming: dict[str, dict[str, Any]] = {}
        for item in items:
            external_id = str(item.get("external_id") or "")
            identity = catalog_identity(plugin_identity, external_id)
            if not external_id or identity in incoming:
                raise PluginError(
                    "INVALID_PLUGIN_RESPONSE", "Duplicate dynamic catalog identity", category="protocol",
                )
            incoming[identity] = item

        self._expire(now)
        plugin_entries = {
            identity: entry for identity, entry in self._entries.items()
            if entry.plugin_identity == plugin_identity
        }
        for identity, item in incoming.items():
            ttl = int(item["ttl_seconds"])
            self._entries[identity] = DynamicCatalogEntry(
                identity=identity,
                plugin_identity=plugin_identity,
                external_id=str(item["external_id"]),
                name=str(item["name"]),
                reference=str(item["reference"]),
                kind=str(item["kind"]),
                group=item.get("group"),
                logo=item.get("logo"),
                starts_at=item.get("starts_at"),
                ends_at=item.get("ends_at"),
                metadata=copy.deepcopy(item.get("metadata") or {}),
                ttl_seconds=ttl,
                discovered_at=now,
                expires_at=now + ttl,
                state=CATALOG_EXPIRED if item.get("ends_at") is not None and item["ends_at"] <= now else CATALOG_ACTIVE,
            )

        for identity, entry in plugin_entries.items():
            if identity in incoming:
                continue
            if entry.ends_at is not None and entry.ends_at <= now:
                entry.state = CATALOG_EXPIRED
            elif entry.state == CATALOG_EXPIRED:
                continue
            else:
                entry.state = CATALOG_STALE
                entry.stale_since = entry.stale_since or now

        self._refresh_failures.pop(plugin_identity, None)
        self._expire(now)
        return self.status(plugin_identity, now=now)

    def record_failure(self, plugin_identity: str, error: PluginError | Exception, *, now: float | None = None) -> dict[str, Any]:
        """Keep healthy cached entries during a discovery failure."""
        now = time.time() if now is None else float(now)
        self._expire(now)
        previous = self._refresh_failures.get(plugin_identity, {})
        self._refresh_failures[plugin_identity] = {
            "count": int(previous.get("count") or 0) + 1,
            "code": getattr(error, "code", "TEMPORARY_UPSTREAM_FAILURE"),
        }
        return self.status(plugin_identity, now=now)

    async def refresh_plugin(
        self, plugin_identity: str, runtime: Any, instance: Any, *, timeout: float = 15.0,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Refresh through the existing PluginRuntime; caller owns scheduling."""
        async with self._lock_for(plugin_identity):
            try:
                result = await runtime.request(instance, "channel_catalog.discover", {}, timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self.record_failure(plugin_identity, exc, now=now)
            return self.apply(plugin_identity, list(result.get("items") or []), now=now)

    def status(self, plugin_identity: str, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        self._expire(now)
        entries = [entry.as_dict() for entry in self._entries.values() if entry.plugin_identity == plugin_identity]
        entries.sort(key=lambda item: item["identity"])
        return {
            "plugin_identity": plugin_identity,
            "items": entries,
            "visible_items": [
                item for item in entries if item["state"] in {CATALOG_ACTIVE, CATALOG_STALE}
            ],
            "failures": copy.deepcopy(self._refresh_failures.get(plugin_identity, {})),
        }

    def get(self, identity: str, *, now: float | None = None) -> dict[str, Any] | None:
        now = time.time() if now is None else float(now)
        self._expire(now)
        entry = self._entries.get(identity)
        if entry is None or entry.state == CATALOG_EXPIRED:
            return None
        return entry.as_dict()

    def remove_plugin(self, plugin_identity: str) -> None:
        """Drop a Plugin's dynamic projection after explicit uninstall."""
        for identity in [
            identity for identity, entry in self._entries.items()
            if entry.plugin_identity == plugin_identity
        ]:
            self._entries.pop(identity, None)
        self._refresh_failures.pop(plugin_identity, None)
        self._refresh_locks.pop(plugin_identity, None)

    async def resolve(self, identity: str, resolver: Any, client: Any, *, now: float | None = None) -> dict[str, Any]:
        """Resolve a visible dynamic item through the existing TV resolver."""
        entry = self.get(identity, now=now)
        if entry is None:
            raise PluginError("RESOURCE_NOT_FOUND", "Dynamic catalog item is expired", category="routing")
        return await resolver.resolve(entry["reference"], client)
