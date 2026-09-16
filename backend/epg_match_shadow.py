"""Shadow execution and persistence for the deterministic EPG matcher.

EPG-2D-b1 intentionally keeps the result separate from the logical binding
table. A run reads one SQLite snapshot, executes the pure matcher in memory,
and writes only the three shadow tables in one transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping

import database as db
from epg_catalog import EpgChannelCatalog, EpgChannelIdentity, build_epg_channel_catalog_from_rows
from epg_matcher import (
    ExistingBindingSnapshot,
    EpgMatchCandidate,
    EpgMatchDecision,
    LogicalChannelHintSnapshot,
    match_logical_channel,
    summarize_match_decisions,
)
from epg_source_preference import (
    EpgSourcePreference,
    EpgSourcePreferenceResolution,
    EpgSourceState,
    resolve_logical_channel_source_preference,
)
from epg_preference_evidence import _resolution_for_row

MAX_JSON_LENGTH = 16384
MAX_ERROR_LENGTH = 1024
SHADOW_FINAL_STATUSES = frozenset({'success', 'partial'})
SHADOW_STATUSES = frozenset({'running', 'success', 'partial', 'failed', 'cancelled'})


@dataclass(frozen=True)
class EpgMatchShadowSnapshot:
    hints: tuple[LogicalChannelHintSnapshot, ...]
    catalog: EpgChannelCatalog
    existing_bindings: Mapping[str, ExistingBindingSnapshot]
    logical_channel_count: int
    raw_member_count: int
    epg_source_count: int
    catalog_channel_count: int
    existing_binding_count: int
    source_revision_summary: tuple[dict[str, Any], ...]
    preferences: Mapping[str, EpgSourcePreferenceResolution]
    applicability: Mapping[str, str]
    preference_snapshot_fingerprint: str
    preference_evidence_count: int
    created_at: str


@dataclass(frozen=True)
class ShadowRun:
    run_id: str
    status: str
    started_at: str
    finished_at: str
    logical_channel_count: int
    raw_member_count: int
    epg_source_count: int
    catalog_channel_count: int
    existing_binding_count: int
    matched_count: int
    ambiguous_count: int
    unmatched_count: int
    conflict_count: int
    not_applicable_count: int
    locked_preserved_count: int
    existing_preserved_count: int
    stale_only_count: int
    candidate_count: int
    source_revision_summary: tuple[dict[str, Any], ...]
    preference_snapshot_fingerprint: str = ''
    error: str = ''

    def as_dict(self) -> dict[str, Any]:
        result = dict(self.__dict__)
        result['source_revision_summary'] = [dict(item) for item in self.source_revision_summary]
        return result


@dataclass(frozen=True)
class ShadowDecisionRecord:
    run_id: str
    logical_channel_id: str
    status: str
    selected_identity: EpgChannelIdentity | None
    match_type: str
    confidence: int
    existing_binding_action: str
    reasons: tuple[str, ...]
    hint_conflicts: tuple[str, ...]
    candidate_count: int
    error: str = ''

    def as_dict(self) -> dict[str, Any]:
        return {
            'run_id': self.run_id,
            'logical_channel_id': self.logical_channel_id,
            'status': self.status,
            'selected_identity': self.selected_identity.as_dict() if self.selected_identity else None,
            'match_type': self.match_type,
            'confidence': self.confidence,
            'existing_binding_action': self.existing_binding_action,
            'reasons': list(self.reasons),
            'hint_conflicts': list(self.hint_conflicts),
            'candidate_count': self.candidate_count,
            'error': self.error,
        }


@dataclass(frozen=True)
class ShadowCandidateRecord:
    run_id: str
    logical_channel_id: str
    rank: int
    identity: EpgChannelIdentity
    match_type: str
    confidence: int
    source_status: str
    source_enabled: bool
    auto_applicable: bool
    evidence: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            'run_id': self.run_id,
            'logical_channel_id': self.logical_channel_id,
            'rank': self.rank,
            'identity': self.identity.as_dict(),
            'match_type': self.match_type,
            'confidence': self.confidence,
            'source_status': self.source_status,
            'source_enabled': self.source_enabled,
            'auto_applicable': self.auto_applicable,
            'evidence': [dict(item) for item in self.evidence],
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: object, limit: int = 256) -> str:
    text = str(value or '')
    lowered = text.lower()
    if any(marker in lowered for marker in ('://', 'token=', 'access_token=', 'authorization:', 'cookie:', '/private/', '/tmp/')):
        return '[redacted]'
    return text[:limit]


def _sanitize(value: object, depth: int = 0) -> object:
    if depth > 5:
        return '[truncated]'
    if isinstance(value, str):
        return _safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            _safe_text(key, 64): _sanitize(item, depth + 1)
            for key, item in list(value.items())[:64]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize(item, depth + 1) for item in list(value)[:64]]
    return _safe_text(value)


def _safe_json(value: object) -> str:
    encoded = json.dumps(_sanitize(value), ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    if len(encoded) <= MAX_JSON_LENGTH:
        return encoded
    return json.dumps({'truncated': True, 'preview': encoded[:MAX_JSON_LENGTH - 64]}, ensure_ascii=False, separators=(',', ':'))


def _safe_error(error: object) -> str:
    return _safe_text(error, MAX_ERROR_LENGTH)


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _group_hints(rows: Iterable[Mapping[str, Any]]) -> tuple[LogicalChannelHintSnapshot, ...]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        logical_id = str(_row_value(row, 'logical_channel_id', '') or '')
        if not logical_id:
            continue
        item = grouped.setdefault(logical_id, {
            'logical_channel_id': logical_id,
            'canonical_key': str(_row_value(row, 'canonical_key', '') or ''),
            'display_name': str(_row_value(row, 'display_name', '') or ''),
            'status': str(_row_value(row, 'logical_status', '') or ''),
            'member_channel_ids': [],
            'raw_tvg_ids': [],
            'raw_tvg_names': [],
            'raw_display_names': [],
            'subscription_ids': [],
        })
        member_id = _row_value(row, 'member_channel_id')
        if member_id is not None:
            item['member_channel_ids'].append(int(member_id))
        subscription_id = _row_value(row, 'subscription_id')
        if subscription_id is not None:
            item['subscription_ids'].append(int(subscription_id))
        for field, output in (
            ('raw_tvg_id', 'raw_tvg_ids'),
            ('raw_tvg_name', 'raw_tvg_names'),
            ('raw_name', 'raw_display_names'),
        ):
            value = str(_row_value(row, field, '') or '').strip()
            if value and value not in item[output]:
                item[output].append(value)
    hints = []
    for item in grouped.values():
        item['member_channel_ids'] = sorted(set(item['member_channel_ids']))
        for field in ('raw_tvg_ids', 'raw_tvg_names', 'raw_display_names'):
            item[field] = sorted(set(item[field]))
        hints.append(LogicalChannelHintSnapshot.from_mapping(item))
    return tuple(sorted(hints, key=lambda hint: hint.logical_channel_id))


def _load_snapshot_sync() -> EpgMatchShadowSnapshot:
    conn = db._connect()
    try:
        conn.execute('BEGIN')
        snapshot = _snapshot_from_connection(conn)
        conn.commit()
        return snapshot
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


async def load_epg_match_shadow_snapshot() -> EpgMatchShadowSnapshot:
    return await asyncio.to_thread(_load_snapshot_sync)


async def _is_stopped(stop: object | None) -> bool:
    if stop is None:
        return False
    value: object
    if hasattr(stop, 'is_set'):
        value = stop.is_set()
    elif callable(stop):
        value = stop()
    else:
        value = bool(stop)
    if inspect.isawaitable(value):
        value = await value
    return bool(value)


def _decision_record(run_id: str, decision: EpgMatchDecision) -> ShadowDecisionRecord:
    selected = decision.selected_identity
    return ShadowDecisionRecord(
        run_id=run_id,
        logical_channel_id=decision.logical_channel_id,
        status=decision.status,
        selected_identity=selected,
        match_type=decision.match_type,
        confidence=int(decision.confidence),
        existing_binding_action=decision.existing_binding_action,
        reasons=tuple(_safe_text(item) for item in decision.reasons),
        hint_conflicts=tuple(_safe_text(item) for item in decision.hint_conflicts),
        candidate_count=len(decision.candidates),
    )


def _candidate_records(run_id: str, decision: EpgMatchDecision) -> tuple[ShadowCandidateRecord, ...]:
    return tuple(
        ShadowCandidateRecord(
            run_id=run_id,
            logical_channel_id=decision.logical_channel_id,
            rank=rank,
            identity=candidate.identity,
            match_type=candidate.match_type,
            confidence=int(candidate.confidence),
            source_status=_safe_text(candidate.source_status),
            source_enabled=bool(candidate.source_enabled),
            auto_applicable=bool(candidate.auto_applicable),
            evidence=tuple(_sanitize(item.as_dict()) for item in candidate.evidence),
        )
        for rank, candidate in enumerate(decision.candidates, start=1)
    )


def _counts(decisions: Iterable[EpgMatchDecision]) -> dict[str, int]:
    summary = summarize_match_decisions(decisions)
    return {
        'matched_count': int(summary['matched']),
        'ambiguous_count': int(summary['ambiguous']),
        'unmatched_count': int(summary['unmatched']),
        'conflict_count': int(summary['conflict']),
        'not_applicable_count': int(summary['not_applicable']),
        'locked_preserved_count': int(summary['locked_preserved']),
        'existing_preserved_count': int(summary['existing_preserved']),
        'stale_only_count': int(summary['stale_only_candidate_count']),
        'candidate_count': int(summary['candidate_count']),
    }


def _run_params(snapshot: EpgMatchShadowSnapshot, run_id: str, status: str, started_at: str, finished_at: str, counts: Mapping[str, int], error: str = '') -> tuple[Any, ...]:
    return (
        run_id, status, started_at, finished_at,
        snapshot.logical_channel_count, snapshot.raw_member_count,
        snapshot.epg_source_count, snapshot.catalog_channel_count,
        snapshot.existing_binding_count, counts.get('matched_count', 0),
        counts.get('ambiguous_count', 0), counts.get('unmatched_count', 0),
        counts.get('conflict_count', 0), counts.get('not_applicable_count', 0),
        counts.get('locked_preserved_count', 0), counts.get('existing_preserved_count', 0),
        counts.get('stale_only_count', 0), counts.get('candidate_count', 0),
        _safe_json(snapshot.source_revision_summary), snapshot.preference_snapshot_fingerprint,
        _safe_error(error),
    )


def _persist_run_sync(
    snapshot: EpgMatchShadowSnapshot,
    run_id: str,
    status: str,
    started_at: str,
    finished_at: str,
    decisions: tuple[EpgMatchDecision, ...],
    errors: Mapping[str, str],
    error: str = '',
) -> ShadowRun:
    if status not in SHADOW_FINAL_STATUSES:
        raise ValueError(f'unsupported result status: {status}')
    counts = _counts(decisions)
    conn = db._connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            """
            INSERT INTO epg_match_shadow_runs(
                run_id, status, started_at, finished_at, logical_channel_count,
                raw_member_count, epg_source_count, catalog_channel_count,
                existing_binding_count, matched_count, ambiguous_count,
                unmatched_count, conflict_count, not_applicable_count,
                locked_preserved_count, existing_preserved_count, stale_only_count,
                candidate_count, source_revision_summary_json, preference_snapshot_fingerprint, error
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _run_params(snapshot, run_id, status, started_at, finished_at, counts, error),
        )
        for decision in decisions:
            record = _decision_record(run_id, decision)
            channel_error = errors.get(decision.logical_channel_id, '')
            conn.execute(
                """
                INSERT INTO epg_match_shadow_decisions(
                    run_id, logical_channel_id, status, selected_epg_source_id,
                    selected_epg_channel_id, match_type, confidence,
                    existing_binding_action, reasons_json, hint_conflicts_json,
                    candidate_count, error
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id, record.logical_channel_id, record.status,
                    record.selected_identity.source_id if record.selected_identity else None,
                    record.selected_identity.channel_id if record.selected_identity else None,
                    record.match_type, record.confidence, record.existing_binding_action,
                    _safe_json(record.reasons), _safe_json(record.hint_conflicts),
                    record.candidate_count, _safe_error(channel_error),
                ),
            )
            for candidate in _candidate_records(run_id, decision):
                conn.execute(
                    """
                    INSERT INTO epg_match_shadow_candidates(
                        run_id, logical_channel_id, rank, epg_source_id, epg_channel_id,
                        match_type, confidence, source_status, source_enabled,
                        auto_applicable, evidence_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.run_id, candidate.logical_channel_id, candidate.rank,
                        candidate.identity.source_id, candidate.identity.channel_id,
                        candidate.match_type, candidate.confidence, candidate.source_status,
                        int(candidate.source_enabled), int(candidate.auto_applicable),
                        _safe_json(candidate.evidence),
                    ),
                )
        for logical_channel_id, channel_error in sorted(errors.items()):
            conn.execute(
                """
                INSERT INTO epg_match_shadow_decisions(
                    run_id, logical_channel_id, status, selected_epg_source_id,
                    selected_epg_channel_id, match_type, confidence,
                    existing_binding_action, reasons_json, hint_conflicts_json,
                    candidate_count, error
                ) VALUES(?, ?, 'error', NULL, NULL, '', 0, 'none', '[]', '[]', 0, ?)
                """,
                (run_id, logical_channel_id, _safe_error(channel_error)),
            )
        conn.commit()
        return _run_from_row(conn.execute(
            'SELECT * FROM epg_match_shadow_runs WHERE run_id=?', (run_id,)
        ).fetchone())
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _persist_metadata_sync(run_id: str, status: str, started_at: str, finished_at: str, error: str = '') -> ShadowRun:
    conn = db._connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            "INSERT INTO epg_match_shadow_runs(run_id, status, started_at, finished_at, error) VALUES(?, ?, ?, ?, ?)",
            (run_id, status, started_at, finished_at, _safe_error(error)),
        )
        conn.commit()
        return _run_from_row(conn.execute('SELECT * FROM epg_match_shadow_runs WHERE run_id=?', (run_id,)).fetchone())
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _run_from_row(row: sqlite3.Row | Mapping[str, Any] | None) -> ShadowRun:
    if row is None:
        raise LookupError('shadow run not found')
    return ShadowRun(
        run_id=str(row['run_id']), status=str(row['status']),
        started_at=str(row['started_at'] or ''), finished_at=str(row['finished_at'] or ''),
        logical_channel_count=int(row['logical_channel_count'] or 0),
        raw_member_count=int(row['raw_member_count'] or 0), epg_source_count=int(row['epg_source_count'] or 0),
        catalog_channel_count=int(row['catalog_channel_count'] or 0), existing_binding_count=int(row['existing_binding_count'] or 0),
        matched_count=int(row['matched_count'] or 0), ambiguous_count=int(row['ambiguous_count'] or 0),
        unmatched_count=int(row['unmatched_count'] or 0), conflict_count=int(row['conflict_count'] or 0),
        not_applicable_count=int(row['not_applicable_count'] or 0), locked_preserved_count=int(row['locked_preserved_count'] or 0),
        existing_preserved_count=int(row['existing_preserved_count'] or 0), stale_only_count=int(row['stale_only_count'] or 0),
        candidate_count=int(row['candidate_count'] or 0),
        source_revision_summary=tuple(json.loads(row['source_revision_summary_json'] or '[]')),
        preference_snapshot_fingerprint=str(row['preference_snapshot_fingerprint'] or ''),
        error=str(row['error'] or ''),
    )


