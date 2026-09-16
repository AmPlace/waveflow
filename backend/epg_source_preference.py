"""Deterministic, read-only EPG source preference resolution.

This module deliberately has no database dependency.  Until the application
has stable persisted associations between subscriptions/market packages and
EPG sources, callers provide explicit preference evidence at runtime.
"""

from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse


PREFERENCE_ORIGINS = frozenset({"manual", "subscription", "market", "url_tvg"})
PREFERENCE_STATUSES = frozenset({
    "preferred",
    "none",
    "conflict",
    "stale_preference",
    "missing_source",
})

_ORIGIN_PRECEDENCE = {
    "manual": 0,
    "subscription": 1,
    "market": 1,
    "url_tvg": 2,
}
_MAX_TEXT = 256
_MAX_EVIDENCE_ITEMS = 16
_MAX_COMPETING = 16
_SENSITIVE_WORDS = ("url", "token", "cookie", "header", "secret", "password", "authorization")


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    value = "" if value is None else str(value)
    return " ".join(value.split())[:limit]


def _safe_evidence(value: Any) -> str | int | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = _text(value)
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https", "ftp", "file"} or "://" in text:
        return "[redacted-url]"
    return text


def sanitize_evidence(value: Any) -> tuple[tuple[str, str | int | bool], ...]:
    """Return bounded evidence without URLs, credentials, or headers."""
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, (list, tuple)):
        items = ((str(index), item) for index, item in enumerate(value))
    else:
        items = (("value", value),)

    result: list[tuple[str, str | int | bool]] = []
    for key, raw in items:
        safe_key = _text(key, 64).lower()
        if not safe_key or any(word in safe_key for word in _SENSITIVE_WORDS):
            continue
        if isinstance(raw, (dict, list, tuple, set)):
            safe_value = _text(repr(raw))
        else:
            safe_value = _safe_evidence(raw)
        result.append((safe_key, safe_value))
        if len(result) >= _MAX_EVIDENCE_ITEMS:
            break
    return tuple(sorted(result, key=lambda item: item[0]))


@dataclass(frozen=True)
class EpgSourceState:
    source_id: int
    enabled: bool = True
    last_status: str = ""

    @property
    def current_success(self) -> bool:
        return self.enabled and self.last_status == "success"


@dataclass(frozen=True)
class EpgSourcePreference:
    """Explicit evidence connecting a context to a source."""

    epg_source_id: int
    origin: str
    evidence: tuple[tuple[str, str | int | bool], ...] = ()
    logical_channel_id: str = ""
    subscription_id: int | None = None
    context_key: str = ""
    valid: bool = True
    current: bool = True

    def __post_init__(self) -> None:
        if self.origin not in PREFERENCE_ORIGINS:
            raise ValueError(f"unsupported preference origin: {self.origin}")
        if self.epg_source_id <= 0:
            raise ValueError("epg_source_id must be positive")

    @property
    def precedence(self) -> int:
        return _ORIGIN_PRECEDENCE[self.origin]

    def as_dict(self) -> dict[str, Any]:
        return {
            "epg_source_id": self.epg_source_id,
            "origin": self.origin,
            "precedence": self.precedence,
            "evidence": dict(self.evidence),
            "logical_channel_id": self.logical_channel_id,
            "subscription_id": self.subscription_id,
            "context_key": self.context_key,
            "valid": self.valid,
            "current": self.current,
        }


