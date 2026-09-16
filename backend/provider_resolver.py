from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from plugin_runtime import PluginError, PluginRuntime
from provider_reference import (
    ProviderReferenceError,
    parse_provider_reference,
)


OWNERSHIP_MODES = frozenset({"legacy", "plugin", "migration_test"})

# Only these modes route to a Plugin.  The database keeps a
# CHECK(mode IN ('legacy', 'plugin', 'migration_test')) constraint, so "legacy"
# stays storable for data compatibility, but it is deliberately non-routable:
# Core no longer ships any provider adapter to fall back to.
ROUTABLE_MODES = frozenset({"plugin", "migration_test"})

# Returned by ``mode()`` when no ownership row exists.  It is never persisted.
UNOWNED_MODE = "unowned"


@dataclass(frozen=True)
class TVReferenceV1:
    raw_url: str
    scheme: str
    resource_id: str
    query: dict[str, list[str]]


def parse_tv_reference(value: str) -> TVReferenceV1:
    reference = parse_provider_reference(value)
    return TVReferenceV1(
        reference.raw_url,
        reference.scheme,
        reference.resource_id,
        reference.query,
    )


class ProviderResolver:
    """Resolve a source reference through Plugin ownership only.

    There is no Core provider fallback.  A scheme without Plugin ownership, or
    whose Plugin is missing, disabled or unhealthy, fails closed.
    """

    def __init__(
        self, *, runtime: PluginRuntime | None,
        ownership: dict[str, str] | None = None,
    ):
        self.runtime = runtime
        self._ownership = {str(k).lower(): str(v) for k, v in (ownership or {}).items()}
        self._expected_plugins: dict[str, str] = {}
        self._unavailable: set[str] = set()
        if any(mode not in OWNERSHIP_MODES for mode in self._ownership.values()):
            raise ValueError("invalid provider ownership mode")

    @classmethod
    def from_ownership_rows(
        cls,
        rows: list[dict[str, Any]],
        *,
        runtime: PluginRuntime | None,
    ) -> "ProviderResolver":
        resolver = cls(runtime=runtime)
        for row in rows:
            mode = str(row.get("mode") or "legacy")
            resolver.set_mode(
                str(row.get("scheme") or ""),
                mode,
                str(row.get("plugin_identity") or ""),
                # No mode is routable on construction.  Plugin ownership only
                # becomes routable after production recovery proves the
                # matching installation/runtime healthy.
                available=False,
            )
        return resolver

    def mode(self, scheme: str) -> str:
        """Stored ownership mode, or ``UNOWNED_MODE`` when nothing owns it."""
        return self._ownership.get(str(scheme).lower(), UNOWNED_MODE)

    def set_mode(
        self, scheme: str, mode: str, plugin_identity: str = "", *, available: bool = True,
    ) -> None:
        if mode not in OWNERSHIP_MODES:
            raise ValueError("invalid provider ownership mode")
        normalized = scheme.lower()
        self._ownership[normalized] = mode
        if mode == "legacy":
            self._expected_plugins.pop(normalized, None)
        elif plugin_identity:
            self._expected_plugins[normalized] = plugin_identity
        if available:
            self._unavailable.discard(normalized)
        else:
            self._unavailable.add(normalized)

    def forget(self, scheme: str) -> None:
        """Drop any projected ownership so the scheme reads as ``UNOWNED_MODE``.

        Used when the durable store holds no row for the scheme: there is no
        Plugin owner and no Core adapter to fall back to.
        """
        normalized = str(scheme).lower()
        self._ownership.pop(normalized, None)
        self._expected_plugins.pop(normalized, None)
        self._unavailable.discard(normalized)

    def fail_closed(self, scheme: str) -> None:
        self._unavailable.add(str(scheme).lower())

    def mark_available(self, scheme: str) -> None:
        self._unavailable.discard(str(scheme).lower())

    def is_available(self, scheme: str) -> bool:
        return str(scheme).lower() not in self._unavailable

    async def resolve(
        self,
        target_url: str,
        client: httpx.AsyncClient,
        *,
        source_id: str = "",
        source_revision: str = "",
    ) -> dict[str, Any]:
        reference = parse_tv_reference(target_url)
        if reference.scheme in self._unavailable:
            raise PluginError(
                "PLUGIN_UNAVAILABLE", "Provider ownership is reconciling", category="lifecycle",
            )
        mode = self.mode(reference.scheme)
        if mode not in ROUTABLE_MODES:
            raise PluginError(
                "PLUGIN_UNAVAILABLE",
                f"No Plugin owns provider '{reference.scheme}'",
                category="routing",
            )
        if self.runtime is None:
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin subsystem is unavailable", category="lifecycle")
        instance = self.runtime.registry.route(reference.scheme)
        expected = self._expected_plugins.get(reference.scheme)
        if expected and instance.manifest.identity != expected:
            raise PluginError("SCHEME_CONFLICT", "Configured Plugin does not own this scheme", category="routing")
        descriptor = await self.runtime.request(instance, "tv.resolve_stream", {
            "reference_version": "1.0",
            "scheme": reference.scheme,
            "resource_id": reference.resource_id,
            "query": reference.query,
            "raw_reference": reference.raw_url,
            "source_id": source_id,
            "source_revision": source_revision,
        })
        result = self._bridge_descriptor(reference.scheme, descriptor)
        return self._bind_source_identity(result, source_id=source_id, source_revision=source_revision)

    @staticmethod
    def _bind_source_identity(
        result: dict[str, Any], *, source_id: str = "", source_revision: str = "",
    ) -> dict[str, Any]:
        bound = dict(result)
        if source_id:
            bound["source_id"] = source_id
        if source_revision:
            bound["source_revision"] = source_revision
        return bound

    def supports_visual_metadata(self, target_url: str) -> bool:
        """Return whether the active Plugin owns the optional visual feature.

        This is a capability check only.  It never invokes a provider and it
        deliberately returns false for non-Plugin ownership or unavailable
        runtimes, so unsupported sources are not probed as a side effect of a
        Home render.
        """
        try:
            reference = parse_tv_reference(target_url)
            if self.mode(reference.scheme) not in ROUTABLE_MODES or self.runtime is None:
                return False
            instance = self.runtime.registry.route(reference.scheme)
            expected = self._expected_plugins.get(reference.scheme)
            if expected and instance.manifest.identity != expected:
                return False
            contract = next(
                (item for item in instance.manifest.provider_contracts if item.contract == "tv_visual_provider"),
                None,
            )
            return bool(contract and "metadata" in contract.features and reference.scheme in contract.schemes)
        except (ProviderReferenceError, PluginError, ValueError):
            return False

    async def visual_metadata(
        self,
        target_url: str,
        *,
        source_id: str = "",
        source_revision: str = "",
    ) -> dict[str, Any]:
        """Resolve optional source visual metadata through the active Plugin."""
        reference = parse_tv_reference(target_url)
        if self.mode(reference.scheme) not in ROUTABLE_MODES:
            raise PluginError("RESOURCE_NOT_FOUND", "Source has no Plugin visual metadata", category="request")
        if self.runtime is None:
            raise PluginError("PLUGIN_UNAVAILABLE", "Plugin subsystem is unavailable", category="lifecycle")
        instance = self.runtime.registry.route(reference.scheme)
        expected = self._expected_plugins.get(reference.scheme)
        if expected and instance.manifest.identity != expected:
            raise PluginError("SCHEME_CONFLICT", "Configured Plugin does not own this scheme", category="routing")
        contract = next(
            (item for item in instance.manifest.provider_contracts if item.contract == "tv_visual_provider"),
            None,
        )
        if contract is None or "metadata" not in contract.features or reference.scheme not in contract.schemes:
            raise PluginError("RESOURCE_NOT_FOUND", "Plugin does not implement TV visual metadata", category="request")
        return await self.runtime.request(instance, "tv.visual_metadata", {
            "reference_version": "1.0",
            "scheme": reference.scheme,
            "resource_id": reference.resource_id,
            "query": reference.query,
            "raw_reference": reference.raw_url,
            "source_id": source_id,
            "source_revision": source_revision,
        })

    @staticmethod
    def _bridge_descriptor(scheme: str, descriptor: dict[str, Any]) -> dict[str, Any]:
        transport = str(descriptor.get("transport") or "hls")
        result = {
            "ok": True,
            "adapter": scheme,
            "url": descriptor.get("url") or "",
            "source_type": transport,
            "direct_playable": not bool(descriptor.get("requires_proxy")),
            "requires_proxy": bool(descriptor.get("requires_proxy")),
            "headers": dict(descriptor.get("headers") or {}),
            "ttl": descriptor.get("ttl_seconds"),
            "expires_at": descriptor.get("expires_at"),
            "volatile_url": bool(descriptor.get("volatile_url")),
            "warnings": list(descriptor.get("warnings") or []),
            "stream_descriptor_version": descriptor.get("descriptor_version"),
        }
        # These are validated, JSON-safe descriptor extensions.  Project them
        # only when present so descriptors without metadata retain the exact
        # historical Core result shape.
        for field in (
            "credential_refs", "quality_variants", "drm", "encryption", "probe_hints",
            "refresh", "provider_diagnostics", "proxy_reasons", "referer", "origin", "user_agent",
        ):
            if field in descriptor:
                result[field] = descriptor[field]
        return result
