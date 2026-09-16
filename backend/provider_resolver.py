from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from adapters import AdapterRequest, AdapterResolveError, parse_adapter_url, resolve_adapter_source
from plugin_runtime import PluginError, PluginRuntime


OWNERSHIP_MODES = frozenset({"legacy", "plugin", "migration_test"})


@dataclass(frozen=True)
class TVReferenceV1:
    raw_url: str
    scheme: str
    resource_id: str
    query: dict[str, list[str]]


def parse_tv_reference(value: str) -> TVReferenceV1:
    raw = str(value or "").strip()
    try:
        legacy = parse_adapter_url(raw)
    except AdapterResolveError:
        parsed = urlparse(raw)
        scheme = parsed.scheme.lower()
        resource = (parsed.netloc + parsed.path).strip("/")
        if not scheme or not resource:
            raise
        return TVReferenceV1(raw, scheme, resource, parse_qs(parsed.query, keep_blank_values=False))
    return TVReferenceV1(legacy.raw_url, legacy.adapter, legacy.resource_id, legacy.query)


class ProviderResolver:
    def __init__(
        self, *, runtime: PluginRuntime | None,
        ownership: dict[str, str] | None = None,
        legacy_resolver: Callable[[str, httpx.AsyncClient], Awaitable[dict[str, Any]]] = resolve_adapter_source,
    ):
        self.runtime = runtime
        self.legacy_resolver = legacy_resolver
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
        legacy_resolver: Callable[[str, httpx.AsyncClient], Awaitable[dict[str, Any]]] = resolve_adapter_source,
    ) -> "ProviderResolver":
        resolver = cls(runtime=runtime, legacy_resolver=legacy_resolver)
        for row in rows:
            mode = str(row.get("mode") or "legacy")
            resolver.set_mode(
                str(row.get("scheme") or ""),
                mode,
                str(row.get("plugin_identity") or ""),
                # Durable Plugin ownership is desired state.  It is only made
                # routable after production recovery proves the matching
                # installation/runtime healthy.  Legacy remains immediately
                # available and does not depend on the Plugin subsystem.
                available=mode == "legacy",
            )
        return resolver

    def mode(self, scheme: str) -> str:
        return self._ownership.get(scheme.lower(), "legacy")

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
        if mode == "legacy":
            result = await self.legacy_resolver(target_url, client)
            return self._bind_source_identity(result, source_id=source_id, source_revision=source_revision)
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
        deliberately returns false for legacy ownership or unavailable
        runtimes, so unsupported sources are not probed as a side effect of a
        Home render.
        """
        try:
            reference = parse_tv_reference(target_url)
            if self.mode(reference.scheme) != "plugin" or self.runtime is None:
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
        except (AdapterResolveError, PluginError, ValueError):
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
        if self.mode(reference.scheme) != "plugin":
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
