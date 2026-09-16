"""Production Radio domain bridge.

This module deliberately keeps Radio storage and routing separate from the
IPTV channel model.  It owns only the small amount of data needed to publish a
Plugin catalog and resolve an explicitly selected Radio source.  Media bytes
still flow through the existing signed-handle/proxy pipeline.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

import database
from plugin_runtime import PluginError
from plugin_runtime.validation import validate_radio_catalog
from provider_resolver import ProviderResolver
from security.source_ids import MediaSourceIdentity, media_source_id_for, media_source_revision_for


RADIO_DOMAIN = "radio"
RADIO_CATALOG_STALE_GRACE_SECONDS = 24 * 60 * 60
RADIO_DESCRIPTOR_CACHE_DEFAULT_TTL_SECONDS = 300
RADIO_PROGRAMME_CACHE_DEFAULT_TTL_SECONDS = 600
RADIO_DESCRIPTOR_CACHE_MAX_ENTRIES = 1024
RADIO_PROGRAMME_CACHE_MAX_ENTRIES = 1024


def radio_station_identity(owner_identity: str, provider_key: str, provider_station_id: str) -> MediaSourceIdentity:
    return MediaSourceIdentity(
        RADIO_DOMAIN,
        owner_identity,
        f"{provider_key.strip().lower()}:{provider_station_id.strip()}",
    )


def radio_source_identity(
    owner_identity: str,
    provider_key: str,
    provider_station_id: str,
    source_discriminator: str = "",
) -> MediaSourceIdentity:
    base = f"{provider_key.strip().lower()}:{provider_station_id.strip()}"
    discriminator = source_discriminator.strip()
    if discriminator:
        base = f"{base}:{discriminator}"
    return MediaSourceIdentity(RADIO_DOMAIN, owner_identity, base)


def radio_source_id(
    owner_identity: str,
    provider_key: str,
    provider_station_id: str,
    source_discriminator: str = "",
) -> str:
    return media_source_id_for(radio_source_identity(
        owner_identity, provider_key, provider_station_id, source_discriminator,
    ))


def radio_station_id(owner_identity: str, provider_key: str, provider_station_id: str) -> str:
    # Keep the station container visibly in the Radio namespace while deriving
    # it from the same non-URL identity material as its source.
    return f"radio_station_{radio_source_id(owner_identity, provider_key, provider_station_id)[4:]}"


def _error_text(error: BaseException) -> str:
    code = str(getattr(error, "code", "") or "").strip()
    category = str(getattr(error, "category", "") or "").strip()
    message = str(getattr(error, "message", "") or str(error)).replace("\x00", "").strip()
    prefix = ": ".join(value for value in (code, category) if value)
    text = f"{prefix}: {message}" if prefix else message
    return text.replace("\r", " ").replace("\n", " ")[:2048] or error.__class__.__name__


class RadioCatalogBridge:
    """Validate and atomically project one Plugin Radio catalog."""

    async def refresh(
        self,
        owner_identity: str,
        catalog: dict[str, Any],
        *,
        owned_schemes: set[str] | frozenset[str],
        now: float | None = None,
        generation: int | None = None,
    ) -> dict:
        if generation is None:
            generation = await database.begin_radio_catalog_refresh(owner_identity)
        normalized = validate_radio_catalog(catalog, owned_schemes=owned_schemes)
        now_value = float(now if now is not None else time.time())
        rows: list[dict[str, Any]] = []
        for item in normalized["stations"]:
            ref = dict(item["station_ref"])
            provider_key = ref["provider_key"].strip().lower()
            provider_station_id = ref["provider_station_id"].strip()
            source_discriminator = str(item.get("source_discriminator") or "").strip()
            source_id = radio_source_id(
                owner_identity, provider_key, provider_station_id, source_discriminator,
            )
            station_id = radio_station_id(owner_identity, provider_key, provider_station_id)
            playback_config = dict(item.get("playback_config") or {})
            source_revision = media_source_revision_for({
                "domain": RADIO_DOMAIN,
                "owner": owner_identity,
                "provider_key": provider_key,
                "provider_station_id": provider_station_id,
                "source_discriminator": source_discriminator,
                "reference": ref,
                "playback_config": playback_config,
            })
            ttl = int(item.get("ttl_seconds", 300))
            rows.append({
                "station_id": station_id,
                "source_id": source_id,
                "owner_identity": owner_identity,
                "provider_key": provider_key,
                "provider_station_id": provider_station_id,
                "source_discriminator": source_discriminator,
                "name": item["name"],
                "logo_url": item.get("logo_url", ""),
                "group_name": item.get("group_name", ""),
                "country": item.get("country", ""),
                "language": item.get("language", ""),
                "frequency": item.get("frequency", ""),
                "metadata": dict(item.get("metadata") or {}),
                "reference": {
                    "station_ref": ref,
                    "playback_config": playback_config,
                    "source_discriminator": source_discriminator,
                },
                "source_revision": source_revision,
                "explicit_priority": int(item.get("priority", 0)),
                "ttl_seconds": ttl,
                "catalog_expires_at": now_value + ttl,
            })
        result = await database.apply_radio_catalog(
            owner_identity,
            rows,
            now_unix=now_value,
            stale_grace_seconds=RADIO_CATALOG_STALE_GRACE_SECONDS,
            generation=generation,
        )
        if result.get("status") == "success":
            await database.prune_radio_catalog(
                owner_identity, now_unix=now_value,
                stale_grace_seconds=RADIO_CATALOG_STALE_GRACE_SECONDS,
            )
        return result

    async def record_failure(
        self, owner_identity: str, error: BaseException, *, generation: int | None = None,
    ) -> None:
        await database.record_radio_catalog_failure(
            owner_identity, _error_text(error), generation=generation,
        )


class RadioResolver:
    """Resolve a persisted Radio source through the Plugin runtime only."""

    def __init__(self, *, runtime=None, clock=time.time):
        self.runtime = runtime
        self.clock = clock
        self._descriptor_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any], Any]] = {}
        self._programme_cache: dict[tuple[str, str], tuple[float, dict[str, Any], Any]] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._programme_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _prune_caches(self, now: float | None = None) -> None:
        current = float(self.clock() if now is None else now)
        for cache, limit in (
            (self._descriptor_cache, RADIO_DESCRIPTOR_CACHE_MAX_ENTRIES),
            (self._programme_cache, RADIO_PROGRAMME_CACHE_MAX_ENTRIES),
        ):
            for key, value in list(cache.items()):
                if value[0] <= current:
                    cache.pop(key, None)
            if len(cache) > limit:
                for key, _value in sorted(cache.items(), key=lambda item: item[1][0])[:len(cache) - limit]:
                    cache.pop(key, None)
        for key, lock in list(self._locks.items()):
            if key not in self._descriptor_cache and not lock.locked():
                self._locks.pop(key, None)
        for key, lock in list(self._programme_locks.items()):
            if key not in self._programme_cache and not lock.locked():
                self._programme_locks.pop(key, None)

    def _lock_for(self, key: tuple[str, str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _programme_lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._programme_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._programme_locks[key] = lock
        return lock

    def _active_owner(self, source: dict, expected=None):
        if self.runtime is None:
            raise PluginError("PLUGIN_UNAVAILABLE", "Radio Plugin runtime is unavailable", category="lifecycle")
        provider_key = str(source.get("provider_key") or "").strip().lower()
        instance = self.runtime.registry.route(provider_key)
        if instance.manifest.identity != str(source.get("owner_identity") or ""):
            raise PluginError("SCHEME_CONFLICT", "Radio source owner does not match the active Plugin", category="routing")
        owned = {scheme for scheme, contract in instance.manifest.owned_schemes if contract == "radio_provider"}
        if provider_key not in owned:
            raise PluginError("SCHEME_CONFLICT", "Active Plugin does not own this Radio scheme", category="routing")
        if expected is not None and (
            instance is not expected[0] or getattr(instance, "process", None) is not expected[1]
        ):
            raise PluginError("PLUGIN_CANDIDATE_CONFLICT", "Radio Plugin changed during resolve", category="lifecycle")
        return instance

    async def resolve_source(self, source_id: str, *, station_id: str = "") -> dict[str, Any]:
        self._prune_caches()
        source = await database.get_radio_station_source(
            source_id, station_id=station_id, now_unix=float(self.clock()),
        )
        if source is None or source.get("lifecycle_state") == "expired":
            raise PluginError("RESOURCE_NOT_FOUND", "Radio source is unavailable", category="routing")
        instance = self._active_owner(source)
        owner = (instance, getattr(instance, "process", None))
        provider_key = str(source.get("provider_key") or "").strip().lower()
        revision = str(source.get("source_revision") or "")
        key = (RADIO_DOMAIN, str(source_id), revision)
        now = float(self.clock())
        cached = self._descriptor_cache.get(key)
        if cached and cached[0] > now and cached[2][0] is owner[0] and cached[2][1] is owner[1]:
            return copy.deepcopy(cached[1])

        async with self._lock_for(key):
            self._active_owner(source, owner)
            now = float(self.clock())
            cached = self._descriptor_cache.get(key)
            if cached and cached[0] > now and cached[2][0] is owner[0] and cached[2][1] is owner[1]:
                return copy.deepcopy(cached[1])
            try:
                reference = source.get("reference") or {}
                station_ref = reference.get("station_ref") if isinstance(reference, dict) else None
                if not isinstance(station_ref, dict):
                    raise PluginError("INVALID_PLUGIN_RESPONSE", "Persisted Radio reference is invalid", category="routing")
                playback_config = reference.get("playback_config") if isinstance(reference, dict) else None
                if not isinstance(playback_config, dict):
                    playback_config = {}
                descriptor = await self.runtime.request(
                    instance, "radio.resolve_stream", {
                        "station_ref": station_ref,
                        "playback_config": playback_config,
                    },
                )
                current = await database.get_radio_station_source(
                    source_id, station_id=source.get("station_id") or station_id,
                    now_unix=float(self.clock()),
                )
                if current is None or str(current.get("source_revision") or "") != revision:
                    raise PluginError(
                        "PLUGIN_CANDIDATE_CONFLICT", "Radio source changed during resolve", category="routing",
                    )
                self._active_owner(current, owner)
                # Keep the Radio projection aligned with the existing TV
                # descriptor bridge. Domain/source fields are additive routing
                # context; transport and generic metadata use one validator.
                bridged = ProviderResolver._bridge_descriptor(provider_key, descriptor)
                bridged.update({
                    "domain": RADIO_DOMAIN,
                    "station_id": source.get("station_id"),
                    "source_id": source_id,
                    "source_revision": revision,
                })
                ttl = descriptor.get("ttl_seconds")
                cache_ttl = (
                    int(ttl) if isinstance(ttl, int) and ttl > 0
                    else RADIO_DESCRIPTOR_CACHE_DEFAULT_TTL_SECONDS if ttl is None else 0
                )
                expires_at = now + cache_ttl if cache_ttl else now
                await database.update_radio_source_health(
                    source_id, expected_source_revision=revision,
                    success=True, resolve_expires_at=expires_at,
                )
                self._active_owner(current, owner)
                if cache_ttl:
                    self._descriptor_cache[key] = (expires_at, copy.deepcopy(bridged), owner)
                return bridged
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await database.update_radio_source_health(
                    source_id, expected_source_revision=revision,
                    success=False, error=_error_text(exc),
                )
                raise

    async def resolve_programme(self, source_id: str, *, station_id: str = "") -> dict[str, Any]:
        """Resolve a provider-native Radio programme snapshot.

        This is deliberately a Radio projection and never touches TV EPG
        tables, matchers, or bindings.  The persisted source revision is part
        of the cache key so a playback-config update cannot reuse an older
        provider snapshot.
        """
        self._prune_caches()
        source = await database.get_radio_station_source(
            source_id, station_id=station_id, now_unix=float(self.clock()),
        )
        if source is None or source.get("lifecycle_state") == "expired":
            raise PluginError("RESOURCE_NOT_FOUND", "Radio source is unavailable", category="routing")
        instance = self._active_owner(source)
        owner = (instance, getattr(instance, "process", None))
        provider_key = str(source.get("provider_key") or "").strip().lower()
        revision = str(source.get("source_revision") or "")
        key = (str(source_id), revision)
        now = float(self.clock())
        cached = self._programme_cache.get(key)
        if cached and cached[0] > now and cached[2][0] is owner[0] and cached[2][1] is owner[1]:
            return copy.deepcopy(cached[1])
        durable = await database.get_radio_programme_snapshot(source_id, now_unix=now)
        self._active_owner(source, owner)
        if durable and str(durable.get("source_revision") or "") == revision:
            result = {
                "domain": RADIO_DOMAIN,
                "station_id": source.get("station_id"),
                "source_id": source_id,
                "source_revision": revision,
                "station_ref": {
                    "provider_key": durable.get("provider_key") or provider_key,
                    "provider_station_id": durable.get("provider_station_id") or source.get("provider_station_id"),
                },
                "revision": durable.get("revision") or "",
                "programmes": durable.get("programmes") or [],
                "expires_at": durable.get("expires_at_unix"),
            }
            self._programme_cache[key] = (float(durable["expires_at_unix"]), copy.deepcopy(result), owner)
            return result

        async with self._programme_lock_for(key):
            self._active_owner(source, owner)
            now = float(self.clock())
            cached = self._programme_cache.get(key)
            if cached and cached[0] > now and cached[2][0] is owner[0] and cached[2][1] is owner[1]:
                return copy.deepcopy(cached[1])
            try:
                reference = source.get("reference") or {}
                station_ref = reference.get("station_ref") if isinstance(reference, dict) else None
                if not isinstance(station_ref, dict):
                    raise PluginError("INVALID_PLUGIN_RESPONSE", "Persisted Radio reference is invalid", category="routing")
                playback_config = reference.get("playback_config") if isinstance(reference, dict) else None
                if not isinstance(playback_config, dict):
                    playback_config = {}
                snapshot = await self.runtime.request(
                    instance, "radio.programme", {
                        "station_ref": station_ref,
                        "playback_config": playback_config,
                    },
                )
                returned_ref = snapshot.get("station_ref") or {}
                if (
                    str(returned_ref.get("provider_key") or "").lower() != provider_key
                    or str(returned_ref.get("provider_station_id") or "") != str(station_ref.get("provider_station_id") or "")
                ):
                    raise PluginError(
                        "INVALID_PLUGIN_RESPONSE", "Radio programme station identity changed", category="routing",
                    )
                current = await database.get_radio_station_source(
                    source_id, station_id=station_id, now_unix=float(self.clock()),
                )
                if current is None or str(current.get("source_revision") or "") != revision:
                    raise PluginError(
                        "PLUGIN_CANDIDATE_CONFLICT", "Radio source changed during programme resolve", category="routing",
                    )
                self._active_owner(current, owner)
                item_expiries = [
                    int(item["expires_at"])
                    for item in snapshot.get("programmes", [])
                    if isinstance(item.get("expires_at"), int) and item["expires_at"] > int(now)
                ]
                expires_at = min(item_expiries) if item_expiries else int(now) + RADIO_PROGRAMME_CACHE_DEFAULT_TTL_SECONDS
                ttl = max(1, min(7 * 24 * 60 * 60, int(expires_at - now)))
                persisted = await database.upsert_radio_programme_snapshot(
                    current, snapshot, updated_at_unix=now, ttl_seconds=ttl,
                )
                self._active_owner(current, owner)
                result = {
                    "domain": RADIO_DOMAIN,
                    "station_id": current.get("station_id"),
                    "source_id": source_id,
                    "source_revision": revision,
                    "station_ref": snapshot["station_ref"],
                    "revision": snapshot["revision"],
                    "programmes": snapshot["programmes"],
                    "expires_at": persisted["expires_at_unix"],
                }
                self._programme_cache[key] = (float(persisted["expires_at_unix"]), copy.deepcopy(result), owner)
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise exc

    def invalidate(self, source_id: str, source_revision: str | None = None) -> None:
        keys = [key for key in self._descriptor_cache if key[1] == source_id and (source_revision is None or key[2] == source_revision)]
        for key in keys:
            self._descriptor_cache.pop(key, None)
        programme_keys = [
            key for key in self._programme_cache
            if key[0] == source_id and (source_revision is None or key[1] == source_revision)
        ]
        for key in programme_keys:
            self._programme_cache.pop(key, None)
        self._prune_caches()
