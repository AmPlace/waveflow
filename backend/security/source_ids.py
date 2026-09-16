"""Opaque media source identifiers.

``source_id`` identifies one logical source within channel playback surfaces.
It is derived from original source configuration only; resolved adapter URLs,
dynamic tokens, probe status, latency, and credentials are intentionally
excluded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

_CONFIG_FIELDS = (
    "url",
    "source_type",
    "custom_ua",
    "referer",
    "force_proxy",
    "requires_headers",
    "requires_proxy_declared",
    "proxy_required_hint",
    "adapter_provider",
    "adapter",
    "adapter_source_url",
    "rtsp_timestamp_mode",
)


def _source_id_key() -> bytes:
    from security.secrets import SOURCE_ID_PURPOSE, derive_key

    return derive_key(SOURCE_ID_PURPOSE)

_MARKET_FIELDS = (
    "market_package_id",
    "market_source_id",
    "market_channel_id",
    "market_source_item_id",
)


@dataclass(frozen=True)
class MediaSourceIdentity:
    """Stable identity for a source in a media domain.

    ``domain`` is part of the identity material rather than a naming prefix.
    This keeps IPTV and Radio namespaces separate even when an owner uses the
    same external identifier in both domains.  A resolved URL is deliberately
    not represented here; providers may rotate it without changing the
    durable source identity.
    """

    domain: str
    owner: str
    external_id: str

    def __post_init__(self) -> None:
        for field in ("domain", "owner", "external_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")

    def material(self) -> dict[str, str]:
        return {
            "domain": self.domain.strip().lower(),
            "owner": self.owner.strip(),
            "external_id": self.external_id.strip(),
        }


def media_source_id_for(identity: MediaSourceIdentity) -> str:
    """Return an opaque stable ID for any explicitly named media domain."""

    encoded = json.dumps(identity.material(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(_source_id_key(), encoded, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest[:18]).rstrip(b"=").decode("ascii")
    return f"src_{token}"


def media_source_revision_for(configuration: Any) -> str:
    """Fingerprint playback configuration without including volatile URLs."""

    try:
        encoded = json.dumps(
            configuration, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("media source configuration must be JSON serializable") from exc
    return hashlib.sha256(encoded).hexdigest()[:24]


def _clean(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def source_config_fingerprint(source: dict[str, Any]) -> str:
    """Stable fingerprint for playback-relevant original source config."""

    payload = {field: _clean(source.get(field)) for field in _CONFIG_FIELDS}
    from rtsp_playback import normalize_rtsp_timestamp_mode
    payload["rtsp_timestamp_mode"] = normalize_rtsp_timestamp_mode(
        source.get("rtsp_timestamp_mode")
    )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_identity_material(source: dict[str, Any]) -> dict[str, Any]:
    """Return non-secret source identity material before HMAC opacity."""

    subscription_id = str(source.get("subscription_id") or "").strip()
    row_id = str(source.get("id") or source.get("db_channel_id") or "").strip()
    if subscription_id and row_id:
        return {
            "kind": "db",
            "subscription_id": subscription_id,
            "channel_row_id": row_id,
        }

    market = {field: str(source.get(field) or "").strip() for field in _MARKET_FIELDS}
    if market.get("market_source_item_id", "").startswith("auto-"):
        market["market_source_item_id"] = ""
    if any(market.values()):
        return {
            "kind": "market",
            **market,
            "config_fp": source_config_fingerprint(source),
        }

    return {
        "kind": "config",
        "config_fp": source_config_fingerprint(source),
    }


def source_id_for(source: dict[str, Any]) -> str:
    """Generate an opaque stable ``src_xxx`` id for a source."""

    material = source_identity_material(source)
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(_source_id_key(), encoded, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest[:18]).rstrip(b"=").decode("ascii")
    return f"src_{token}"


def source_revision_for(source: dict[str, Any]) -> str:
    """Internal config revision for cache invalidation.

    Unlike ``source_id``, this is expected to change when playback-affecting
    source config changes. It is not a public selector.
    """

    return source_config_fingerprint(source)[:24]