def _decision_from_row(row: sqlite3.Row | Mapping[str, Any]) -> ShadowDecisionRecord:
    source_id = row['selected_epg_source_id']
    selected = EpgChannelIdentity(int(source_id), str(row['selected_epg_channel_id'])) if source_id is not None and row['selected_epg_channel_id'] is not None else None
    return ShadowDecisionRecord(
        run_id=str(row['run_id']), logical_channel_id=str(row['logical_channel_id']), status=str(row['status']),
        selected_identity=selected, match_type=str(row['match_type'] or ''), confidence=int(row['confidence'] or 0),
        existing_binding_action=str(row['existing_binding_action'] or 'none'),
        reasons=tuple(json.loads(row['reasons_json'] or '[]')),
        hint_conflicts=tuple(json.loads(row['hint_conflicts_json'] or '[]')),
        candidate_count=int(row['candidate_count'] or 0), error=str(row['error'] or ''),
    )


def _candidate_from_row(row: sqlite3.Row | Mapping[str, Any]) -> ShadowCandidateRecord:
    return ShadowCandidateRecord(
        run_id=str(row['run_id']), logical_channel_id=str(row['logical_channel_id']), rank=int(row['rank']),
        identity=EpgChannelIdentity(int(row['epg_source_id']), str(row['epg_channel_id'])),
        match_type=str(row['match_type'] or ''), confidence=int(row['confidence'] or 0),
        source_status=str(row['source_status'] or ''), source_enabled=bool(row['source_enabled']),
        auto_applicable=bool(row['auto_applicable']), evidence=tuple(json.loads(row['evidence_json'] or '[]')),
    )


