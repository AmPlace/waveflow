from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class TVReference:
    scheme: str
    resource_id: str
    query: dict[str, list[str]] = field(default_factory=dict)
    raw_reference: str = ""
    reference_version: str = "1.0"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TVReference":
        query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
        return cls(str(payload.get("scheme") or ""), str(payload.get("resource_id") or ""),
                   {str(k): [str(v) for v in values] for k, values in query.items() if isinstance(values, list)},
                   str(payload.get("raw_reference") or ""), str(payload.get("reference_version") or "1.0"))


@dataclass(frozen=True)
class RadioReference:
    provider_key: str
    provider_station_id: str
    playback_config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RadioReference":
        station = payload.get("station_ref") if isinstance(payload.get("station_ref"), dict) else payload
        config = payload.get("playback_config")
        if not isinstance(config, dict):
            config = {}
        return cls(str(station.get("provider_key") or ""), str(station.get("provider_station_id") or ""), dict(config))


@dataclass(frozen=True)
class ResolveContext:
    request_id: str
    deadline_unix_ms: int
    metadata: dict[str, Any]
    capabilities: Any
    _is_cancelled: Callable[[], bool] = field(default=lambda: False, repr=False)

    @property
    def cancelled(self) -> bool:
        return self._is_cancelled()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            from .errors import PluginError
            raise PluginError("PLUGIN_CANCELLED", "Provider request was cancelled", category="lifecycle")


@dataclass(frozen=True)
class ChannelCatalogItem:
    external_id: str
    name: str
    reference: str
    kind: str = "channel"
    group: str | None = None
    logo: str | None = None
    starts_at: int | None = None
    ends_at: int | None = None
    ttl_seconds: int = 300
    metadata: dict[str, Any] | None = None

    def as_contract(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "external_id": self.external_id,
            "name": self.name,
            "reference": self.reference,
            "kind": self.kind,
            "ttl_seconds": self.ttl_seconds,
        }
        for field in ("group", "logo", "starts_at", "ends_at", "metadata"):
            item = getattr(self, field)
            if item is not None:
                value[field] = dict(item) if field == "metadata" else item
        return value


@dataclass(frozen=True)
class ChannelCatalog:
    items: tuple[ChannelCatalogItem, ...]

    def as_contract(self) -> dict[str, Any]:
        return {"items": [item.as_contract() for item in self.items]}


@dataclass(frozen=True)
class StreamDescriptor:
    url: str
    transport: str = "hls"
    headers: dict[str, str] = field(default_factory=dict)
    credential_refs: list[str] = field(default_factory=list)
    ttl_seconds: int | None = None
    expires_at: int | None = None
    volatile_url: bool = True
    requires_proxy: bool = False
    warnings: list[str] = field(default_factory=list)
    # Generic data-only extension points.  The runtime validates JSON shape
    # and bounds at the Plugin boundary; the SDK does not assign provider-
    # specific meaning to either field.
    provider_diagnostics: dict[str, Any] = field(default_factory=dict)
    probe_hints: dict[str, Any] | None = None

    @property
    def direct_playable(self) -> bool:
        return not self.requires_proxy

    def as_contract(self) -> dict[str, Any]:
        value = {"descriptor_version": "1.0", "transport": self.transport, "url": self.url,
                 "headers": dict(self.headers), "credential_refs": list(self.credential_refs),
                 "ttl_seconds": self.ttl_seconds, "expires_at": self.expires_at,
                 "volatile_url": self.volatile_url, "requires_proxy": self.requires_proxy,
                 "warnings": list(self.warnings)}
        if self.provider_diagnostics:
            value["provider_diagnostics"] = dict(self.provider_diagnostics)
        if self.probe_hints is not None:
            value["probe_hints"] = dict(self.probe_hints)
        return value

    @classmethod
    def hls(cls, url: str, **kwargs: Any) -> "StreamDescriptor":
        return cls(url=url, transport="hls", **kwargs)

    @classmethod
    def dash(cls, url: str, **kwargs: Any) -> "StreamDescriptor":
        return cls(url=url, transport="dash", **kwargs)

    @classmethod
    def flv(cls, url: str, **kwargs: Any) -> "StreamDescriptor":
        return cls(url=url, transport="http_flv", **kwargs)

    @classmethod
    def rtsp(cls, url: str, **kwargs: Any) -> "StreamDescriptor":
        return cls(url=url, transport="rtsp", **kwargs)


@dataclass(frozen=True)
class VisualMetadata:
    """Optional source-scoped TV visual metadata, separate from playback."""

    avatar_url: str = ""
    # Stable room/channel art and volatile live art are separate generic
    # fields.  ``cover_url`` remains for compatibility with existing Plugin
    # artifacts that predate the distinction.
    stable_cover_url: str = ""
    dynamic_cover_url: str = ""
    cover_url: str = ""
    is_live: bool = False
    title: str = ""
    owner_name: str = ""
    ttl_seconds: int = 300
    cover_role: str = "live"

    def as_contract(self) -> dict[str, Any]:
        return {
            "visual_version": "1.0",
            "avatar_url": self.avatar_url,
            "stable_cover_url": self.stable_cover_url,
            "dynamic_cover_url": self.dynamic_cover_url,
            "cover_url": self.cover_url,
            "is_live": self.is_live,
            "title": self.title,
            "owner_name": self.owner_name,
            "ttl_seconds": self.ttl_seconds,
            "cover_role": self.cover_role,
        }
