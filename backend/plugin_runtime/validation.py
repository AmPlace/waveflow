from __future__ import annotations

import json
import math
import re
from typing import Any
from urllib.parse import urlparse, urlsplit

from .errors import invalid_response


TRANSPORTS = frozenset({"hls", "dash", "http_flv", "mpegts", "rtsp", "audio_http", "probe_only"})
SECRET_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization", "x-api-key"})
DESCRIPTOR_FIELDS = frozenset({
    "descriptor_version", "transport", "url", "headers", "credential_refs", "ttl_seconds", "expires_at",
    "volatile_url", "requires_proxy", "warnings", "proxy_reasons", "referer", "origin", "user_agent",
    "quality_variants", "drm", "encryption", "probe_hints", "refresh", "provider_diagnostics",
})

# Provider diagnostics and probe hints are data-only extension points.  Keep
# their boundary independent from the frame limit so a valid descriptor can
# still carry the rest of its transport fields without allowing an unbounded
# metadata blob through the Plugin boundary.
DESCRIPTOR_METADATA_MAX_BYTES = 16 * 1024
DESCRIPTOR_METADATA_MAX_DEPTH = 5
DESCRIPTOR_METADATA_MAX_ITEMS = 64
DESCRIPTOR_METADATA_MAX_STRING_LENGTH = 2048
DESCRIPTOR_METADATA_MAX_KEY_LENGTH = 128
CHANNEL_CATALOG_MAX_ITEMS = 256
CHANNEL_CATALOG_MAX_BYTES = 256 * 1024
CHANNEL_CATALOG_MAX_EXTERNAL_ID_LENGTH = 256
CHANNEL_CATALOG_MAX_NAME_LENGTH = 200
CHANNEL_CATALOG_MAX_REFERENCE_LENGTH = 2048
CHANNEL_CATALOG_MAX_GROUP_LENGTH = 128
CHANNEL_CATALOG_MAX_LOGO_LENGTH = 2048
CHANNEL_CATALOG_MAX_TTL_SECONDS = 24 * 60 * 60
CHANNEL_CATALOG_KINDS = frozenset({"channel", "event"})
VISUAL_METADATA_MAX_TTL_SECONDS = 7 * 24 * 60 * 60
VISUAL_METADATA_MAX_URL_LENGTH = 4096
VISUAL_METADATA_MAX_TEXT_LENGTH = 512
VISUAL_METADATA_COVER_ROLES = frozenset({"stable", "live", "content"})
RADIO_CATALOG_MAX_ITEMS = 2048
RADIO_CATALOG_MAX_BYTES = 512 * 1024
RADIO_CATALOG_MAX_TTL_SECONDS = 7 * 24 * 60 * 60
RADIO_CATALOG_MAX_STRING_LENGTH = 2048
RADIO_PROGRAMME_MAX_ITEMS = 128
RADIO_PROGRAMME_MAX_BYTES = 128 * 1024
RADIO_PROGRAMME_MAX_STRING_LENGTH = 2048
_CATALOG_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*$")


def validate_descriptor_metadata(value: Any, *, field: str = "descriptor metadata") -> dict[str, Any]:
    """Validate a JSON-safe, bounded metadata object.

    Metadata is intentionally opaque to the runtime.  It is never interpreted
    as headers, commands, ownership, or capability authority.  Restricting
    the shape here keeps that opaque data safe to carry through IPC and into
    probe diagnostics.
    """
    if not isinstance(value, dict):
        raise invalid_response(f"Invalid {field}")

    active: set[int] = set()

    def walk(item: Any, depth: int) -> None:
        if depth > DESCRIPTOR_METADATA_MAX_DEPTH:
            raise invalid_response(f"{field} exceeds maximum depth")
        if isinstance(item, str):
            if len(item) > DESCRIPTOR_METADATA_MAX_STRING_LENGTH:
                raise invalid_response(f"{field} contains an oversized string")
            return
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise invalid_response(f"{field} contains a non-finite number")
            return
        if isinstance(item, dict):
            if len(item) > DESCRIPTOR_METADATA_MAX_ITEMS:
                raise invalid_response(f"{field} contains too many object members")
            identity = id(item)
            if identity in active:
                raise invalid_response(f"{field} contains a cycle")
            active.add(identity)
            try:
                for key, child in item.items():
                    if not isinstance(key, str) or len(key) > DESCRIPTOR_METADATA_MAX_KEY_LENGTH:
                        raise invalid_response(f"{field} contains an invalid object key")
                    walk(child, depth + 1)
            finally:
                active.remove(identity)
            return
        if isinstance(item, list):
            if len(item) > DESCRIPTOR_METADATA_MAX_ITEMS:
                raise invalid_response(f"{field} contains too many array items")
            identity = id(item)
            if identity in active:
                raise invalid_response(f"{field} contains a cycle")
            active.add(identity)
            try:
                for child in item:
                    walk(child, depth + 1)
            finally:
                active.remove(identity)
            return
        raise invalid_response(f"{field} contains a non-JSON value")

    walk(value, 1)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise invalid_response(f"Invalid {field}") from exc
    if len(encoded.encode("utf-8")) > DESCRIPTOR_METADATA_MAX_BYTES:
        raise invalid_response(f"{field} exceeds maximum size")
    return dict(value)


