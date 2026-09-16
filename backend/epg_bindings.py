"""Logical IPTV channel bindings to source-aware EPG targets."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

import database as db
from epg_catalog import EpgChannelIdentity
from m3u8_parser import normalize_channel_name


BINDING_STATUSES = {
    'matched',
    'ambiguous',
    'unmatched',
    'not_applicable',
    'conflict',
    'orphan_target',
}
BINDING_ORIGINS = {'legacy_migrated', 'automatic', 'manual'}
LOGICAL_CONFLICT_STATUSES = {'split_conflict', 'merge_conflict'}
EPG_POLICY_MODES = {'automatic', 'no_epg'}


class EpgBindingOperationError(ValueError):
    """Stable domain failure for product-facing logical binding operations."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EpgLogicalChannelBinding:
    id: int
    logical_channel_id: str
    target: EpgChannelIdentity
    status: str
    match_type: str
    confidence: int
    locked: bool
    origin: str
    legacy_canonical_key: str
    created_at: str
    updated_at: str
    shadow_run_id: str | None = None

    @property
    def epg_source_id(self) -> int:
        return self.target.source_id

    @property
    def epg_channel_id(self) -> str:
        return self.target.channel_id

    def as_dict(self) -> dict:
        return {
            'id': self.id,
            'logical_channel_id': self.logical_channel_id,
            'epg_source_id': self.epg_source_id,
            'epg_channel_id': self.epg_channel_id,
            'target': self.target.as_dict(),
            'status': self.status,
            'match_type': self.match_type,
            'confidence': self.confidence,
            'locked': self.locked,
            'origin': self.origin,
            'legacy_canonical_key': self.legacy_canonical_key,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'shadow_run_id': self.shadow_run_id,
        }


