"""Read-only, source-aware EPG channel catalog.

The legacy EPG matcher still treats a bare channel id as globally unique.  This
module deliberately does not change that matcher.  It provides a separate
snapshot model whose identity is the durable database pair
``(epg_source.id, epg_channels.channel_id)`` so later migration work can reason
about source collisions without changing current production behaviour.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

import database as db
from m3u8_parser import normalize_channel_name


@dataclass(frozen=True, order=True)
class EpgChannelIdentity:
    """The only stable identity of an EPG channel in the shadow catalog."""

    source_id: int
    channel_id: str

    def as_dict(self) -> dict[str, int | str]:
        return {
            'source_id': self.source_id,
            'channel_id': self.channel_id,
        }


@dataclass(frozen=True)
class EpgCatalogEntry:
    """Safe metadata for one source-aware EPG channel."""

    identity: EpgChannelIdentity
    source_id: int
    source_name: str
    source_enabled: bool
    source_status: str
    channel_id: str
    display_names: tuple[str, ...]
    normalized_display_names: tuple[str, ...]
    normalized_channel_id: str

    def as_dict(self) -> dict:
        return {
            'identity': self.identity.as_dict(),
            'source_id': self.source_id,
            'source_name': self.source_name,
            'source_enabled': self.source_enabled,
            'source_status': self.source_status,
            'channel_id': self.channel_id,
            'display_names': list(self.display_names),
            'normalized_display_names': list(self.normalized_display_names),
            'normalized_channel_id': self.normalized_channel_id,
        }


@dataclass(frozen=True)
class EpgCatalogCollision:
    """A safe, source-aware collision sample for internal diagnostics."""

    channel_id: str
    candidates: tuple[EpgCatalogEntry, ...]

    def as_dict(self) -> dict:
        return {
            'channel_id': self.channel_id,
            'candidates': [
                {
                    'source_id': entry.source_id,
                    'channel_id': entry.channel_id,
                    'display_names': list(entry.display_names),
                }
                for entry in self.candidates
            ],
        }


@dataclass(frozen=True)
class EpgCatalogDiagnostics:
    source_count: int
    epg_channel_count: int
    unique_identity_count: int
    duplicate_raw_channel_id_count: int
    duplicate_normalized_channel_id_count: int
    duplicate_display_name_count: int
    cross_source_collision_count: int
    disabled_source_channel_count: int
    stale_source_channel_count: int
    cross_source_collisions: tuple[EpgCatalogCollision, ...]

    def as_dict(self) -> dict:
        return {
            'source_count': self.source_count,
            'epg_channel_count': self.epg_channel_count,
            'unique_identity_count': self.unique_identity_count,
            'duplicate_raw_channel_id_count': self.duplicate_raw_channel_id_count,
            'duplicate_normalized_channel_id_count': self.duplicate_normalized_channel_id_count,
            'duplicate_display_name_count': self.duplicate_display_name_count,
            'cross_source_collision_count': self.cross_source_collision_count,
            'disabled_source_channel_count': self.disabled_source_channel_count,
            'stale_source_channel_count': self.stale_source_channel_count,
            'cross_source_collisions': [collision.as_dict() for collision in self.cross_source_collisions],
        }


@dataclass(frozen=True)
class EpgChannelCatalog:
    """An immutable snapshot and multimap indexes over EPG channel entries."""

    entries: tuple[EpgCatalogEntry, ...]
    by_identity: Mapping[EpgChannelIdentity, EpgCatalogEntry]
    by_raw_channel_id: Mapping[str, tuple[EpgChannelIdentity, ...]]
    by_normalized_channel_id: Mapping[str, tuple[EpgChannelIdentity, ...]]
    by_exact_display_name: Mapping[str, tuple[EpgChannelIdentity, ...]]
    by_normalized_display_name: Mapping[str, tuple[EpgChannelIdentity, ...]]
    diagnostics: EpgCatalogDiagnostics

    def get(self, identity: EpgChannelIdentity) -> EpgCatalogEntry | None:
        return self.by_identity.get(identity)

    def get_by_identity(self, source_id: int, channel_id: str) -> EpgCatalogEntry | None:
        return self.get(EpgChannelIdentity(source_id, channel_id))

    def lookup_raw_channel_id(self, channel_id: str) -> tuple[EpgChannelIdentity, ...]:
        return self.by_raw_channel_id.get(channel_id, ())

    def lookup_normalized_channel_id(self, channel_id: str) -> tuple[EpgChannelIdentity, ...]:
        return self.by_normalized_channel_id.get(channel_id, ())

    def lookup_exact_display_name(self, display_name: str) -> tuple[EpgChannelIdentity, ...]:
        return self.by_exact_display_name.get(display_name, ())

    def lookup_normalized_display_name(self, display_name: str) -> tuple[EpgChannelIdentity, ...]:
        return self.by_normalized_display_name.get(display_name, ())

    def diagnostic_dict(self) -> dict:
        return self.diagnostics.as_dict()


def _unique_strings(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _decode_string_list(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return _unique_strings(value)
    if not isinstance(value, str) or not value:
        return ()
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return ()
    return _unique_strings(decoded) if isinstance(decoded, list) else ()


def _mapping_proxy(index: Mapping[object, Iterable[EpgChannelIdentity]]) -> Mapping:
    return MappingProxyType({
        key: tuple(sorted(set(values)))
        for key, values in index.items()
    })


def _entry_from_row(row: Mapping[str, object]) -> EpgCatalogEntry:
    source_id = int(row['source_id'])
    channel_id = str(row['channel_id'])
    display_names = _decode_string_list(row.get('display_names'))
    normalized_display_names = _decode_string_list(row.get('normalized_names'))
    if not normalized_display_names:
        normalized_display_names = _unique_strings(
            normalize_channel_name(name) for name in display_names
        )
    return EpgCatalogEntry(
        identity=EpgChannelIdentity(source_id, channel_id),
        source_id=source_id,
        source_name=str(row.get('source_name') or ''),
        source_enabled=bool(row.get('source_enabled')),
        source_status=str(row.get('source_status') or ''),
        channel_id=channel_id,
        display_names=display_names,
        normalized_display_names=normalized_display_names,
        normalized_channel_id=normalize_channel_name(channel_id),
    )


def _build_catalog(rows: Iterable[Mapping[str, object]]) -> EpgChannelCatalog:
    entries = tuple(sorted((_entry_from_row(row) for row in rows), key=lambda entry: entry.identity))
    by_identity: dict[EpgChannelIdentity, EpgCatalogEntry] = {}
    raw_index: dict[str, set[EpgChannelIdentity]] = defaultdict(set)
    normalized_id_index: dict[str, set[EpgChannelIdentity]] = defaultdict(set)
    exact_name_index: dict[str, set[EpgChannelIdentity]] = defaultdict(set)
    normalized_name_index: dict[str, set[EpgChannelIdentity]] = defaultdict(set)

    for entry in entries:
        if entry.identity in by_identity:
            raise ValueError(f'duplicate EPG channel identity: {entry.identity!r}')
        by_identity[entry.identity] = entry
        raw_index[entry.channel_id].add(entry.identity)
        if entry.normalized_channel_id:
            normalized_id_index[entry.normalized_channel_id].add(entry.identity)
        for name in entry.display_names:
            exact_name_index[name].add(entry.identity)
        for name in entry.normalized_display_names:
            if name:
                normalized_name_index[name].add(entry.identity)

    def duplicate_count(index: Mapping[object, set[EpgChannelIdentity]]) -> int:
        return sum(len(identities) > 1 for identities in index.values())

    cross_source_collisions: list[EpgCatalogCollision] = []
    for channel_id, identities in raw_index.items():
        candidates = tuple(by_identity[identity] for identity in sorted(identities))
        if len({entry.source_id for entry in candidates}) > 1:
            cross_source_collisions.append(EpgCatalogCollision(channel_id, candidates))

    cross_source_collisions.sort(key=lambda collision: collision.channel_id)
    diagnostics = EpgCatalogDiagnostics(
        source_count=len({entry.source_id for entry in entries}),
        epg_channel_count=len(entries),
        unique_identity_count=len(by_identity),
        duplicate_raw_channel_id_count=duplicate_count(raw_index),
        duplicate_normalized_channel_id_count=duplicate_count(normalized_id_index),
        duplicate_display_name_count=duplicate_count(normalized_name_index),
        cross_source_collision_count=len(cross_source_collisions),
        disabled_source_channel_count=sum(not entry.source_enabled for entry in entries),
        stale_source_channel_count=sum(entry.source_status == 'stale' for entry in entries),
        cross_source_collisions=tuple(cross_source_collisions),
    )
    return EpgChannelCatalog(
        entries=entries,
        by_identity=MappingProxyType(by_identity),
        by_raw_channel_id=_mapping_proxy(raw_index),
        by_normalized_channel_id=_mapping_proxy(normalized_id_index),
        by_exact_display_name=_mapping_proxy(exact_name_index),
        by_normalized_display_name=_mapping_proxy(normalized_name_index),
        diagnostics=diagnostics,
    )


def build_epg_channel_catalog_from_rows(rows: Iterable[Mapping[str, object]]) -> EpgChannelCatalog:
    """Build a catalog from safe database-shaped rows.

    This small pure helper makes the source-collision semantics independently
    testable without opening a second database connection.
    """

    return _build_catalog(rows)


async def build_epg_channel_catalog(
    *,
    include_disabled: bool = True,
    current_success_only: bool = False,
) -> EpgChannelCatalog:
    """Build a current source-aware catalog snapshot from the database.

    ``current_success_only`` retains only enabled sources whose latest refresh
    status is ``success``.  It describes current source status, not whether a
    stale source's preserved dataset can still serve programme queries.  The
    default snapshot retains disabled, stale, and failed source rows for
    diagnostics.
    """

    rows = await db.list_epg_channel_catalog_rows()
    filtered_rows = []
    for row in rows:
        enabled = bool(row.get('source_enabled'))
        status = str(row.get('source_status') or '')
        if not include_disabled and not enabled:
            continue
        if current_success_only and (not enabled or status != 'success'):
            continue
        filtered_rows.append(row)
    return _build_catalog(filtered_rows)
