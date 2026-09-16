"""Bounded retention garbage collection for orphaned IPTV logical channels.

Human intent is the retention boundary. Matcher output, unlocked automatic or
legacy-migrated bindings and generated diagnostics are derived state and may
be removed after the logical channel has remained orphaned for the retention
window.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import database as db


DEFAULT_ORPHAN_RETENTION_DAYS = 30
DEFAULT_GC_BATCH_SIZE = 100
MAX_GC_BATCH_SIZE = 1000


def _normalize_non_negative_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{field} 必须是非负整数')
    return value


def _normalize_batch_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError('batch_size 必须是整数')
    if not 1 <= value <= MAX_GC_BATCH_SIZE:
        raise ValueError(f'batch_size 必须在 1 到 {MAX_GC_BATCH_SIZE} 之间')
    return value


def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise TypeError('now 必须是 datetime')
    if value.tzinfo is None:
        raise ValueError('now 必须包含时区')
    return value.astimezone(timezone.utc)


_MANUAL_INTENT = """
    (
        EXISTS (
            SELECT 1 FROM iptv_logical_channel_epg_bindings AS manual_binding
            WHERE manual_binding.logical_channel_id=lc.id
              AND manual_binding.origin='manual'
        )
        OR EXISTS (
            SELECT 1 FROM epg_source_preference_evidence AS manual_preference
            WHERE manual_preference.logical_channel_id=lc.id
              AND manual_preference.origin='manual'
        )
    )
"""

_LOCKED_INTENT = """
    EXISTS (
        SELECT 1 FROM iptv_logical_channel_epg_bindings AS locked_binding
        WHERE locked_binding.logical_channel_id=lc.id
          AND locked_binding.locked=1
    )
"""

_NO_EPG_INTENT = """
    EXISTS (
        SELECT 1 FROM iptv_logical_channel_epg_policies AS no_epg_policy
        WHERE no_epg_policy.logical_channel_id=lc.id
          AND no_epg_policy.mode='no_epg'
    )
"""

_UNSUPPORTED_BINDING = """
    EXISTS (
        SELECT 1 FROM iptv_logical_channel_epg_bindings AS other_binding
        WHERE other_binding.logical_channel_id=lc.id
          AND other_binding.origin NOT IN ('automatic', 'legacy_migrated', 'manual')
    )
"""

_HAS_MEMBER = """
    EXISTS (
        SELECT 1 FROM iptv_logical_channel_members AS member
        WHERE member.logical_channel_id=lc.id
    )
"""

_ELIGIBLE_BASE = f"""
    lc.status='orphaned'
    AND NOT {_HAS_MEMBER}
    AND NOT {_MANUAL_INTENT}
    AND NOT {_LOCKED_INTENT}
    AND NOT {_NO_EPG_INTENT}
    AND NOT {_UNSUPPORTED_BINDING}