def validate_visual_metadata(value: Any) -> dict[str, Any]:
    """Validate the bounded, data-only TV visual metadata contract."""
    if not isinstance(value, dict):
        raise invalid_response("TV visual metadata must be an object")
    allowed = {
        "visual_version", "avatar_url", "stable_cover_url", "dynamic_cover_url", "cover_url",
        "is_live", "title", "owner_name",
        "ttl_seconds", "cover_role",
    }
    if set(value) - allowed or value.get("visual_version") != "1.0":
        raise invalid_response("TV visual metadata contains unsupported fields")
    for field in ("avatar_url", "stable_cover_url", "dynamic_cover_url", "cover_url"):
        raw = value.get(field, "")
        if not isinstance(raw, str) or len(raw) > VISUAL_METADATA_MAX_URL_LENGTH:
            raise invalid_response(f"Invalid TV visual {field}")
        if raw:
            try:
                if urlsplit(raw).scheme.lower() not in {"http", "https"}:
                    raise invalid_response(f"Invalid TV visual {field}")
            except ValueError as exc:
                raise invalid_response(f"Invalid TV visual {field}") from exc
    if not isinstance(value.get("is_live", False), bool):
        raise invalid_response("Invalid TV visual is_live")
    for field in ("title", "owner_name"):
        raw = value.get(field, "")
        if not isinstance(raw, str) or len(raw) > VISUAL_METADATA_MAX_TEXT_LENGTH:
            raise invalid_response(f"Invalid TV visual {field}")
    ttl = value.get("ttl_seconds")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1 or ttl > VISUAL_METADATA_MAX_TTL_SECONDS:
        raise invalid_response("Invalid TV visual TTL")
    role = value.get("cover_role", "live")
    if role not in VISUAL_METADATA_COVER_ROLES:
        raise invalid_response("Invalid TV visual cover role")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise invalid_response("TV visual metadata is not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > DESCRIPTOR_METADATA_MAX_BYTES:
        raise invalid_response("TV visual metadata exceeds the size limit")
    normalized = dict(value)
    normalized.setdefault("avatar_url", "")
    normalized.setdefault("stable_cover_url", "")
    normalized.setdefault("dynamic_cover_url", "")
    normalized.setdefault("cover_url", "")
    normalized.setdefault("is_live", False)
    normalized.setdefault("title", "")
    normalized.setdefault("owner_name", "")
    normalized.setdefault("cover_role", "live")
    return normalized


def validate_channel_catalog(value: Any, *, owned_schemes: set[str] | frozenset[str]) -> dict[str, Any]:
    """Validate a bounded runtime catalog owned by one Plugin instance.

    Catalog references are opaque provider references, not arbitrary network
    URLs.  Their scheme must be one of the Plugin's declared TV schemes; the
    existing ProviderResolver remains the authority that resolves them.
    """
    if not isinstance(value, dict) or set(value) != {"items"}:
        raise invalid_response("Channel catalog must contain only items")
    items = value["items"]
    if not isinstance(items, list) or len(items) > CHANNEL_CATALOG_MAX_ITEMS:
        raise invalid_response("Invalid channel catalog items")

    allowed = {str(scheme).lower() for scheme in owned_schemes}
    seen_external_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    allowed_fields = {
        "external_id", "name", "reference", "kind", "group", "logo",
        "starts_at", "ends_at", "ttl_seconds", "metadata",
    }
    for item in items:
        if not isinstance(item, dict) or not set(item).issubset(allowed_fields):
            raise invalid_response("Invalid channel catalog item")
        required = {"external_id", "name", "reference", "kind", "ttl_seconds"}
        if required - item.keys():
            raise invalid_response("Channel catalog item is missing required fields")
        external_id = item["external_id"]
        name = item["name"]
        reference = item["reference"]
        kind = item["kind"]
        if (not isinstance(external_id, str) or not external_id.strip()
                or len(external_id) > CHANNEL_CATALOG_MAX_EXTERNAL_ID_LENGTH):
            raise invalid_response("Invalid channel catalog external_id")
        if external_id in seen_external_ids:
            raise invalid_response("Duplicate channel catalog external_id")
        seen_external_ids.add(external_id)
        if not isinstance(name, str) or not name.strip() or len(name) > CHANNEL_CATALOG_MAX_NAME_LENGTH:
            raise invalid_response("Invalid channel catalog name")
        if not isinstance(reference, str) or not reference or len(reference) > CHANNEL_CATALOG_MAX_REFERENCE_LENGTH:
            raise invalid_response("Invalid channel catalog reference")
        try:
            parsed = urlsplit(reference)
        except ValueError as exc:
            raise invalid_response("Invalid channel catalog reference") from exc
        scheme = parsed.scheme.lower()
        if (not _CATALOG_SCHEME_RE.fullmatch(scheme) or scheme not in allowed
                or not (parsed.netloc or parsed.path.strip("/"))
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            raise invalid_response("Channel catalog reference scheme is not owned")
        if kind not in CHANNEL_CATALOG_KINDS:
            raise invalid_response("Invalid channel catalog kind")
        ttl = item["ttl_seconds"]
        if (not isinstance(ttl, int) or isinstance(ttl, bool)
                or ttl < 1 or ttl > CHANNEL_CATALOG_MAX_TTL_SECONDS):
            raise invalid_response("Invalid channel catalog TTL")
        for field, limit in (("group", CHANNEL_CATALOG_MAX_GROUP_LENGTH), ("logo", CHANNEL_CATALOG_MAX_LOGO_LENGTH)):
            if field in item and item[field] is not None:
                if not isinstance(item[field], str) or len(item[field]) > limit:
                    raise invalid_response(f"Invalid channel catalog {field}")
        logo = item.get("logo")
        if logo:
            try:
                logo_scheme = urlsplit(logo).scheme.lower()
            except ValueError as exc:
                raise invalid_response("Invalid channel catalog logo") from exc
            if logo_scheme not in {"http", "https"}:
                raise invalid_response("Invalid channel catalog logo")
        starts_at = item.get("starts_at")
        ends_at = item.get("ends_at")
        for field, timestamp in (("starts_at", starts_at), ("ends_at", ends_at)):
            if timestamp is not None and (not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0):
                raise invalid_response(f"Invalid channel catalog {field}")
        if starts_at is not None and ends_at is not None and ends_at < starts_at:
            raise invalid_response("Channel catalog event window is invalid")
        if "metadata" in item and item["metadata"] is not None:
            validate_descriptor_metadata(item["metadata"], field="channel catalog metadata")
        normalized.append(dict(item))

    try:
        encoded = json.dumps({"items": normalized}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise invalid_response("Channel catalog is not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > CHANNEL_CATALOG_MAX_BYTES:
        raise invalid_response("Channel catalog exceeds the size limit")
    return {"items": normalized}


def validate_radio_catalog(value: Any, *, owned_schemes: set[str] | frozenset[str]) -> dict[str, Any]:
    """Validate the bounded Radio V1 catalog without making Core storage public.

    The original Radio contract exposed ``stations`` and allowed providers to
    omit presentation fields.  Keep that wire compatibility while adding
    bounded fields used by the durable Radio projection.  ``owned_schemes``
    is the only authority for the provider key; no URL or metadata value can
    select another provider.
    """
    if not isinstance(value, dict) or not isinstance(value.get("stations"), list):
        raise invalid_response("Invalid Radio catalog")
    stations = value["stations"]
    if len(stations) > RADIO_CATALOG_MAX_ITEMS:
        raise invalid_response("Radio catalog contains too many stations")
    allowed_schemes = {str(scheme).lower() for scheme in owned_schemes}
    allowed_fields = {
        "station_ref", "name", "logo_url", "group_name", "country", "language",
        "frequency", "metadata", "playback_config", "source_discriminator",
        "ttl_seconds", "priority",
    }
    seen: set[tuple[str, str, str]] = set()
    normalized: list[dict[str, Any]] = []
    for station in stations:
        if not isinstance(station, dict) or not set(station).issubset(allowed_fields):
            raise invalid_response("Invalid Radio catalog station")
        ref = validate_station_ref(station.get("station_ref"))
        identity = (ref["provider_key"].strip().lower(), ref["provider_station_id"])
        if allowed_schemes and identity[0] not in allowed_schemes:
            raise invalid_response("Radio catalog station is not owned by this Plugin")
        discriminator = station.get("source_discriminator", "")
        if discriminator is None:
            discriminator = ""
        if not isinstance(discriminator, str) or len(discriminator) > 128:
            raise invalid_response("Invalid Radio catalog source discriminator")
        discriminator = discriminator.strip()
        source_identity = (*identity, discriminator)
        if source_identity in seen:
            raise invalid_response("Duplicate Radio catalog source identity")
        seen.add(source_identity)
        name = station.get("name", ref["provider_station_id"])
        if not isinstance(name, str) or not name.strip() or len(name) > RADIO_CATALOG_MAX_STRING_LENGTH:
            raise invalid_response("Invalid Radio catalog station name")
        ttl = station.get("ttl_seconds", 300)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1 or ttl > RADIO_CATALOG_MAX_TTL_SECONDS:
            raise invalid_response("Invalid Radio catalog TTL")
        priority = station.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0 or priority > 1_000_000:
            raise invalid_response("Invalid Radio catalog priority")
        item = dict(station)
        item["station_ref"] = ref
        item["source_discriminator"] = discriminator
        item["name"] = name.strip()
        item["ttl_seconds"] = ttl
        item["priority"] = priority
        for field in ("logo_url", "group_name", "country", "language", "frequency"):
            raw = item.get(field, "")
            if raw is None:
                raw = ""
            if not isinstance(raw, str) or len(raw) > RADIO_CATALOG_MAX_STRING_LENGTH:
                raise invalid_response(f"Invalid Radio catalog {field}")
            item[field] = raw
        if item["logo_url"]:
            try:
                if urlsplit(item["logo_url"]).scheme.lower() not in {"http", "https"}:
                    raise invalid_response("Invalid Radio catalog logo_url")
            except ValueError as exc:
                raise invalid_response("Invalid Radio catalog logo_url") from exc
        for field in ("metadata", "playback_config"):
            if field in item and item[field] is not None:
                validate_descriptor_metadata(item[field], field=f"Radio catalog {field}")
            elif field == "metadata":
                item[field] = {}
            else:
                item[field] = {}
        normalized.append(item)
    normalized_value = {"stations": normalized}
    if "next_cursor" in value:
        cursor = value["next_cursor"]
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512):
            raise invalid_response("Invalid Radio catalog cursor")
        normalized_value["next_cursor"] = cursor
    try:
        encoded = json.dumps(normalized_value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise invalid_response("Radio catalog is not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > RADIO_CATALOG_MAX_BYTES:
        raise invalid_response("Radio catalog exceeds the size limit")
    return normalized_value


def validate_radio_programme(value: Any, *, owned_schemes: set[str] | frozenset[str]) -> dict[str, Any]:
    """Validate bounded Radio programme snapshots separately from TV EPG."""
    if not isinstance(value, dict) or set(value) != {"station_ref", "revision", "programmes"}:
        raise invalid_response("Invalid Radio programme snapshot")
    ref = validate_station_ref(value.get("station_ref"))
    allowed = {str(scheme).lower() for scheme in owned_schemes}
    if allowed and ref["provider_key"].strip().lower() not in allowed:
        raise invalid_response("Radio programme station is not owned by this Plugin")
    revision = value["revision"]
    if not isinstance(revision, str) or len(revision) > 256:
        raise invalid_response("Invalid Radio programme revision")
    programmes = value["programmes"]
    if not isinstance(programmes, list) or len(programmes) > RADIO_PROGRAMME_MAX_ITEMS:
        raise invalid_response("Invalid Radio programme list")
    fields = {"provider_programme_id", "title", "start", "end", "description", "subtitle", "updated_at", "expires_at"}
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in programmes:
        if not isinstance(item, dict) or not set(item).issubset(fields) or "title" not in item or "updated_at" not in item:
            raise invalid_response("Invalid Radio programme item")
        title = item["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > RADIO_PROGRAMME_MAX_STRING_LENGTH:
            raise invalid_response("Invalid Radio programme title")
        provider_id = item.get("provider_programme_id", "")
        if not isinstance(provider_id, str) or len(provider_id) > RADIO_PROGRAMME_MAX_STRING_LENGTH or provider_id in seen:
            raise invalid_response("Invalid Radio programme provider identity")
        if provider_id:
            seen.add(provider_id)
        for field in ("description", "subtitle"):
            if field in item and item[field] is not None and (not isinstance(item[field], str) or len(item[field]) > RADIO_PROGRAMME_MAX_STRING_LENGTH):
                raise invalid_response(f"Invalid Radio programme {field}")
        for field in ("start", "end", "updated_at", "expires_at"):
            timestamp = item.get(field)
            if timestamp is not None and (isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0):
                raise invalid_response(f"Invalid Radio programme {field}")
        if item.get("start") is not None and item.get("end") is not None and item["end"] < item["start"]:
            raise invalid_response("Radio programme time range is invalid")
        normalized_item = dict(item)
        normalized_item.update({"title": title.strip(), "description": item.get("description", ""),
                                "subtitle": item.get("subtitle", ""), "provider_programme_id": provider_id})
        normalized.append(normalized_item)
    normalized_value = {"station_ref": ref, "revision": revision, "programmes": normalized}
    try:
        encoded = json.dumps(normalized_value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise invalid_response("Radio programme snapshot is not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > RADIO_PROGRAMME_MAX_BYTES:
        raise invalid_response("Radio programme snapshot exceeds the size limit")
    return normalized_value


def validate_stream_descriptor(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid_response("Stream descriptor must be an object")
    required = {"descriptor_version", "transport", "url", "headers", "credential_refs", "ttl_seconds",
                "expires_at", "volatile_url", "requires_proxy", "warnings"}
    if required - value.keys():
        raise invalid_response("Stream descriptor is missing required fields")
    if not set(value).issubset(DESCRIPTOR_FIELDS):
        raise invalid_response("Stream descriptor contains unsupported fields")
    if value["descriptor_version"] != "1.0" or value["transport"] not in TRANSPORTS:
        raise invalid_response("Unsupported stream descriptor version or transport")
    url = value["url"]
    if not isinstance(url, str) or len(url) > 4096:
        raise invalid_response("Invalid stream URL")
    if value["transport"] != "probe_only":
        try:
            scheme = urlparse(url).scheme.lower()
        except ValueError as exc:
            raise invalid_response("Invalid stream URL") from exc
        if scheme not in {"http", "https", "rtsp"}:
            raise invalid_response("Invalid stream URL")
    headers = value["headers"]
    if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        raise invalid_response("Invalid stream headers")
    if SECRET_HEADERS.intersection(k.lower() for k in headers):
        raise invalid_response("Secret headers must use credential references")
    refs = value["credential_refs"]
    if not isinstance(refs, list) or any(not isinstance(v, str) or not v for v in refs):
        raise invalid_response("Invalid credential references")
    ttl = value["ttl_seconds"]
    if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0):
        raise invalid_response("Invalid stream TTL")
    expiry = value["expires_at"]
    if expiry is not None and (not isinstance(expiry, int) or isinstance(expiry, bool)):
        raise invalid_response("Invalid stream expiry")
    for key in ("volatile_url", "requires_proxy"):
        if not isinstance(value[key], bool):
            raise invalid_response(f"Invalid {key}")
    if not isinstance(value["warnings"], list) or any(not isinstance(v, str) for v in value["warnings"]):
        raise invalid_response("Invalid stream warnings")
    for key in ("quality_variants", "proxy_reasons"):
        if key in value and not isinstance(value[key], list):
            raise invalid_response(f"Invalid {key}")
    if "quality_variants" in value:
        for variant in value["quality_variants"]:
            if not isinstance(variant, dict):
                raise invalid_response("Invalid quality variant")
    for key in ("referer", "origin", "user_agent"):
        if key in value and value[key] is not None and not isinstance(value[key], str):
            raise invalid_response(f"Invalid {key}")
    for key in ("drm", "encryption", "probe_hints", "refresh"):
        if key in value and value[key] is not None and not isinstance(value[key], dict):
            raise invalid_response(f"Invalid {key}")
    if "probe_hints" in value and value["probe_hints"] is not None:
        validate_descriptor_metadata(value["probe_hints"], field="probe_hints")
    diagnostics = value.get("provider_diagnostics", {})
    validate_descriptor_metadata(diagnostics, field="provider diagnostics")
    return dict(value)


def validate_station_ref(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"provider_key", "provider_station_id"}:
        raise invalid_response("Invalid StationRef")
    if any(not isinstance(v, str) or not v for v in value.values()):
        raise invalid_response("Invalid StationRef")
    return dict(value)
