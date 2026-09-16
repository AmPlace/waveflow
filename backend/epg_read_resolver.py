"""Read-only resolution of logical EPG bindings for production reads."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Iterable, Mapping

import database as db
from epg_catalog import EpgChannelIdentity


READABLE_BINDING_STATUSES = {'matched'}
logger = logging.getLogger(__name__)
_DIAGNOSTIC_STATUSES = {
    'target_changed',
    'logical_ambiguous',
    'logical_conflict',
    'shadow_orphan_target',
    'migrated_target_mismatch',
}


@dataclass(frozen=True)
class EpgReadTarget:
    """A source-aware EPG target plus the current dataset/source state."""

    source_id: int
    channel_id: str
    exists: bool
    source_enabled: bool | None = None
    source_status: str = ''

    @property
    def identity(self) -> EpgChannelIdentity:
        return EpgChannelIdentity(self.source_id, self.channel_id)

    @property
    def readable(self) -> bool:
        # EPG-1 preserves the last committed dataset and programme reads do
        # not filter on enabled/last_status. Therefore stale, failed, and
        # disabled sources remain queryable while the composite channel row
        # exists; those states are diagnostics, not read blockers.
        return self.exists

    def as_dict(self) -> dict:
        return {
            'source_id': self.source_id,
            'channel_id': self.channel_id,
            'exists': self.exists,
            'readable': self.readable,
            'source_enabled': self.source_enabled,
            'source_status': self.source_status,
        }


@dataclass(frozen=True)
class EpgReadResolution:
    """A safe, internal diagnostic for one canonical-key read."""

    canonical_key: str
    logical_channel_ids: tuple[str, ...]
    logical_channel_id: str | None
    logical_target: EpgReadTarget | None
    comparison_status: str
    effective_target: EpgReadTarget | None
    effective_source: str
    fallback_reason: str
    management_mode: str
    binding: dict | None

    def as_dict(self) -> dict:
        return {
            'canonical_key': self.canonical_key,
            'logical_channel_ids': list(self.logical_channel_ids),
            'logical_channel_id': self.logical_channel_id,
            'logical_target': self.logical_target.as_dict() if self.logical_target else None,
            'comparison_status': self.comparison_status,
            'effective_target': self.effective_target.as_dict() if self.effective_target else None,
            'effective_source': self.effective_source,
            'fallback_reason': self.fallback_reason,
            'management_mode': self.management_mode,
            'binding': self.binding,
        }


def _target_from_identity(
    identity: EpgChannelIdentity | None,
    target_rows: Mapping[EpgChannelIdentity, Mapping[str, object]],
) -> EpgReadTarget | None:
    if identity is None:
        return None
    row = target_rows.get(identity)
    return EpgReadTarget(
        source_id=identity.source_id,
        channel_id=identity.channel_id,
        exists=row is not None,
        source_enabled=(bool(row.get('source_enabled')) if row is not None else None),
        source_status=(str(row.get('source_status') or '') if row is not None else ''),
    )


def _binding_identity(row: Mapping[str, object] | None) -> EpgChannelIdentity | None:
    if not row:
        return None
    source_id = row.get('epg_source_id')
    channel_id = row.get('epg_channel_id')
    if source_id is None or channel_id in (None, ''):
        return None
    try:
        return EpgChannelIdentity(int(source_id), str(channel_id))
    except (TypeError, ValueError):
        return None


def _safe_shadow_binding(row: Mapping[str, object] | None) -> dict | None:
    if row is None:
        return None
    fields = (
        'id', 'logical_channel_id', 'epg_source_id', 'epg_channel_id',
        'status', 'match_type', 'confidence', 'locked', 'origin',
        'shadow_run_id', 'legacy_canonical_key', 'created_at', 'updated_at',
    )
    return {field: row.get(field) for field in fields if field in row}


def resolve_epg_read_snapshot(
    canonical_key: str,
    *,
    logical_rows: Iterable[Mapping[str, object]],
    binding_rows: Iterable[Mapping[str, object]],
    target_rows: Iterable[Mapping[str, object]],
    policy_rows: Iterable[Mapping[str, object]] = (),
) -> EpgReadResolution:
    """Resolve and select a read target from caller-provided snapshots."""
    indexes = _build_snapshot_indexes(
        logical_rows,
        binding_rows,
        target_rows,
        policy_rows,
    )
    return _resolve_epg_read_indexed(
        canonical_key,
        indexes=indexes,
    )


def _build_snapshot_indexes(
    logical_rows: Iterable[Mapping[str, object]],
    binding_rows: Iterable[Mapping[str, object]],
    target_rows: Iterable[Mapping[str, object]],
    policy_rows: Iterable[Mapping[str, object]] = (),
) -> dict[str, dict]:
    """Build deterministic in-memory indexes for one read snapshot."""
    logical_by_key: dict[str, list[dict]] = {}
    for row in logical_rows:
        item = dict(row)
        key = str(item.get('canonical_key') or '')
        logical_by_key.setdefault(key, []).append(item)
    for rows in logical_by_key.values():
        rows.sort(key=lambda row: str(row.get('id') or ''))

    target_by_identity: dict[EpgChannelIdentity, Mapping[str, object]] = {}
    for row in target_rows:
        target_by_identity[EpgChannelIdentity(int(row['source_id']), str(row['channel_id']))] = row

    binding_by_logical: dict[str, dict] = {}
    for row in binding_rows:
        binding_by_logical[str(row.get('logical_channel_id') or '')] = dict(row)

    policy_by_logical = {
        str(row.get('logical_channel_id') or ''): str(row.get('mode') or '')
        for row in policy_rows
    }

    return {
        'logical_by_key': logical_by_key,
        'binding_by_logical': binding_by_logical,
        'target_by_identity': target_by_identity,
        'policy_by_logical': policy_by_logical,
    }


def _resolve_epg_read_indexed(
    canonical_key: str,
    *,
    indexes: Mapping[str, Mapping],
) -> EpgReadResolution:
    """Resolve one key against indexes built from a single snapshot."""
    canonical_key = str(canonical_key)
    logical_candidates = list(indexes['logical_by_key'].get(canonical_key, ()))
    active_candidates = [
        row for row in logical_candidates
        if str(row.get('status') or '') == 'active'
    ]
    logical_ids = tuple(str(row.get('id') or '') for row in logical_candidates)
    logical_channel_id: str | None = None
    logical_status = ''
    if len(active_candidates) == 1:
        logical_channel_id = str(active_candidates[0].get('id') or '')
        logical_status = 'active'
    elif not active_candidates and len(logical_candidates) == 1:
        logical_channel_id = logical_ids[0]
        logical_status = str(logical_candidates[0].get('status') or '')

    target_by_identity = indexes['target_by_identity']

    binding_by_logical = indexes['binding_by_logical']
    management_mode = str(
        indexes.get('policy_by_logical', {}).get(logical_channel_id or '', '')
    )
    binding = None
    shadow_target = None
    if logical_channel_id and logical_status == 'active':
        candidate_binding = binding_by_logical.get(logical_channel_id or '')
        if (
            candidate_binding is not None
            and str(candidate_binding.get('status') or '') in READABLE_BINDING_STATUSES
        ):
            binding = candidate_binding
            shadow_target = _target_from_identity(_binding_identity(binding), target_by_identity)

    if management_mode == 'no_epg':
        comparison_status = 'not_applicable'
        fallback_reason = 'explicit_no_epg'
    elif not logical_candidates:
        comparison_status = 'logical_missing'
        fallback_reason = 'logical_channel_missing'
    elif len(active_candidates) > 1:
        comparison_status = 'logical_ambiguous'
        fallback_reason = 'multiple_logical_channels'
    elif not active_candidates:
        comparison_status = 'logical_conflict'
        fallback_reason = (
            f'logical_channel_{logical_status}'
            if logical_status
            else 'logical_channel_historical'
        )
    elif binding is not None and shadow_target is not None and not shadow_target.exists:
        comparison_status = 'shadow_orphan_target'
        fallback_reason = 'logical_target_missing'
    elif binding is not None and shadow_target is not None:
        comparison_status = 'bound'
        fallback_reason = 'logical_binding'
    else:
        comparison_status = 'unbound'
        fallback_reason = 'logical_binding_unavailable'

    if management_mode == 'no_epg':
        effective_target = None
        effective_source = 'none'
    else:
        effective_target = shadow_target if shadow_target is not None and shadow_target.readable else None
        effective_source = 'logical' if effective_target is not None else 'none'

    return EpgReadResolution(
        canonical_key=canonical_key,
        logical_channel_ids=logical_ids,
        logical_channel_id=logical_channel_id,
        logical_target=shadow_target,
        comparison_status=comparison_status,
        effective_target=effective_target,
        effective_source=effective_source,
        fallback_reason=fallback_reason,
        management_mode=management_mode,
        binding=_safe_shadow_binding(binding),
    )


def _diagnostic_payload(resolution: EpgReadResolution | Mapping[str, object], *, context: str) -> dict:
    """Build a bounded, non-sensitive payload for internal read diagnostics."""
    if isinstance(resolution, EpgReadResolution):
        data = resolution.as_dict()
    else:
        data = dict(resolution)
    logical = data.get('logical_target') or {}
    comparison_status = str(data.get('comparison_status') or '')
    fallback_reason = str(data.get('fallback_reason') or '')
    diagnostic_status = (
        'migrated_target_mismatch'
        if fallback_reason == 'migrated_target_mismatch'
        else comparison_status
    )
    return {
        'context': context,
        'status': diagnostic_status,
        'comparison_status': comparison_status,
        'canonical_key': str(data.get('canonical_key') or ''),
        'logical_channel_id': data.get('logical_channel_id'),
        'logical_channel_count': len(data.get('logical_channel_ids') or []),
        'logical_target': (logical.get('source_id'), logical.get('channel_id')) if logical else None,
        'fallback_reason': fallback_reason,
    }

def emit_epg_read_diagnostic(
    resolution: EpgReadResolution | Mapping[str, object],
    *,
    context: str,
) -> None:
    """Emit low-noise structured diagnostics for actionable comparisons only."""
    payload = _diagnostic_payload(resolution, context=context)
    if payload['status'] in _DIAGNOSTIC_STATUSES:
        logger.warning('epg_read_shadow_diagnostic', extra={'epg_read_diagnostic': payload})

def emit_epg_read_resolver_error(*, context: str, error: BaseException) -> None:
    """Emit an error diagnostic without persisting or logging exception text."""
    payload = {
        'context': context,
        'status': 'resolver_error',
        'error_type': type(error).__name__,
    }
    logger.warning('epg_read_shadow_diagnostic', extra={'epg_read_diagnostic': payload})


async def _load_snapshot():
    logical_rows = await db.get_iptv_logical_channels()
    target_rows = await db.list_epg_channel_catalog_rows()
    from epg_bindings import list_epg_binding_policies, list_epg_bindings

    bindings = [binding.as_dict() for binding in await list_epg_bindings()]
    policies = [policy.as_dict() for policy in await list_epg_binding_policies()]
    return logical_rows, bindings, target_rows, policies


async def resolve_epg_read(
    canonical_key: str,
) -> dict:
    """Resolve one production EPG read target from logical state only."""
    (
        logical_rows,
        binding_rows,
        target_rows,
        policy_rows,
    ) = await _load_snapshot()
    return resolve_epg_read_snapshot(
        canonical_key,
        logical_rows=logical_rows,
        binding_rows=binding_rows,
        target_rows=target_rows,
        policy_rows=policy_rows,
    ).as_dict()


async def resolve_epg_read_many(canonical_keys: Iterable[str]) -> dict[str, dict]:
    """Resolve a batch in one logical/catalog/binding snapshot."""
    keys = [str(key) for key in canonical_keys]
    logical_rows, binding_rows, target_rows, policy_rows = await _load_snapshot()
    indexes = _build_snapshot_indexes(
        logical_rows,
        binding_rows,
        target_rows,
        policy_rows,
    )
    return {
        key: _resolve_epg_read_indexed(
            key,
            indexes=indexes,
        ).as_dict()
        for key in keys
    }