"""


def _count(conn, where: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(
        f'SELECT COUNT(*) FROM iptv_logical_channels AS lc WHERE {where}',
        params,
    ).fetchone()[0])


def _scan_gc_candidates_sync(
    *,
    cutoff_at: str,
    batch_size: int,
) -> dict[str, Any]:
    """Read one candidate snapshot without holding a write lock."""
    conn = db._connect()
    try:
        scanned_count = _count(conn, "lc.status='orphaned'")
        retained_conflict_count = _count(
            conn,
            "lc.status IN ('split_conflict', 'merge_conflict')",
        )
        retained_manual_count = _count(
            conn,
            f"lc.status='orphaned' AND {_MANUAL_INTENT}",
        )
        retained_locked_count = _count(
            conn,
            f"lc.status='orphaned' AND NOT {_MANUAL_INTENT} AND {_LOCKED_INTENT}",
        )
        retained_policy_count = _count(
            conn,
            (
                "lc.status='orphaned' "
                f"AND NOT {_MANUAL_INTENT} AND NOT {_LOCKED_INTENT} "
                f"AND {_NO_EPG_INTENT}"
            ),
        )
        retained_member_count = _count(
            conn,
            f"lc.status='orphaned' AND {_HAS_MEMBER}",
        )
        retained_other_count = _count(
            conn,
            f"lc.status='orphaned' AND {_UNSUPPORTED_BINDING}",
        )
        too_recent_count = _count(
            conn,
            f"{_ELIGIBLE_BASE} AND (lc.orphaned_at IS NULL OR lc.orphaned_at>?)",
            (cutoff_at,),
        )
        candidate_count = _count(
            conn,
            f"{_ELIGIBLE_BASE} AND lc.orphaned_at IS NOT NULL AND lc.orphaned_at<=?",
            (cutoff_at,),
        )
        rows = conn.execute(
            f"""
            SELECT lc.id
            FROM iptv_logical_channels AS lc
            WHERE {_ELIGIBLE_BASE}
              AND lc.orphaned_at IS NOT NULL
              AND lc.orphaned_at<=?
            ORDER BY lc.orphaned_at, lc.id
            LIMIT ?
            """,
            (cutoff_at, batch_size),
        ).fetchall()
        return {
            'scanned_count': scanned_count,
            'candidate_count': candidate_count,
            'selected_count': len(rows),
            'candidate_ids': tuple(str(row['id']) for row in rows),
            'retained_manual_count': retained_manual_count,
            'retained_locked_count': retained_locked_count,
            'retained_policy_count': retained_policy_count,
            'retained_conflict_count': retained_conflict_count,
            'retained_member_count': retained_member_count,
            'retained_other_count': retained_other_count,
            'too_recent_count': too_recent_count,
        }
    finally:
        conn.close()


def _candidate_still_eligible(conn, logical_channel_id: str, cutoff_at: str) -> bool:
    return conn.execute(
        f"""
        SELECT 1 FROM iptv_logical_channels AS lc
        WHERE lc.id=?
          AND {_ELIGIBLE_BASE}
          AND lc.orphaned_at IS NOT NULL
          AND lc.orphaned_at<=?
        LIMIT 1
        """,
        (logical_channel_id, cutoff_at),
    ).fetchone() is not None


def _delete_gc_candidates_sync(
    candidate_ids: Iterable[str],
    *,
    cutoff_at: str,
) -> dict[str, int]:
    """Revalidate and delete one candidate batch in one immediate transaction."""
    conn = db._connect()
    deleted_count = 0
    revalidated_out_count = 0
    deleted_binding_count = 0
    deleted_policy_count = 0
    deleted_preference_count = 0
    deleted_shadow_decision_count = 0
    deleted_shadow_candidate_count = 0
    try:
        conn.execute('BEGIN IMMEDIATE')
        for logical_channel_id in candidate_ids:
            if not _candidate_still_eligible(conn, logical_channel_id, cutoff_at):
                revalidated_out_count += 1
                continue

            deleted_shadow_candidate_count += conn.execute(
                "DELETE FROM epg_match_shadow_candidates WHERE logical_channel_id=?",
                (logical_channel_id,),
            ).rowcount
            deleted_shadow_decision_count += conn.execute(
                "DELETE FROM epg_match_shadow_decisions WHERE logical_channel_id=?",
                (logical_channel_id,),
            ).rowcount
            deleted_preference_count += conn.execute(
                """
                DELETE FROM epg_source_preference_evidence
                WHERE logical_channel_id=? AND origin<>'manual'
                """,
                (logical_channel_id,),
            ).rowcount
            deleted_policy_count += conn.execute(
                """
                DELETE FROM iptv_logical_channel_epg_policies
                WHERE logical_channel_id=? AND mode='automatic'
                """,
                (logical_channel_id,),
            ).rowcount
            deleted_binding_count += conn.execute(
                """
                DELETE FROM iptv_logical_channel_epg_bindings
                WHERE logical_channel_id=? AND locked=0
                  AND origin IN ('automatic', 'legacy_migrated')
                """,
                (logical_channel_id,),
            ).rowcount

            cursor = conn.execute(
                f"""
                DELETE FROM iptv_logical_channels
                WHERE id=?
                  AND EXISTS (
                      SELECT 1 FROM iptv_logical_channels AS lc
                      WHERE lc.id=iptv_logical_channels.id
                        AND {_ELIGIBLE_BASE}
                        AND lc.orphaned_at IS NOT NULL
                        AND lc.orphaned_at<=?
                  )
                """,
                (logical_channel_id, cutoff_at),
            )
            if cursor.rowcount != 1:
                raise RuntimeError('logical channel GC revalidation changed during transaction')
            deleted_count += 1
        conn.commit()
        return {
            'deleted_count': deleted_count,
            'revalidated_out_count': revalidated_out_count,
            'deleted_binding_count': deleted_binding_count,
            'deleted_policy_count': deleted_policy_count,
            'deleted_preference_count': deleted_preference_count,
            'deleted_shadow_decision_count': deleted_shadow_decision_count,
            'deleted_shadow_candidate_count': deleted_shadow_candidate_count,
        }
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


async def garbage_collect_iptv_logical_channels(
    *,
    retention_days: int = DEFAULT_ORPHAN_RETENTION_DAYS,
    batch_size: int = DEFAULT_GC_BATCH_SIZE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete one bounded batch of expired orphaned logical channels."""
    retention_days = _normalize_non_negative_int(retention_days, 'retention_days')
    batch_size = _normalize_batch_size(batch_size)
    current = _normalize_now(now)
    cutoff_at = (current - timedelta(days=retention_days)).isoformat()

    snapshot = await asyncio.to_thread(
        _scan_gc_candidates_sync,
        cutoff_at=cutoff_at,
        batch_size=batch_size,
    )
    deleted = await asyncio.to_thread(
        _delete_gc_candidates_sync,
        snapshot.pop('candidate_ids'),
        cutoff_at=cutoff_at,
    )
    return {
        'retention_days': retention_days,
        'batch_size': batch_size,
        'cutoff_at': cutoff_at,
        **snapshot,
        **deleted,
        'has_more': snapshot['candidate_count'] > snapshot['selected_count'],
    }


__all__ = [
    'DEFAULT_GC_BATCH_SIZE',
    'DEFAULT_ORPHAN_RETENTION_DAYS',
    'garbage_collect_iptv_logical_channels',
]