async def run_epg_match_shadow(stop: object | None = None) -> dict[str, Any]:
    """Run the deterministic matcher against one coherent read snapshot."""
    run_id = str(uuid.uuid4())
    started_at = _now()
    if await _is_stopped(stop):
        run = await asyncio.to_thread(_persist_metadata_sync, run_id, 'cancelled', started_at, _now(), 'stopped before snapshot')
        return run.as_dict()
    try:
        snapshot = await load_epg_match_shadow_snapshot()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            run = await asyncio.to_thread(_persist_metadata_sync, run_id, 'failed', started_at, _now(), f'snapshot failed: {_safe_error(exc)}')
            return run.as_dict()
        except Exception:
            return {'run_id': run_id, 'status': 'failed', 'error': f'snapshot failed: {_safe_error(exc)}'}
    if await _is_stopped(stop):
        run = await asyncio.to_thread(_persist_metadata_sync, run_id, 'cancelled', started_at, _now(), 'stopped after snapshot')
        return run.as_dict()

    decisions: list[EpgMatchDecision] = []
    errors: dict[str, str] = {}
    for hint in snapshot.hints:
        if await _is_stopped(stop):
            run = await asyncio.to_thread(_persist_metadata_sync, run_id, 'cancelled', started_at, _now(), 'stopped during matching')
            return run.as_dict()
        try:
            decisions.append(match_logical_channel(
                hint,
                snapshot.catalog,
                existing_binding=snapshot.existing_bindings.get(hint.logical_channel_id),
                applicability=snapshot.applicability.get(
                    hint.logical_channel_id,
                    'unknown',
                ),
                preference=snapshot.preferences.get(hint.logical_channel_id),
            ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors[hint.logical_channel_id] = _safe_error(exc)
    if await _is_stopped(stop):
        run = await asyncio.to_thread(_persist_metadata_sync, run_id, 'cancelled', started_at, _now(), 'stopped before persistence')
        return run.as_dict()

    error_summary = '; '.join(f'{key}: {value}' for key, value in sorted(errors.items()))
    # A partial run must contain at least one valid decision.  If every logical
    # channel failed before producing a decision, retain only failed run metadata
    # rather than presenting an empty result set as a usable partial run.
    if errors and not decisions:
        run = await asyncio.to_thread(
            _persist_metadata_sync, run_id, 'failed', started_at, _now(),
            f'matching failed: {error_summary}',
        )
        return run.as_dict()

    status = 'partial' if errors else 'success'
    try:
        run = await asyncio.to_thread(
            _persist_run_sync, snapshot, run_id, status, started_at, _now(), tuple(decisions), errors,
            error_summary,
        )
        return run.as_dict()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            run = await asyncio.to_thread(_persist_metadata_sync, run_id, 'failed', started_at, _now(), f'persistence failed: {_safe_error(exc)}')
            return run.as_dict()
        except Exception:
            return {'run_id': run_id, 'status': 'failed', 'error': f'persistence failed: {_safe_error(exc)}'}


async def get_shadow_run(run_id: str) -> ShadowRun | None:
    def _get():
        conn = db._connect()
        try:
            row = conn.execute('SELECT * FROM epg_match_shadow_runs WHERE run_id=?', (run_id,)).fetchone()
            return _run_from_row(row) if row else None
        finally:
            conn.close()
    return await asyncio.to_thread(_get)


async def get_latest_completed_shadow_run() -> ShadowRun | None:
    def _get():
        conn = db._connect()
        try:
            return _run_from_row(conn.execute(
                "SELECT * FROM epg_match_shadow_runs WHERE status IN ('success','partial') ORDER BY finished_at DESC, run_id DESC LIMIT 1"
            ).fetchone())
        finally:
            conn.close()
    return await asyncio.to_thread(_get)


async def get_shadow_decision(run_id: str, logical_channel_id: str) -> ShadowDecisionRecord | None:
    records = await list_shadow_decisions(run_id, logical_channel_id=logical_channel_id)
    return records[0] if records else None


async def list_shadow_decisions(run_id: str, status: str | None = None, logical_channel_id: str | None = None) -> list[ShadowDecisionRecord]:
    def _list():
        conn = db._connect()
        try:
            query = 'SELECT * FROM epg_match_shadow_decisions WHERE run_id=?'
            params: list[Any] = [run_id]
            if status is not None:
                query += ' AND status=?'; params.append(status)
            if logical_channel_id is not None:
                query += ' AND logical_channel_id=?'; params.append(logical_channel_id)
            query += ' ORDER BY logical_channel_id'
            return [_decision_from_row(row) for row in conn.execute(query, params).fetchall()]
        finally:
            conn.close()
    return await asyncio.to_thread(_list)


async def get_shadow_decision_candidates(run_id: str, logical_channel_id: str) -> list[ShadowCandidateRecord]:
    def _list():
        conn = db._connect()
        try:
            rows = conn.execute(
                'SELECT * FROM epg_match_shadow_candidates WHERE run_id=? AND logical_channel_id=? ORDER BY rank',
                (run_id, logical_channel_id),
            ).fetchall()
            return [_candidate_from_row(row) for row in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_list)


async def list_shadow_ambiguous(run_id: str) -> list[ShadowDecisionRecord]:
    return await list_shadow_decisions(run_id, status='ambiguous')


async def list_shadow_conflict(run_id: str) -> list[ShadowDecisionRecord]:
    return await list_shadow_decisions(run_id, status='conflict')


async def list_shadow_unmatched(run_id: str) -> list[ShadowDecisionRecord]:
    return await list_shadow_decisions(run_id, status='unmatched')


# EPG-2D-b2: safe, opt-in application of deterministic shadow decisions.
# This entry point deliberately lives beside the shadow executor and does not
# participate in production matching, refresh, or API paths.
_APPLY_LOGICAL_CONFLICT_STATUSES = frozenset({'split_conflict', 'merge_conflict'})
_APPLY_ELIGIBLE_STATUS = 'matched'


def _snapshot_from_connection(conn: sqlite3.Connection) -> EpgMatchShadowSnapshot:
    """Build the current matcher inputs from one already-open read/write connection."""
    hint_rows = [dict(row) for row in conn.execute(
        """
        SELECT lc.id AS logical_channel_id, lc.canonical_key, lc.display_name,
               lc.status AS logical_status, m.channel_id AS member_channel_id,
               c.name AS raw_name, c.tvg_id AS raw_tvg_id, c.tvg_name AS raw_tvg_name,
               c.subscription_id AS subscription_id
        FROM iptv_logical_channels AS lc
        LEFT JOIN iptv_logical_channel_members AS m ON m.logical_channel_id = lc.id
        LEFT JOIN channels AS c ON c.id = m.channel_id
        ORDER BY lc.id, m.channel_id
        """
    ).fetchall()]
    catalog_rows = [dict(row) for row in conn.execute(
        """
        SELECT s.id AS source_id, s.name AS source_name, s.enabled AS source_enabled,
               s.last_status AS source_status, c.channel_id, c.display_names,
               c.normalized_names
        FROM epg_channels AS c
        JOIN epg_sources AS s ON s.id = c.source_id
        ORDER BY s.id, c.channel_id, c.id
        """
    ).fetchall()]
    source_rows = [dict(row) for row in conn.execute(
        'SELECT id, url, revision, enabled, last_status FROM epg_sources ORDER BY id'
    ).fetchall()]
    binding_rows = [dict(row) for row in conn.execute(
        """
        SELECT b.logical_channel_id, b.epg_source_id, b.epg_channel_id,
               b.status, b.match_type, b.confidence, b.locked, b.origin
        FROM iptv_logical_channel_epg_bindings AS b
        ORDER BY b.logical_channel_id
        """
    ).fetchall()]
    hints = _group_hints(hint_rows)
    catalog = build_epg_channel_catalog_from_rows(catalog_rows)
    source_states = tuple(
        EpgSourceState(int(row['id']), bool(row['enabled']), str(row['last_status'] or ''))
        for row in source_rows
    )
    evidence_rows = [dict(row) for row in conn.execute(
        'SELECT * FROM epg_source_preference_evidence WHERE valid=1 AND current=1 ORDER BY subscription_id, id'
    ).fetchall()]
    policy_rows = [dict(row) for row in conn.execute(
        """
        SELECT logical_channel_id, mode
        FROM iptv_logical_channel_epg_policies
        ORDER BY logical_channel_id
        """
    ).fetchall()]
    all_preferences: list[EpgSourcePreference] = []
    fingerprint_rows = []
    for row in evidence_rows:
        preferences, _invalid_reason = _resolution_for_row(row, source_rows)
        for preference in preferences:
            all_preferences.append(preference)
        fingerprint_rows.append({
            'id': int(row['id']), 'subscription_id': row['subscription_id'],
            'logical_channel_id': row['logical_channel_id'], 'origin': row['origin'],
            'source_id': row['epg_source_id'], 'hint_fingerprint': row['hint_fingerprint'],
            'resolution_status': row['resolution_status'],
        })
    preference_digest = hashlib.sha256(
        json.dumps(fingerprint_rows, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    preference_map = {}
    for hint in hints:
        preference_map[hint.logical_channel_id] = resolve_logical_channel_source_preference(
            logical_channel_id=hint.logical_channel_id,
            subscription_ids=hint.subscription_ids,
            available_sources=source_states,
            preferences=all_preferences,
        )
    hint_ids = {hint.logical_channel_id for hint in hints}
    existing: dict[str, ExistingBindingSnapshot] = {}
    for row in binding_rows:
        logical_id = str(row['logical_channel_id'])
        if logical_id in hint_ids:
            existing[logical_id] = ExistingBindingSnapshot(
                target=EpgChannelIdentity(int(row['epg_source_id']), str(row['epg_channel_id'])),
                locked=bool(row['locked']),
                status=str(row['status'] or ''),
                match_type=str(row['match_type'] or ''),
                confidence=int(row['confidence'] or 0),
                origin=str(row['origin'] or ''),
            )
    return EpgMatchShadowSnapshot(
        hints=hints,
        catalog=catalog,
        existing_bindings=existing,
        logical_channel_count=len(hints),
        raw_member_count=sum(len(hint.member_channel_ids) for hint in hints),
        epg_source_count=len(source_rows),
        catalog_channel_count=len(catalog.entries),
        existing_binding_count=len(binding_rows),
        source_revision_summary=tuple({
            'source_id': int(row['id']),
            'revision': int(row['revision'] or 0),
            'enabled': bool(row['enabled']),
            'last_status': str(row['last_status'] or ''),
        } for row in source_rows),
        preferences=preference_map,
        applicability={
            str(row['logical_channel_id']): (
                'not_applicable'
                if str(row['mode']) == 'no_epg'
                else 'unknown'
            )
            for row in policy_rows
        },
        preference_snapshot_fingerprint=preference_digest,
        preference_evidence_count=len(evidence_rows),
        created_at=_now(),
    )


def _apply_result(run_id: str, started_at: str) -> dict[str, Any]:
    return {
        'run_id': run_id,
        'run_status': '',
        'eligible_decision_count': 0,
        'applied_count': 0,
        'already_bound_count': 0,
        'skipped_status_count': 0,
        'stale_decision_count': 0,
        'revalidation_mismatch_count': 0,
        'target_missing_count': 0,
        'logical_missing_count': 0,
        'logical_conflict_count': 0,
        'locked_conflict_count': 0,
        'existing_conflict_count': 0,
        'rollback': False,
        'error': '',
        'started_at': started_at,
        'finished_at': '',
    }


def _binding_row_for_logical(conn: sqlite3.Connection, logical_channel_id: str):
    return conn.execute(
        """
        SELECT id, logical_channel_id, epg_source_id, epg_channel_id,
               status, match_type, confidence, locked, origin
        FROM iptv_logical_channel_epg_bindings
        WHERE logical_channel_id=?
        """,
        (logical_channel_id,),
    ).fetchone()


def _apply_epg_match_shadow_run_sync(run_id: str, started_at: str) -> dict[str, Any]:
    result = _apply_result(run_id, started_at)
    conn = db._connect()
    try:
        # BEGIN IMMEDIATE makes the revalidation and all inserts one serialized
        # operation. No shadow table is changed until every check passes.
        conn.execute('BEGIN IMMEDIATE')
        run_row = conn.execute(
            'SELECT * FROM epg_match_shadow_runs WHERE run_id=?', (run_id,)
        ).fetchone()
        if run_row is None:
            result['error'] = 'shadow run 不存在'
            result['finished_at'] = _now()
            conn.rollback()
            return result
        result['run_status'] = str(run_row['status'])
        if result['run_status'] != 'success':
            result['error'] = f"shadow run 状态不可应用: {result['run_status']}"
            result['finished_at'] = _now()
            conn.rollback()
            return result

        decision_rows = [dict(row) for row in conn.execute(
            """
            SELECT * FROM epg_match_shadow_decisions
            WHERE run_id=? ORDER BY logical_channel_id
            """,
            (run_id,),
        ).fetchall()]
        eligible: list[dict[str, Any]] = []
        for row in decision_rows:
            selected_source = row['selected_epg_source_id']
            selected_channel = row['selected_epg_channel_id']
            if (
                str(row['status']) == _APPLY_ELIGIBLE_STATUS
                and selected_source is not None
                and selected_channel not in (None, '')
            ):
                eligible.append(row)
            else:
                result['skipped_status_count'] += 1
        result['eligible_decision_count'] = len(eligible)

        snapshot = _snapshot_from_connection(conn)
        hints = {hint.logical_channel_id: hint for hint in snapshot.hints}
        for row in eligible:
            logical_id = str(row['logical_channel_id'])
            selected = EpgChannelIdentity(int(row['selected_epg_source_id']), str(row['selected_epg_channel_id']))
            logical = conn.execute(
                'SELECT id, status FROM iptv_logical_channels WHERE id=?', (logical_id,)
            ).fetchone()
            if logical is None:
                result['logical_missing_count'] += 1
                continue
            if str(logical['status'] or '') in _APPLY_LOGICAL_CONFLICT_STATUSES:
                result['logical_conflict_count'] += 1
                continue
            if str(logical['status'] or '') != 'active':
                result['stale_decision_count'] += 1
                continue
            hint = hints.get(logical_id)
            if hint is None:
                result['stale_decision_count'] += 1
                continue
            if hint.status in _APPLY_LOGICAL_CONFLICT_STATUSES:
                result['logical_conflict_count'] += 1
                continue

            target = conn.execute(
                """
                SELECT s.enabled, s.last_status
                FROM epg_channels AS c
                JOIN epg_sources AS s ON s.id=c.source_id
                WHERE c.source_id=? AND c.channel_id=?
                LIMIT 1
                """,
                (selected.source_id, selected.channel_id),
            ).fetchone()
            if target is None:
                result['target_missing_count'] += 1
                continue
            if not bool(target['enabled']) or str(target['last_status'] or '') != 'success':
                result['stale_decision_count'] += 1
                continue
            catalog_entry = snapshot.catalog.get(selected)
            if catalog_entry is None:
                result['target_missing_count'] += 1
                continue

            try:
                current_decision = match_logical_channel(
                    hint,
                    snapshot.catalog,
                    existing_binding=None,
                    applicability=snapshot.applicability.get(
                        logical_id,
                        'unknown',
                    ),
                    preference=snapshot.preferences.get(logical_id),
                )
            except Exception as exc:
                raise RuntimeError(f'当前 Matcher 重验证失败 ({logical_id}): {_safe_error(exc)}') from exc
            current_selected = current_decision.selected_identity
            if current_decision.status != 'matched' or current_selected != selected:
                result['revalidation_mismatch_count'] += 1
                continue

            current_binding = _binding_row_for_logical(conn, logical_id)
            if current_binding is not None:
                current_target = EpgChannelIdentity(
                    int(current_binding['epg_source_id']), str(current_binding['epg_channel_id'])
                )
                if current_target == selected:
                    result['already_bound_count'] += 1
                elif bool(current_binding['locked']):
                    result['locked_conflict_count'] += 1
                else:
                    result['existing_conflict_count'] += 1
                continue

            now = db._utc_now()
            conn.execute(
                """
                INSERT INTO iptv_logical_channel_epg_bindings(
                    logical_channel_id, epg_source_id, epg_channel_id,
                    status, match_type, confidence, locked, origin,
                    shadow_run_id, legacy_canonical_key, created_at, updated_at
                ) VALUES(?, ?, ?, 'matched', ?, ?, 0, 'automatic', ?, '', ?, ?)
                """,
                (
                    logical_id,
                    selected.source_id,
                    selected.channel_id,
                    _safe_text(row['match_type']),
                    max(0, min(100, int(row['confidence'] or 0))),
                    run_id,
                    now,
                    now,
                ),
            )
            result['applied_count'] += 1
        conn.commit()
        result['finished_at'] = _now()
        return result
    except asyncio.CancelledError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        result['rollback'] = True
        result['error'] = _safe_error(exc)
        result['finished_at'] = _now()
        return result
    finally:
        conn.close()


async def apply_epg_match_shadow_run(run_id: str) -> dict[str, Any]:
    """Safely apply eligible deterministic decisions from one successful run.

    This is an explicit b2 operation. It never updates or deletes an existing
    binding, never touches ``channel_epg_map``, and is not called by startup or
    any production matcher path.
    """
    run_id = _safe_text(run_id)
    if not run_id:
        raise ValueError('run_id 不能为空')
    started_at = _now()
    return await asyncio.to_thread(_apply_epg_match_shadow_run_sync, run_id, started_at)
