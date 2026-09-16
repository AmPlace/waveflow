"""Pure deterministic matcher for IPTV logical channels and EPG catalog entries.

EPG-2D-a deliberately contains no repository, database, network, or API code.
It consumes immutable snapshots and returns immutable decisions.  Production
matching remains in :mod:`epg` until a later shadow-execution phase.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from epg_catalog import EpgChannelCatalog, EpgChannelIdentity, EpgCatalogEntry
from epg_source_preference import EpgSourcePreferenceResolution
from m3u8_parser import normalize_channel_name


MATCH_RULES: Mapping[str, tuple[int, int]] = MappingProxyType({
    'exact_tvg_id': (0, 100),
    'exact_tvg_name': (1, 95),
    'exact_name': (2, 90),
    'normalized_channel_id': (3, 80),
    'normalized_name': (4, 75),
    'alias': (5, 70),
})

LEGACY_MATCH_TYPE_MAP: Mapping[str, str] = MappingProxyType({
    'raw_tvg_id': 'exact_tvg_id',
    'canonical_key': 'normalized_channel_id',
    'name_exact': 'exact_name',
    'normalized': 'normalized_name',
    'alias': 'alias',
    'manual': 'manual',
})

DECISION_STATUSES = frozenset({
    'matched',
    'ambiguous',
    'unmatched',
    'not_applicable',
    'conflict',
    'locked_preserved',
    'existing_preserved',
})

LOGICAL_CONFLICT_STATUSES = frozenset({'split_conflict', 'merge_conflict'})
AUTO_SOURCE_STATUS = 'success'


@dataclass(frozen=True, order=True)
class EpgMatchEvidence:
    kind: str
    value: str
    member_channel_ids: tuple[int, ...] = ()

    def as_dict(self) -> dict:
        return {
            'kind': self.kind,
            'value': self.value,
            'member_channel_ids': list(self.member_channel_ids),
        }


@dataclass(frozen=True)
class LogicalChannelHintSnapshot:
    logical_channel_id: str
    canonical_key: str
    display_name: str
    status: str
    member_channel_ids: tuple[int, ...]
    raw_tvg_ids: tuple[str, ...]
    raw_tvg_names: tuple[str, ...]
    raw_display_names: tuple[str, ...]
    subscription_ids: tuple[int, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> 'LogicalChannelHintSnapshot':
        def strings(key: str) -> tuple[str, ...]:
            raw = value.get(key) or ()
            if not isinstance(raw, (list, tuple, set, frozenset)):
                return ()
            return tuple(sorted({str(item).strip() for item in raw if str(item).strip()}))

        raw_members = value.get('member_channel_ids') or ()
        member_ids = tuple(sorted({int(item) for item in raw_members}))
        return cls(
            logical_channel_id=str(value.get('logical_channel_id') or ''),
            canonical_key=str(value.get('canonical_key') or ''),
            display_name=str(value.get('display_name') or ''),
            status=str(value.get('status') or ''),
            member_channel_ids=member_ids,
            raw_tvg_ids=strings('raw_tvg_ids'),
            raw_tvg_names=strings('raw_tvg_names'),
            raw_display_names=strings('raw_display_names'),
            subscription_ids=tuple(sorted({int(item) for item in (value.get('subscription_ids') or ())})),
        )


@dataclass(frozen=True)
class ExistingBindingSnapshot:
    target: EpgChannelIdentity
    locked: bool
    status: str = 'matched'
    match_type: str = ''
    confidence: int = 0
    origin: str = ''

    @classmethod
    def from_value(cls, value: object) -> 'ExistingBindingSnapshot':
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            target = value.get('target')
            if not isinstance(target, EpgChannelIdentity):
                target = EpgChannelIdentity(
                    int(value.get('epg_source_id')),
                    str(value.get('epg_channel_id') or ''),
                )
            return cls(
                target=target,
                locked=bool(value.get('locked')),
                status=str(value.get('status') or ''),
                match_type=str(value.get('match_type') or ''),
                confidence=int(value.get('confidence') or 0),
                origin=str(value.get('origin') or ''),
            )
        target = getattr(value, 'target')
        return cls(
            target=target,
            locked=bool(getattr(value, 'locked')),
            status=str(getattr(value, 'status', '') or ''),
            match_type=str(getattr(value, 'match_type', '') or ''),
            confidence=int(getattr(value, 'confidence', 0) or 0),
            origin=str(getattr(value, 'origin', '') or ''),
        )


@dataclass(frozen=True)
class EpgMatchCandidate:
    identity: EpgChannelIdentity
    source_id: int
    channel_id: str
    match_type: str
    confidence: int
    evidence: tuple[EpgMatchEvidence, ...]
    source_status: str
    source_enabled: bool
    auto_applicable: bool

    @property
    def stable_sort_key(self) -> tuple[int, int, int, str]:
        rank = MATCH_RULES.get(self.match_type, (99, self.confidence))[0]
        return (rank, -self.confidence, self.source_id, self.channel_id)

    def as_dict(self) -> dict:
        return {
            'identity': self.identity.as_dict(),
            'source_id': self.source_id,
            'channel_id': self.channel_id,
            'match_type': self.match_type,
            'confidence': self.confidence,
            'evidence': [item.as_dict() for item in self.evidence],
            'source_status': self.source_status,
            'source_enabled': self.source_enabled,
            'auto_applicable': self.auto_applicable,
            'stable_sort_key': list(self.stable_sort_key),
        }


@dataclass(frozen=True)
class EpgMatchDecision:
    logical_channel_id: str
    status: str
    selected_identity: EpgChannelIdentity | None
    match_type: str
    confidence: int
    candidates: tuple[EpgMatchCandidate, ...]
    reasons: tuple[str, ...]
    hint_conflicts: tuple[str, ...]
    existing_binding_action: str

    def as_dict(self) -> dict:
        return {
            'logical_channel_id': self.logical_channel_id,
            'status': self.status,
            'selected_identity': (
                self.selected_identity.as_dict() if self.selected_identity else None
            ),
            'match_type': self.match_type,
            'confidence': self.confidence,
            'candidates': [candidate.as_dict() for candidate in self.candidates],
            'reasons': list(self.reasons),
            'hint_conflicts': list(self.hint_conflicts),
            'existing_binding_action': self.existing_binding_action,
        }


@dataclass
class _CandidateAccumulator:
    entry: EpgCatalogEntry
    match_type: str
    confidence: int
    evidence: set[EpgMatchEvidence]


def convert_legacy_match_type(match_type: str) -> str:
    """Return the stable EPG-2D display type for a legacy match type."""

    value = str(match_type or '')
    return LEGACY_MATCH_TYPE_MAP.get(value, value or 'legacy_unknown')


def _unique_normalized(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({normalized for value in values if (normalized := normalize_channel_name(value))}))


def _safe_evidence_value(value: str) -> str:
    lowered = value.lower()
    sensitive_markers = (
        '://', 'token=', 'access_token=', 'authorization:', 'cookie:',
        '/private/', '/tmp/',
    )
    if any(marker in lowered for marker in sensitive_markers):
        return '[redacted]'
    return value[:256]


def _candidate_from_binding(
    entry: EpgCatalogEntry,
    binding: ExistingBindingSnapshot,
) -> EpgMatchCandidate:
    return EpgMatchCandidate(
        identity=entry.identity,
        source_id=entry.source_id,
        channel_id=entry.channel_id,
        match_type=convert_legacy_match_type(binding.match_type),
        confidence=max(0, min(100, binding.confidence)),
        evidence=(EpgMatchEvidence('existing_binding', binding.origin or binding.status),),
        source_status=entry.source_status,
        source_enabled=entry.source_enabled,
        auto_applicable=False,
    )


def _build_candidates(
    hint: LogicalChannelHintSnapshot,
    catalog: EpgChannelCatalog,
    alias_resolver: Callable[[str], str] | None,
) -> tuple[EpgMatchCandidate, ...]:
    accumulators: dict[EpgChannelIdentity, _CandidateAccumulator] = {}

    def add(
        match_type: str,
        identities: Iterable[EpgChannelIdentity],
        evidence: EpgMatchEvidence,
    ) -> None:
        rank, confidence = MATCH_RULES[match_type]
        for identity in set(identities):
            entry = catalog.get(identity)
            if entry is None:
                continue
            current = accumulators.get(identity)
            if current is None:
                accumulators[identity] = _CandidateAccumulator(
                    entry=entry,
                    match_type=match_type,
                    confidence=confidence,
                    evidence={evidence},
                )
                continue
            current.evidence.add(evidence)
            current_rank = MATCH_RULES[current.match_type][0]
            if rank < current_rank:
                current.match_type = match_type
                current.confidence = confidence

    member_ids = hint.member_channel_ids

    for value in hint.raw_tvg_ids:
        add(
            'exact_tvg_id',
            catalog.lookup_raw_channel_id(value),
            EpgMatchEvidence('raw_tvg_id', _safe_evidence_value(value), member_ids),
        )

    for value in hint.raw_tvg_names:
        add(
            'exact_tvg_name',
            catalog.lookup_exact_display_name(value),
            EpgMatchEvidence('raw_tvg_name', _safe_evidence_value(value), member_ids),
        )

    exact_names = tuple(sorted({value for value in (*hint.raw_display_names, hint.display_name) if value}))
    for value in exact_names:
        add(
            'exact_name',
            catalog.lookup_exact_display_name(value),
            EpgMatchEvidence('exact_name', _safe_evidence_value(value), member_ids),
        )

    normalized_ids = _unique_normalized((*hint.raw_tvg_ids, hint.canonical_key))
    for value in normalized_ids:
        add(
            'normalized_channel_id',
            catalog.lookup_normalized_channel_id(value),
            EpgMatchEvidence('normalized_channel_id', _safe_evidence_value(value), member_ids),
        )

    normalized_names = _unique_normalized(
        (*hint.raw_tvg_names, *hint.raw_display_names, hint.display_name)
    )
    for value in normalized_names:
        add(
            'normalized_name',
            catalog.lookup_normalized_display_name(value),
            EpgMatchEvidence('normalized_name', _safe_evidence_value(value), member_ids),
        )

    if alias_resolver is not None:
        alias_inputs = tuple(sorted({
            value
            for value in (
                *hint.raw_tvg_ids,
                *hint.raw_tvg_names,
                *hint.raw_display_names,
                hint.display_name,
            )
            if value
        }))
        for original in alias_inputs:
            resolved = str(alias_resolver(original) or '').strip()
            if not resolved or resolved == original:
                continue
            normalized = normalize_channel_name(resolved)
            identities = set(catalog.lookup_exact_display_name(resolved))
            if normalized:
                identities.update(catalog.lookup_normalized_display_name(normalized))
                identities.update(catalog.lookup_normalized_channel_id(normalized))
            add(
                'alias',
                identities,
                EpgMatchEvidence(
                    'alias',
                    _safe_evidence_value(f'{original} -> {resolved}'),
                    member_ids,
                ),
            )

    candidates = []
    for accumulator in accumulators.values():
        entry = accumulator.entry
        candidates.append(EpgMatchCandidate(
            identity=entry.identity,
            source_id=entry.source_id,
            channel_id=entry.channel_id,
            match_type=accumulator.match_type,
            confidence=accumulator.confidence,
            evidence=tuple(sorted(accumulator.evidence)),
            source_status=entry.source_status,
            source_enabled=entry.source_enabled,
            auto_applicable=(
                entry.source_enabled and entry.source_status == AUTO_SOURCE_STATUS
            ),
        ))
    return tuple(sorted(candidates, key=lambda candidate: candidate.stable_sort_key))


def _disable_auto(candidates: tuple[EpgMatchCandidate, ...]) -> tuple[EpgMatchCandidate, ...]:
    return tuple(
        candidate if not candidate.auto_applicable else replace(candidate, auto_applicable=False)
        for candidate in candidates
    )


def _decision(
    hint: LogicalChannelHintSnapshot,
    *,
    status: str,
    selected_identity: EpgChannelIdentity | None = None,
    match_type: str = '',
    confidence: int = 0,
    candidates: tuple[EpgMatchCandidate, ...] = (),
    reasons: Iterable[str] = (),
    hint_conflicts: Iterable[str] = (),
    existing_binding_action: str = 'none',
) -> EpgMatchDecision:
    if status not in DECISION_STATUSES:
        raise ValueError(f'unsupported decision status: {status}')
    return EpgMatchDecision(
        logical_channel_id=hint.logical_channel_id,
        status=status,
        selected_identity=selected_identity,
        match_type=match_type,
        confidence=confidence,
        candidates=candidates,
        reasons=tuple(reasons),
        hint_conflicts=tuple(sorted(set(hint_conflicts))),
        existing_binding_action=existing_binding_action,
    )


def match_logical_channel(
    hint: LogicalChannelHintSnapshot | Mapping[str, object],
    catalog: EpgChannelCatalog,
    *,
    existing_binding: ExistingBindingSnapshot | Mapping[str, object] | object | None = None,
    applicability: str = 'unknown',
    alias_resolver: Callable[[str], str] | None = None,
    preference: EpgSourcePreferenceResolution | None = None,
) -> EpgMatchDecision:
    """Return one deterministic decision without reading or writing external state."""

    if not isinstance(hint, LogicalChannelHintSnapshot):
        hint = LogicalChannelHintSnapshot.from_mapping(hint)
    binding = (
        ExistingBindingSnapshot.from_value(existing_binding)
        if existing_binding is not None else None
    )

    if binding is not None and binding.locked:
        entry = catalog.get(binding.target)
        if entry is None:
            return _decision(
                hint,
                status='conflict',
                reasons=('locked_binding_target_missing',),
                hint_conflicts=('orphan_target',),
                existing_binding_action='orphan_target',
            )
        return _decision(
            hint,
            status='locked_preserved',
            selected_identity=binding.target,
            match_type=convert_legacy_match_type(binding.match_type),
            confidence=max(0, min(100, binding.confidence)),
            candidates=(_candidate_from_binding(entry, binding),),
            reasons=(f'existing_target_status:{entry.source_status}',),
            existing_binding_action='preserve_locked',
        )

    if applicability not in {'unknown', 'applicable', 'not_applicable'}:
        raise ValueError(f'unsupported applicability: {applicability}')
    if applicability == 'not_applicable':
        return _decision(
            hint,
            status='not_applicable',
            reasons=('explicit_not_applicable',),
            existing_binding_action='preserve' if binding else 'none',
        )

    candidates = _build_candidates(hint, catalog, alias_resolver)
    normalized_tvg_ids = _unique_normalized(hint.raw_tvg_ids)
    hint_conflicts = []
    if len(normalized_tvg_ids) > 1:
        hint_conflicts.append('tvg_id_conflict')
    if hint.status in LOGICAL_CONFLICT_STATUSES:
        hint_conflicts.append(hint.status)

    if hint.status in LOGICAL_CONFLICT_STATUSES:
        return _decision(
            hint,
            status='conflict',
            candidates=_disable_auto(candidates),
            reasons=('logical_channel_conflict',),
            hint_conflicts=hint_conflicts,
            existing_binding_action='review_required' if binding else 'none',
        )

    existing_entry = catalog.get(binding.target) if binding is not None else None
    if binding is not None and existing_entry is None:
        return _decision(
            hint,
            status='conflict',
            candidates=_disable_auto(candidates),
            reasons=('existing_binding_target_missing',),
            hint_conflicts=('orphan_target',),
            existing_binding_action='orphan_target',
        )

    if 'tvg_id_conflict' in hint_conflicts:
        if binding is not None and existing_entry is not None:
            return _decision(
                hint,
                status='conflict',
                selected_identity=binding.target,
                match_type=convert_legacy_match_type(binding.match_type),
                confidence=max(0, min(100, binding.confidence)),
                candidates=_disable_auto(candidates),
                reasons=('member_tvg_ids_conflict', 'existing_binding_preserved'),
                hint_conflicts=hint_conflicts,
                existing_binding_action='review_recommended',
            )
        return _decision(
            hint,
            status='conflict',
            candidates=_disable_auto(candidates),
            reasons=('member_tvg_ids_conflict',),
            hint_conflicts=hint_conflicts,
            existing_binding_action='review_required' if binding else 'none',
        )

    if binding is not None:
        supported = any(candidate.identity == binding.target for candidate in candidates)
        action = 'preserve' if supported else 'review_recommended'
        reasons = [f'existing_target_status:{existing_entry.source_status}']
        best_rank = min((candidate.stable_sort_key[0] for candidate in candidates), default=None)
        if best_rank is not None:
            top_candidates = tuple(
                candidate for candidate in candidates
                if candidate.stable_sort_key[0] == best_rank
            )
            if len(top_candidates) > 1:
                reasons.append('multiple_composite_candidates')
                action = 'review_recommended'
        if not supported:
            reasons.append('current_hints_do_not_support_existing_target')
        return _decision(
            hint,
            status='existing_preserved',
            selected_identity=binding.target,
            match_type=convert_legacy_match_type(binding.match_type),
            confidence=max(0, min(100, binding.confidence)),
            candidates=_disable_auto(candidates),
            reasons=reasons,
            existing_binding_action=action,
        )

    if not candidates:
        return _decision(
            hint,
            status='unmatched',
            reasons=('no_deterministic_candidate',),
        )

    best_rank = min(candidate.stable_sort_key[0] for candidate in candidates)
    top_candidates = tuple(
        candidate for candidate in candidates
        if candidate.stable_sort_key[0] == best_rank
    )
    if len(top_candidates) > 1:
        sources = {candidate.source_id for candidate in top_candidates}
        reasons = ['multiple_composite_candidates']
        if len(sources) > 1:
            reasons.append('multi_source_collision')
        if preference is not None and preference.status == 'preferred':
            preferred = tuple(
                candidate for candidate in top_candidates
                if candidate.source_id == preference.preferred_source_id
                and candidate.auto_applicable
            )
            if len(preferred) == 1:
                selected = preferred[0]
                return _decision(
                    hint,
                    status='matched',
                    selected_identity=selected.identity,
                    match_type=selected.match_type,
                    confidence=selected.confidence,
                    candidates=candidates,
                    reasons=(
                        'unique_current_success_candidate',
                        'preference_applied',
                        f'preferred_source_id:{selected.source_id}',
                        f'preference_origin:{preference.origin or ""}',
                        f'preference_candidate_count:{len(top_candidates)}',
                    ),
                )
        return _decision(
            hint,
            status='ambiguous',
            candidates=_disable_auto(candidates),
            reasons=reasons,
        )

    selected = top_candidates[0]
    if not selected.auto_applicable:
        return _decision(
            hint,
            status='unmatched',
            candidates=candidates,
            reasons=(f'candidate_source_not_current_success:{selected.source_status}',),
        )

    return _decision(
        hint,
        status='matched',
        selected_identity=selected.identity,
        match_type=selected.match_type,
        confidence=selected.confidence,
        candidates=candidates,
        reasons=('unique_current_success_candidate',),
    )


def summarize_match_decisions(decisions: Iterable[EpgMatchDecision]) -> dict:
    """Return a safe deterministic diagnostic summary for decision snapshots."""

    snapshot = tuple(decisions)
    result = {
        'total_logical_channels': len(snapshot),
        'matched': 0,
        'ambiguous': 0,
        'unmatched': 0,
        'conflict': 0,
        'not_applicable': 0,
        'locked_preserved': 0,
        'existing_preserved': 0,
        'candidate_count': sum(len(decision.candidates) for decision in snapshot),
        'stale_only_candidate_count': 0,
        'multi_source_collision_count': 0,
        'tvg_id_conflict_count': 0,
    }
    for decision in snapshot:
        result[decision.status] += 1
        if decision.candidates and all(
            candidate.source_status == 'stale' for candidate in decision.candidates
        ):
            result['stale_only_candidate_count'] += 1
        if 'multi_source_collision' in decision.reasons:
            result['multi_source_collision_count'] += 1
        if 'tvg_id_conflict' in decision.hint_conflicts:
            result['tvg_id_conflict_count'] += 1
    return result