@dataclass(frozen=True)
class EpgSourcePreferenceResolution:
    preferred_source_id: int | None
    status: str
    origin: str | None = None
    evidence: tuple[tuple[str, str | int | bool], ...] = ()
    competing_preferences: tuple[EpgSourcePreference, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in PREFERENCE_STATUSES:
            raise ValueError(f"unsupported preference status: {self.status}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "preferred_source_id": self.preferred_source_id,
            "status": self.status,
            "origin": self.origin,
            "evidence": dict(self.evidence),
            "competing_preferences": [item.as_dict() for item in self.competing_preferences],
        }


def _context_matches(item: EpgSourcePreference, *, logical_channel_id: str, subscription_id: int | None, context_key: str) -> bool:
    if item.logical_channel_id and item.logical_channel_id != logical_channel_id:
        return False
    if item.subscription_id is not None and item.subscription_id != subscription_id:
        return False
    if item.context_key and item.context_key != context_key:
        return False
    return True


def _preference_key(item: EpgSourcePreference) -> tuple[Any, ...]:
    return (
        item.epg_source_id,
        item.origin,
        item.logical_channel_id,
        item.subscription_id,
        item.context_key,
        item.evidence,
        item.valid,
        item.current,
    )


def _preference_identity(item: EpgSourcePreference) -> tuple[Any, ...]:
    return (
        item.epg_source_id,
        item.origin,
        item.logical_channel_id,
        item.subscription_id,
        item.context_key,
        item.valid,
        item.current,
    )


def _dedupe_preferences(items: Iterable[EpgSourcePreference]) -> list[EpgSourcePreference]:
    grouped: dict[tuple[Any, ...], list[EpgSourcePreference]] = {}
    for item in items:
        grouped.setdefault(_preference_identity(item), []).append(item)
    result: list[EpgSourcePreference] = []
    for group in grouped.values():
        first = min(group, key=_preference_key)
        evidence = tuple(
            sorted(
                {pair for item in group for pair in item.evidence},
                key=lambda pair: (pair[0], repr(pair[1])),
            )
        )
        result.append(
            EpgSourcePreference(
                epg_source_id=first.epg_source_id,
                origin=first.origin,
                evidence=evidence[:_MAX_EVIDENCE_ITEMS],
                logical_channel_id=first.logical_channel_id,
                subscription_id=first.subscription_id,
                context_key=first.context_key,
                valid=first.valid,
                current=first.current,
            )
        )
    return result


def resolve_epg_source_preference(
    *,
    logical_channel_id: str = "",
    subscription_id: int | None = None,
    context_key: str = "",
    available_sources: Iterable[EpgSourceState] = (),
    preferences: Iterable[EpgSourcePreference] = (),
) -> EpgSourcePreferenceResolution:
    """Resolve explicit preference evidence without choosing by iteration order."""
    source_map = {int(item.source_id): item for item in available_sources}
    applicable = [
        item for item in preferences
        if _context_matches(
            item,
            logical_channel_id=logical_channel_id,
            subscription_id=subscription_id,
            context_key=context_key,
        ) and item.valid and item.current
    ]
    applicable = _dedupe_preferences(applicable)
    if not applicable:
        return EpgSourcePreferenceResolution(None, "none")

    best_precedence = min(item.precedence for item in applicable)
    strongest = [item for item in applicable if item.precedence == best_precedence]
    source_ids = {item.epg_source_id for item in strongest}
    ordered = tuple(sorted(strongest, key=_preference_key)[:_MAX_COMPETING])
    if len(source_ids) > 1:
        return EpgSourcePreferenceResolution(None, "conflict", competing_preferences=ordered)

    preferred = ordered[0]
    source = source_map.get(preferred.epg_source_id)
    if source is None:
        return EpgSourcePreferenceResolution(
            preferred.epg_source_id,
            "missing_source",
            origin=preferred.origin,
            evidence=preferred.evidence,
            competing_preferences=ordered,
        )
    if not source.current_success:
        return EpgSourcePreferenceResolution(
            preferred.epg_source_id,
            "stale_preference",
            origin=preferred.origin,
            evidence=preferred.evidence,
            competing_preferences=ordered,
        )
    return EpgSourcePreferenceResolution(
        preferred.epg_source_id,
        "preferred",
        origin=preferred.origin,
        evidence=preferred.evidence,
        competing_preferences=ordered,
    )


def resolve_logical_channel_source_preference(
    *,
    logical_channel_id: str,
    subscription_ids: Iterable[int] = (),
    available_sources: Iterable[EpgSourceState] = (),
    preferences: Iterable[EpgSourcePreference] = (),
) -> EpgSourcePreferenceResolution:
    """Resolve all evidence attached to one logical channel deterministically.

    Subscription-scoped evidence is included only when its subscription is a
    current member of the logical channel.  The lower-level resolver remains
    the single source of precedence and conflict semantics.
    """
    member_subscriptions = {int(value) for value in subscription_ids}
    applicable = tuple(
        item for item in preferences
        if (not item.logical_channel_id or item.logical_channel_id == logical_channel_id)
        and (item.subscription_id is None or item.subscription_id in member_subscriptions)
    )
    normalized = tuple(
        EpgSourcePreference(
            epg_source_id=item.epg_source_id,
            origin=item.origin,
            evidence=item.evidence,
            logical_channel_id=logical_channel_id,
            subscription_id=None,
            # The member subscription itself is the context proof.  Once its
            # evidence is aggregated for this logical channel, retaining a
            # narrower context key would make the lower-level resolver discard
            # otherwise applicable evidence because no single member context
            # is selected here.
            context_key="",
            valid=item.valid,
            current=item.current,
        )
        for item in applicable
    )
    return resolve_epg_source_preference(
        logical_channel_id=logical_channel_id,
        available_sources=available_sources,
        preferences=normalized,
    )


__all__ = [
    "EpgSourcePreference",
    "EpgSourcePreferenceResolution",
    "EpgSourceState",
    "PREFERENCE_ORIGINS",
    "PREFERENCE_STATUSES",
    "resolve_epg_source_preference",
    "resolve_logical_channel_source_preference",
    "sanitize_evidence",
]