@dataclass(frozen=True)
class EpgLogicalChannelPolicy:
    logical_channel_id: str
    mode: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict:
        return {
            'logical_channel_id': self.logical_channel_id,
            'mode': self.mode,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


_BINDING_SELECT = """
    SELECT id, logical_channel_id, epg_source_id, epg_channel_id,
           status, match_type, confidence, locked, origin,
           legacy_canonical_key, created_at, updated_at, shadow_run_id
    FROM iptv_logical_channel_epg_bindings
"""

_POLICY_SELECT = """
    SELECT logical_channel_id, mode, created_at, updated_at
    FROM iptv_logical_channel_epg_policies
"""


def _binding_from_row(row: Mapping[str, object]) -> EpgLogicalChannelBinding:
    return EpgLogicalChannelBinding(
        id=int(row['id']),
        logical_channel_id=str(row['logical_channel_id']),
        target=EpgChannelIdentity(int(row['epg_source_id']), str(row['epg_channel_id'])),
        status=str(row['status']),
        match_type=str(row.get('match_type') or ''),
        confidence=int(row.get('confidence') or 0),
        locked=bool(row.get('locked')),
        origin=str(row.get('origin') or ''),
        legacy_canonical_key=str(row.get('legacy_canonical_key') or ''),
        created_at=str(row.get('created_at') or ''),
        updated_at=str(row.get('updated_at') or ''),
        shadow_run_id=(str(row['shadow_run_id']) if row.get('shadow_run_id') else None),
    )


def _policy_from_row(row: Mapping[str, object]) -> EpgLogicalChannelPolicy:
    return EpgLogicalChannelPolicy(
        logical_channel_id=str(row['logical_channel_id']),
        mode=str(row['mode']),
        created_at=str(row.get('created_at') or ''),
        updated_at=str(row.get('updated_at') or ''),
    )


def _validate_confidence(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError('confidence 必须是 0 到 100 的整数')
    if not 0 <= value <= 100:
        raise ValueError('confidence 必须是 0 到 100 的整数')
    return value


def _validate_text(value: str, field: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{field} 必须是字符串')
    if not value:
        raise ValueError(f'{field} 不能为空')
    if len(value) > max_length:
        raise ValueError(f'{field} 长度不能超过 {max_length}')
    return value


def _validate_status(status: str) -> str:
    if status not in BINDING_STATUSES:
        raise ValueError(f'非法 binding 状态: {status}')
    return status


def _validate_origin(origin: str) -> str:
    if origin not in BINDING_ORIGINS:
        raise ValueError(f'非法 binding 来源: {origin}')
    return origin


def _validate_policy_mode(mode: str) -> str:
    if mode not in EPG_POLICY_MODES:
        raise ValueError(f'非法 EPG policy mode: {mode}')
    return mode


def _validate_source_id(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError('epg_source_id 必须是正整数')
    try:
        source_id = int(value)
    except (TypeError, ValueError) as error:
        raise TypeError('epg_source_id 必须是正整数') from error
    if source_id <= 0:
        raise ValueError('epg_source_id 必须是正整数')
    return source_id


def _target_exists(conn, target: EpgChannelIdentity) -> bool:
    row = conn.execute(
        "SELECT 1 FROM epg_channels WHERE source_id=? AND channel_id=? LIMIT 1",
        (target.source_id, target.channel_id),
    ).fetchone()
    return row is not None


def _logical_row(conn, logical_channel_id: str):
    return conn.execute(
        "SELECT id, canonical_key, status FROM iptv_logical_channels WHERE id=?",
        (logical_channel_id,),
    ).fetchone()


def _manageable_logical_row(conn, logical_channel_id: str):
    logical = _logical_row(conn, logical_channel_id)
    if logical is None:
        raise EpgBindingOperationError(
            'logical_channel_not_found',
            '逻辑频道不存在',
        )
    status = str(logical['status'] or '')
    if status in LOGICAL_CONFLICT_STATUSES:
        raise EpgBindingOperationError(
            'logical_channel_conflict',
            '逻辑频道处于 split/merge conflict 状态',
        )
    if status != 'active':
        raise EpgBindingOperationError(
            'logical_channel_inactive',
            '逻辑频道当前不可管理',
        )
    return logical


def _validate_management_target(conn, target: EpgChannelIdentity) -> None:
    source = conn.execute(
        'SELECT id FROM epg_sources WHERE id=?',
        (target.source_id,),
    ).fetchone()
    if source is None:
        raise EpgBindingOperationError(
            'epg_source_not_found',
            'EPG 来源不存在',
        )
    if _target_exists(conn, target):
        return
    same_channel_elsewhere = conn.execute(
        'SELECT 1 FROM epg_channels WHERE channel_id=? LIMIT 1',
        (target.channel_id,),
    ).fetchone()
    if same_channel_elsewhere is not None:
        raise EpgBindingOperationError(
            'invalid_composite_target',
            '该 EPG channel ID 不属于所选来源',
        )
    raise EpgBindingOperationError(
        'epg_channel_not_found',
        'EPG 频道不存在',
    )


def _upsert_policy(
    conn,
    logical_channel_id: str,
    mode: str,
) -> EpgLogicalChannelPolicy:
    mode = _validate_policy_mode(mode)
    now = db._utc_now()
    conn.execute(
        """
        INSERT INTO iptv_logical_channel_epg_policies(
            logical_channel_id, mode, created_at, updated_at
        ) VALUES(?, ?, ?, ?)
        ON CONFLICT(logical_channel_id) DO UPDATE SET
            mode=excluded.mode,
            updated_at=excluded.updated_at
        """,
        (logical_channel_id, mode, now, now),
    )
    row = conn.execute(
        _POLICY_SELECT + ' WHERE logical_channel_id=?',
        (logical_channel_id,),
    ).fetchone()
    return _policy_from_row(dict(row))


def _load_binding_rows(conn) -> list[dict]:
    return [dict(row) for row in conn.execute(_BINDING_SELECT + ' ORDER BY id').fetchall()]


async def get_epg_binding(logical_channel_id: str) -> EpgLogicalChannelBinding | None:
    logical_channel_id = _validate_text(logical_channel_id, 'logical_channel_id')

    def _get():
        conn = db._connect()
        try:
            row = conn.execute(
                _BINDING_SELECT + ' WHERE logical_channel_id=?',
                (logical_channel_id,),
            ).fetchone()
            return _binding_from_row(dict(row)) if row else None
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def list_epg_bindings() -> list[EpgLogicalChannelBinding]:
    def _list():
        conn = db._connect()
        try:
            return [_binding_from_row(row) for row in _load_binding_rows(conn)]
        finally:
            conn.close()

    return await asyncio.to_thread(_list)


async def get_epg_binding_policy(
    logical_channel_id: str,
) -> EpgLogicalChannelPolicy | None:
    logical_channel_id = _validate_text(
        logical_channel_id,
        'logical_channel_id',
    )

    def _get():
        conn = db._connect()
        try:
            row = conn.execute(
                _POLICY_SELECT + ' WHERE logical_channel_id=?',
                (logical_channel_id,),
            ).fetchone()
            return _policy_from_row(dict(row)) if row else None
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def list_epg_binding_policies() -> list[EpgLogicalChannelPolicy]:
    def _list():
        conn = db._connect()
        try:
            rows = conn.execute(
                _POLICY_SELECT + ' ORDER BY logical_channel_id'
            ).fetchall()
            return [_policy_from_row(dict(row)) for row in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_list)


async def validate_epg_binding_target(source_id: int, channel_id: str) -> bool:
    target = EpgChannelIdentity(int(source_id), _validate_text(channel_id, 'epg_channel_id'))

    def _validate():
        conn = db._connect()
        try:
            return _target_exists(conn, target)
        finally:
            conn.close()

    return await asyncio.to_thread(_validate)


async def create_matched_epg_binding(
    logical_channel_id: str,
    epg_source_id: int,
    epg_channel_id: str,
    *,
    match_type: str = '',
    confidence: int = 0,
    locked: bool = False,
    origin: str = 'manual',
    legacy_canonical_key: str = '',
    shadow_run_id: str | None = None,
) -> EpgLogicalChannelBinding:
    logical_channel_id = _validate_text(logical_channel_id, 'logical_channel_id')
    target = EpgChannelIdentity(int(epg_source_id), _validate_text(epg_channel_id, 'epg_channel_id'))
    confidence = _validate_confidence(confidence)
    match_type = '' if match_type is None else _validate_text(match_type, 'match_type') if match_type else ''
    origin = _validate_origin(origin)
    if not isinstance(locked, bool) and locked not in (0, 1):
        raise TypeError('locked 必须是布尔值')
    if shadow_run_id is not None and shadow_run_id != '':
        shadow_run_id = _validate_text(str(shadow_run_id), 'shadow_run_id')
    else:
        shadow_run_id = None
    if legacy_canonical_key is None or legacy_canonical_key == '':
        legacy_canonical_key = ''
    else:
        legacy_canonical_key = _validate_text(
            str(legacy_canonical_key), 'legacy_canonical_key'
        )

    def _create():
        conn = db._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            logical = _logical_row(conn, logical_channel_id)
            if logical is None:
                raise ValueError('logical channel 不存在')
            if logical['status'] in LOGICAL_CONFLICT_STATUSES:
                raise ValueError('logical channel 处于 conflict 状态')
            if origin == 'automatic' and logical['status'] != 'active':
                raise ValueError(f"automatic binding 仅允许 active logical channel（当前 {logical['status']}）")
            if not _target_exists(conn, target):
                raise ValueError('EPG composite target 不存在')
            existing = conn.execute(
                _BINDING_SELECT + ' WHERE logical_channel_id=?',
                (logical_channel_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError('logical channel 已有 binding')
            now = db._utc_now()
            cursor = conn.execute(
                """
                INSERT INTO iptv_logical_channel_epg_bindings(
                    logical_channel_id, epg_source_id, epg_channel_id,
                    status, match_type, confidence, locked, origin,
                    legacy_canonical_key, shadow_run_id, created_at, updated_at
                ) VALUES(?, ?, ?, 'matched', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    logical_channel_id,
                    target.source_id,
                    target.channel_id,
                    match_type,
                    confidence,
                    int(bool(locked)),
                    origin,
                    legacy_canonical_key,
                    shadow_run_id,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                _BINDING_SELECT + ' WHERE id=?',
                (cursor.lastrowid,),
            ).fetchone()
            conn.commit()
            return _binding_from_row(dict(row))
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_create)


async def set_manual_epg_binding(
    logical_channel_id: str,
    epg_source_id: int,
    epg_channel_id: str,
) -> tuple[str, EpgLogicalChannelBinding]:
    """Atomically create or replace one exact manual composite binding."""
    logical_channel_id = _validate_text(
        logical_channel_id,
        'logical_channel_id',
    )
    target = EpgChannelIdentity(
        _validate_source_id(epg_source_id),
        _validate_text(epg_channel_id, 'epg_channel_id'),
    )

    def _set():
        conn = db._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            _manageable_logical_row(conn, logical_channel_id)
            _validate_management_target(conn, target)
            existing = conn.execute(
                _BINDING_SELECT + ' WHERE logical_channel_id=?',
                (logical_channel_id,),
            ).fetchone()
            now = db._utc_now()
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO iptv_logical_channel_epg_bindings(
                        logical_channel_id, epg_source_id, epg_channel_id,
                        status, match_type, confidence, locked, origin,
                        shadow_run_id, legacy_canonical_key,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, 'matched', 'manual', 100, 1, 'manual',
                             NULL, '', ?, ?)
                    """,
                    (
                        logical_channel_id,
                        target.source_id,
                        target.channel_id,
                        now,
                        now,
                    ),
                )
                binding_id = int(cursor.lastrowid)
                action = 'created'
            else:
                binding_id = int(existing['id'])
                conn.execute(
                    """
                    UPDATE iptv_logical_channel_epg_bindings
                    SET epg_source_id=?, epg_channel_id=?, status='matched',
                        match_type='manual', confidence=100, locked=1,
                        origin='manual', shadow_run_id=NULL,
                        legacy_canonical_key='', updated_at=?
                    WHERE id=?
                    """,
                    (
                        target.source_id,
                        target.channel_id,
                        now,
                        binding_id,
                    ),
                )
                action = 'replaced'
            # Manual target selection supersedes an earlier explicit
            # automatic/no-EPG policy in the same transaction.
            conn.execute(
                """
                DELETE FROM iptv_logical_channel_epg_policies
                WHERE logical_channel_id=?
                """,
                (logical_channel_id,),
            )
            row = conn.execute(
                _BINDING_SELECT + ' WHERE id=?',
                (binding_id,),
            ).fetchone()
            conn.commit()
            return action, _binding_from_row(dict(row))
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_set)


async def set_epg_binding_locked(
    logical_channel_id: str,
    *,
    locked: bool,
) -> EpgLogicalChannelBinding:
    """Change only protection; unlocking deliberately retains the binding."""
    logical_channel_id = _validate_text(
        logical_channel_id,
        'logical_channel_id',
    )
    if not isinstance(locked, bool):
        raise TypeError('locked 必须是布尔值')

    def _set():
        conn = db._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            _manageable_logical_row(conn, logical_channel_id)
            current = conn.execute(
                _BINDING_SELECT + ' WHERE logical_channel_id=?',
                (logical_channel_id,),
            ).fetchone()
            if current is None:
                raise EpgBindingOperationError(
                    'binding_conflict',
                    '逻辑频道当前没有可锁定的 EPG binding',
                )
            current_target = EpgChannelIdentity(
                int(current['epg_source_id']),
                str(current['epg_channel_id']),
            )
            if locked and not _target_exists(conn, current_target):
                raise EpgBindingOperationError(
                    'orphan_target',
                    '当前 EPG binding target 已不存在，请先替换或恢复自动匹配',
                )
            conn.execute(
                """
                UPDATE iptv_logical_channel_epg_bindings
                SET locked=?, updated_at=? WHERE logical_channel_id=?
                """,
                (int(locked), db._utc_now(), logical_channel_id),
            )
            row = conn.execute(
                _BINDING_SELECT + ' WHERE logical_channel_id=?',
                (logical_channel_id,),
            ).fetchone()
            conn.commit()
            return _binding_from_row(dict(row))
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_set)


async def set_epg_binding_management_mode(
    logical_channel_id: str,
    mode: str,
) -> dict:
    """Remove any target and persist explicit automatic or no-EPG intent."""
    logical_channel_id = _validate_text(
        logical_channel_id,
        'logical_channel_id',
    )
    mode = _validate_policy_mode(mode)

    def _set():
        conn = db._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            _manageable_logical_row(conn, logical_channel_id)
            current = conn.execute(
                _BINDING_SELECT + ' WHERE logical_channel_id=?',
                (logical_channel_id,),
            ).fetchone()
            previous = _binding_from_row(dict(current)) if current else None
            conn.execute(
                """
                DELETE FROM iptv_logical_channel_epg_bindings
                WHERE logical_channel_id=?
                """,
                (logical_channel_id,),
            )
            policy = _upsert_policy(conn, logical_channel_id, mode)
            conn.commit()
            return {
                'logical_channel_id': logical_channel_id,
                'mode': mode,
                'binding_removed': previous is not None,
                'previous_binding': previous,
                'policy': policy,
            }
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_set)


async def update_epg_binding_status(
    logical_channel_id: str,
    status: str,
) -> EpgLogicalChannelBinding | None:
    logical_channel_id = _validate_text(logical_channel_id, 'logical_channel_id')
    status = _validate_status(status)

    def _update():
        conn = db._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute(
                _BINDING_SELECT + ' WHERE logical_channel_id=?',
                (logical_channel_id,),
            ).fetchone()
            if current is None:
                conn.commit()
                return None
            current_dict = dict(current)
            target = EpgChannelIdentity(
                int(current_dict['epg_source_id']),
                str(current_dict['epg_channel_id']),
            )
            if status == 'matched' and not _target_exists(conn, target):
                raise ValueError('EPG composite target 不存在')
            conn.execute(
                "UPDATE iptv_logical_channel_epg_bindings SET status=?, updated_at=? WHERE logical_channel_id=?",
                (status, db._utc_now(), logical_channel_id),
            )
            row = conn.execute(
                _BINDING_SELECT + ' WHERE logical_channel_id=?',
                (logical_channel_id,),
            ).fetchone()
            conn.commit()
            return _binding_from_row(dict(row))
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    return await asyncio.to_thread(_update)


async def delete_epg_binding(logical_channel_id: str) -> bool:
    logical_channel_id = _validate_text(logical_channel_id, 'logical_channel_id')

    def _delete():
        conn = db._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM iptv_logical_channel_epg_bindings WHERE logical_channel_id=?",
                    (logical_channel_id,),
                )
                return cursor.rowcount == 1
        finally:
            conn.close()

    return await asyncio.to_thread(_delete)


async def list_orphan_target_bindings() -> list[EpgLogicalChannelBinding]:
    def _list():
        conn = db._connect()
        try:
            rows = conn.execute(
                _BINDING_SELECT + " b WHERE NOT EXISTS (SELECT 1 FROM epg_channels c WHERE c.source_id=b.epg_source_id AND c.channel_id=b.epg_channel_id) ORDER BY b.id"
            ).fetchall()
            return [_binding_from_row(dict(row)) for row in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_list)


async def list_conflict_bindings() -> list[EpgLogicalChannelBinding]:
    def _list():
        conn = db._connect()
        try:
            rows = conn.execute(
                _BINDING_SELECT + " WHERE status='conflict' ORDER BY id"
            ).fetchall()
            return [_binding_from_row(dict(row)) for row in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_list)


async def validate_epg_binding_shadow() -> dict:
    def _validate():
        conn = db._connect()
        try:
            bindings = _load_binding_rows(conn)
            logical_rows = {
                str(row['id']): dict(row)
                for row in conn.execute('SELECT id, status FROM iptv_logical_channels').fetchall()
            }
            targets = {
                EpgChannelIdentity(int(row['source_id']), str(row['channel_id']))
                for row in conn.execute('SELECT source_id, channel_id FROM epg_channels').fetchall()
            }
            valid_target_count = 0
            orphan_target_count = 0
            logical_orphan_count = 0
            conflict_count = 0
            cross_source_resolution_errors = 0
            orphan_samples = []
            for row in bindings:
                target = EpgChannelIdentity(int(row['epg_source_id']), str(row['epg_channel_id']))
                if target in targets:
                    valid_target_count += 1
                else:
                    orphan_target_count += 1
                    if len(orphan_samples) < 10:
                        orphan_samples.append(_safe_summary({
                            'mapping_id': row['id'],
                            'reason': 'orphan_target',
                            'logical_channel_id': row['logical_channel_id'],
                            'canonical_key': row['legacy_canonical_key'],
                            'epg_source_id': row['epg_source_id'],
                            'epg_channel_id': row['epg_channel_id'],
                        }))
                    same_channel_other_source = conn.execute(
                        "SELECT 1 FROM epg_channels WHERE channel_id=? AND source_id<>? LIMIT 1",
                        (row['epg_channel_id'], row['epg_source_id']),
                    ).fetchone()
                    cross_source_resolution_errors += int(same_channel_other_source is not None)
                logical = logical_rows.get(str(row['logical_channel_id']))
                if logical is None or logical['status'] == 'orphaned':
                    logical_orphan_count += 1
                if row['status'] == 'conflict' or (logical and logical['status'] in LOGICAL_CONFLICT_STATUSES):
                    conflict_count += 1
            duplicate_rows = conn.execute(
                """
                SELECT COALESCE(SUM(binding_count - 1), 0) AS duplicate_count
                FROM (
                    SELECT logical_channel_id, COUNT(*) AS binding_count
                    FROM iptv_logical_channel_epg_bindings
                    GROUP BY logical_channel_id
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()
            return {
                'binding_count': len(bindings),
                'valid_target_count': valid_target_count,
                'orphan_target_count': orphan_target_count,
                'logical_orphan_count': logical_orphan_count,
                'conflict_count': conflict_count,
                'locked_count': sum(bool(row['locked']) for row in bindings),
                'migrated_count': sum(row['origin'] == 'legacy_migrated' for row in bindings),
                'duplicate_logical_binding_count': int(duplicate_rows['duplicate_count'] or 0),
                'cross_source_resolution_errors': cross_source_resolution_errors,
                'orphan_samples': orphan_samples,
            }
        finally:
            conn.close()

    return await asyncio.to_thread(_validate)

# EPG-2E: read-only lifecycle reconciliation for logical-channel changes.
RECONCILIATION_CATEGORIES = (
    'unchanged',
    'logical_orphan',
    'split_no_inherit',
    'merge_no_binding',
    'merge_single_binding',
    'merge_same_target',
    'merge_conflicting_targets',
    'locked_conflict',
)


def _reconciliation_binding_payload(row: Mapping[str, object]) -> dict:
    # Keep this contract deliberately limited to identity and binding
    # metadata. In particular, it never exposes source URLs or playback data.
    return {
        'id': int(row['id']),
        'logical_channel_id': str(row['logical_channel_id']),
        'epg_source_id': int(row['epg_source_id']),
        'epg_channel_id': str(row['epg_channel_id']),
        'status': str(row.get('status') or ''),
        'match_type': str(row.get('match_type') or ''),
        'confidence': int(row.get('confidence') or 0),
        'locked': bool(row.get('locked')),
        'origin': str(row.get('origin') or ''),
        'shadow_run_id': str(row['shadow_run_id']) if row.get('shadow_run_id') else None,
    }


def _reconciliation_binding_target(row: Mapping[str, object]) -> EpgChannelIdentity:
    return EpgChannelIdentity(int(row['epg_source_id']), str(row['epg_channel_id']))


def _reconciliation_item(category: str, logical_ids: Iterable[str], bindings: list[Mapping[str, object]], **extra: object) -> dict:
    item = {
        'category': category,
        'logical_channel_ids': sorted({str(value) for value in logical_ids}),
        'binding_count': len(bindings),
        'bindings': [_reconciliation_binding_payload(row) for row in bindings],
    }
    item.update(extra)
    return item


def _merge_groups_from_sync_result(sync_result: Mapping[str, object] | None) -> list[dict]:
    if not isinstance(sync_result, Mapping):
        return []
    raw_groups = sync_result.get('merge_conflicts') or []
    groups = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, Mapping):
            continue
        logical_ids = sorted({str(value) for value in (raw_group.get('logical_channel_ids') or []) if str(value)})
        if logical_ids:
            groups.append({
                'logical_channel_ids': logical_ids,
                'canonical_key': str(raw_group.get('canonical_key') or ''),
                'inferred': False,
            })
    return groups


def _inferred_merge_groups(
    logical_rows: Iterable[Mapping[str, object]],
    member_rows: Iterable[Mapping[str, object]],
) -> list[dict]:
    """Infer independent merge groups from the current raw member projection.

    A sync result is the authoritative source for merge-group membership when
    available. Without one, group only merge-conflict logical IDs whose
    current members share the same production normalization key. Ambiguous or
    empty projections remain isolated rather than being merged by database
    order or by the fact that they share a status.
    """
    merge_ids = sorted(
        str(row['id'])
        for row in logical_rows
        if str(row.get('status') or '') == 'merge_conflict'
    )
    if not merge_ids:
        return []
    merge_id_set = set(merge_ids)
    keys_by_logical: dict[str, set[str]] = defaultdict(set)
    for row in member_rows:
        logical_id = str(row.get('logical_channel_id') or '')
        if logical_id not in merge_id_set:
            continue
        raw_name = str(row.get('raw_name') or '')
        key = normalize_channel_name(raw_name) if raw_name else ''
        if key:
            keys_by_logical[logical_id].add(key)

    grouped: dict[str, set[str]] = defaultdict(set)
    singleton_ids: set[str] = set()
    for logical_id in merge_ids:
        keys = keys_by_logical.get(logical_id, set())
        if len(keys) == 1:
            grouped[next(iter(keys))].add(logical_id)
        else:
            # There is insufficient continuity evidence to safely join this
            # logical ID to another inferred group.
            singleton_ids.add(logical_id)

    groups = [
        {
            'logical_channel_ids': sorted(logical_ids),
            'canonical_key': key,
            'inferred': True,
        }
        for key, logical_ids in grouped.items()
    ]
    groups.extend(
        {
            'logical_channel_ids': [logical_id],
            'canonical_key': '',
            'inferred': True,
        }
        for logical_id in sorted(singleton_ids)
    )
    return sorted(groups, key=lambda group: (group['canonical_key'], group['logical_channel_ids']))


async def preview_epg_binding_reconciliation(sync_result: Mapping[str, object] | None = None) -> dict:
    """Return a read-only binding lifecycle reconciliation preview.

    ``sync_result`` may be the result of ``sync_iptv_logical_channels``. Its
    merge groups provide the strongest available continuity evidence. When it
    is omitted, current merge-conflict members are grouped by their shared
    production normalization key; insufficient evidence remains isolated.
    No binding is copied, moved, updated, or deleted.
    """
    def _preview():
        conn = db._connect()
        try:
            logical_rows = [dict(row) for row in conn.execute(
                'SELECT id, canonical_key, display_name, status FROM iptv_logical_channels ORDER BY id'
            ).fetchall()]
            binding_rows = [dict(row) for row in conn.execute(
                _BINDING_SELECT + ' ORDER BY logical_channel_id, id'
            ).fetchall()]
            member_rows = [dict(row) for row in conn.execute(
                """
                SELECT m.logical_channel_id, c.name AS raw_name
                FROM iptv_logical_channel_members AS m
                JOIN channels AS c ON c.id = m.channel_id
                ORDER BY m.logical_channel_id, m.channel_id
                """
            ).fetchall()]
        finally:
            conn.close()

        logical_by_id = {str(row['id']): row for row in logical_rows}
        bindings_by_logical: dict[str, list[dict]] = defaultdict(list)
        for row in binding_rows:
            bindings_by_logical[str(row['logical_channel_id'])].append(row)

        counts = {category: 0 for category in RECONCILIATION_CATEGORIES}
        items: list[dict] = []
        handled: set[str] = set()

        def add(category: str, logical_ids: Iterable[str], bindings: list[Mapping[str, object]], **extra: object) -> None:
            counts[category] += 1
            items.append(_reconciliation_item(category, logical_ids, bindings, **extra))

        # A split is never inherited. Report the logical channel even when it
        # has no binding, because the absence of inheritance is itself the
        # auditable decision.
        split_ids = {
            str(row['id']) for row in logical_rows
            if str(row['status'] or '') == 'split_conflict'
        }
        if isinstance(sync_result, Mapping):
            for raw_group in sync_result.get('split_conflicts') or []:
                if isinstance(raw_group, Mapping):
                    value = raw_group.get('logical_channel_id')
                    if value:
                        split_ids.add(str(value))
        for logical_id in sorted(split_ids):
            if logical_id not in logical_by_id:
                continue
            handled.add(logical_id)
            add(
                'split_no_inherit',
                [logical_id],
                bindings_by_logical.get(logical_id, []),
                logical_status=logical_by_id[logical_id]['status'],
                inheritance='none',
            )

        merge_groups = _merge_groups_from_sync_result(sync_result)
        if not merge_groups:
            merge_groups = _inferred_merge_groups(logical_rows, member_rows)
        for group in merge_groups:
            logical_ids = group['logical_channel_ids']
            group_bindings = [
                binding
                for logical_id in logical_ids
                for binding in bindings_by_logical.get(logical_id, [])
            ]
            for logical_id in logical_ids:
                handled.add(logical_id)
            targets = {_reconciliation_binding_target(row) for row in group_bindings}
            locked = any(bool(row.get('locked')) for row in group_bindings)
            if not group_bindings:
                category = 'merge_no_binding'
            elif len(group_bindings) == 1:
                category = 'merge_single_binding'
            elif len(targets) == 1:
                category = 'merge_same_target'
            elif locked:
                category = 'locked_conflict'
            else:
                category = 'merge_conflicting_targets'
            # A single binding is deliberately conservative and is not an
            # inheritance instruction; it remains attached to its old ID.
            add(
                category,
                logical_ids,
                group_bindings,
                canonical_key=group.get('canonical_key', ''),
                inferred=bool(group.get('inferred')),
                safe_same_target=(category == 'merge_same_target'),
                automatic_action='none',
            )

        # Orphaned logical IDs retain their bindings, but cannot receive new
        # automatic bindings. This is separate from split/merge handling.
        for row in logical_rows:
            logical_id = str(row['id'])
            if logical_id in handled:
                continue
            if str(row['status'] or '') == 'orphaned':
                handled.add(logical_id)
                add(
                    'logical_orphan',
                    [logical_id],
                    bindings_by_logical.get(logical_id, []),
                    logical_status='orphaned',
                    automatic_action='forbidden',
                )

        # Existing active bindings are unchanged by this preview. Logical
        # channels without a binding have no binding lifecycle action to report.
        for logical_id in sorted(bindings_by_logical):
            if logical_id in handled or logical_id not in logical_by_id:
                continue
            if str(logical_by_id[logical_id]['status'] or '') == 'active':
                add('unchanged', [logical_id], bindings_by_logical[logical_id], automatic_action='none')
                handled.add(logical_id)

        return {
            'readonly': True,
            'logical_channel_count': len(logical_rows),
            'binding_count': len(binding_rows),
            'counts': counts,
            'items': items,
        }

    return await asyncio.to_thread(_preview)
