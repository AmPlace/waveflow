"""Product-facing EPG source lifecycle without secret-bearing projections."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, Mapping

import database as db
from epg_preference_evidence import reconcile_url_tvg_source_resolutions
from epg_source_model import (
    BUILTIN_EPG_SOURCE_PRESETS,
    validate_epg_source_name,
    validate_epg_source_url,
)
from epg_tasks import EPG_RECONCILIATION_LOCK, epg_task_id


logger = logging.getLogger(__name__)
_UNSET = object()
MAX_DELETE_SAMPLES = 20


class EpgSourceManagementError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            **dict(self.details),
        }


def _invalid_name(error: BaseException) -> EpgSourceManagementError:
    return EpgSourceManagementError("invalid_name", str(error), 422)


def _invalid_url() -> EpgSourceManagementError:
    return EpgSourceManagementError(
        "invalid_url",
        "请输入有效的 HTTP 或 HTTPS XMLTV 地址",
        422,
    )


async def _reconcile_url_evidence_safely() -> bool:
    try:
        await reconcile_url_tvg_source_resolutions()
        return True
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning(
            "epg_source_preference_reconcile_deferred",
            extra={"epg_source_management": {"error_type": type(error).__name__}},
        )
        return False


async def ensure_builtin_epg_sources() -> list[dict]:
    async with EPG_RECONCILIATION_LOCK:
        for preset in BUILTIN_EPG_SOURCE_PRESETS:
            try:
                await db.ensure_builtin_epg_source(
                    builtin_key=preset.key,
                    name=preset.name,
                    url=preset.url,
                )
            except sqlite3.IntegrityError as error:
                raise EpgSourceManagementError(
                    "builtin_preset_conflict",
                    "内置 EPG 预设与现有来源冲突，请重建当前开发数据库",
                    409,
                ) from error
        sources = await db.get_epg_sources()
    await _reconcile_url_evidence_safely()
    return sources


async def create_custom_epg_source(
    *,
    name: object,
    url: object,
    enabled: object = True,
) -> tuple[dict, bool]:
    try:
        normalized_name = validate_epg_source_name(name)
    except (TypeError, ValueError) as error:
        raise _invalid_name(error) from error
    try:
        normalized_url = validate_epg_source_url(url)
    except (TypeError, ValueError) as error:
        raise _invalid_url() from error
    if not isinstance(enabled, bool):
        raise EpgSourceManagementError(
            "invalid_enabled",
            "enabled 必须是布尔值",
            422,
        )
    async with EPG_RECONCILIATION_LOCK:
        try:
            source_id = await db.add_epg_source(
                normalized_name,
                normalized_url,
                enabled=enabled,
                source_origin="custom",
            )
        except sqlite3.IntegrityError as error:
            raise EpgSourceManagementError(
                "source_url_exists",
                "该 EPG URL 已存在",
                409,
            ) from error
    preference_reconciled = await _reconcile_url_evidence_safely()
    source = await db.get_epg_source(source_id)
    if source is None:
        raise EpgSourceManagementError(
            "source_not_found",
            "EPG 来源创建后不可用",
            500,
        )
    return source, preference_reconciled


async def update_managed_epg_source(
    source_id: int,
    *,
    name: object = _UNSET,
    url: object = _UNSET,
    enabled: object = _UNSET,
) -> tuple[dict, bool]:
    async with EPG_RECONCILIATION_LOCK:
        source = await db.get_epg_source(source_id)
        if source is None:
            raise EpgSourceManagementError(
                "source_not_found",
                "EPG 来源不存在",
                404,
            )
        if name is _UNSET and url is _UNSET and enabled is _UNSET:
            raise EpgSourceManagementError(
                "empty_update",
                "至少提供一个需要修改的字段",
                422,
            )
        updates: dict[str, Any] = {}
        if name is not _UNSET:
            try:
                updates["name"] = validate_epg_source_name(name)
            except (TypeError, ValueError) as error:
                raise _invalid_name(error) from error
        if url is not _UNSET:
            if source.get("source_origin") == "builtin":
                raise EpgSourceManagementError(
                    "builtin_url_managed",
                    "内置 EPG 来源 URL 由 WaveFlow 管理",
                    409,
                )
            try:
                updates["url"] = validate_epg_source_url(url)
            except (TypeError, ValueError) as error:
                raise _invalid_url() from error
        if enabled is not _UNSET:
            if not isinstance(enabled, bool):
                raise EpgSourceManagementError(
                    "invalid_enabled",
                    "enabled 必须是布尔值",
                    422,
                )
            updates["enabled"] = enabled
        try:
            updated = await db.update_epg_source(source_id, **updates)
        except sqlite3.IntegrityError as error:
            raise EpgSourceManagementError(
                "source_url_exists",
                "该 EPG URL 已存在",
                409,
            ) from error
        if updated is None:
            raise EpgSourceManagementError(
                "source_not_found",
                "EPG 来源不存在",
                404,
            )
    preference_reconciled = True
    if "url" in updates:
        preference_reconciled = await _reconcile_url_evidence_safely()
    return updated, preference_reconciled


def _source_delete_impact_sync(
    conn: sqlite3.Connection,
    source_id: int,
) -> dict[str, Any]:
    source = conn.execute(
        """
        SELECT id, name, source_origin, builtin_key
        FROM epg_sources WHERE id=?
        """,
        (source_id,),
    ).fetchone()
    if source is None:
        raise EpgSourceManagementError(
            "source_not_found",
            "EPG 来源不存在",
            404,
        )
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS binding_count,
            SUM(CASE WHEN lc.status='active' THEN 1 ELSE 0 END) AS active_binding_count,
            SUM(CASE WHEN b.origin='automatic' THEN 1 ELSE 0 END) AS automatic_binding_count,
            SUM(CASE WHEN b.origin='manual' THEN 1 ELSE 0 END) AS manual_binding_count,
            SUM(CASE WHEN b.locked=1 THEN 1 ELSE 0 END) AS locked_binding_count,
            SUM(CASE WHEN b.origin='manual' OR b.locked=1 THEN 1 ELSE 0 END) AS protected_binding_count,
            COUNT(DISTINCT CASE WHEN lc.status='active' THEN lc.id END) AS affected_active_logical_count
        FROM iptv_logical_channel_epg_bindings AS b
        LEFT JOIN iptv_logical_channels AS lc ON lc.id=b.logical_channel_id
        WHERE b.epg_source_id=?
        """,
        (source_id,),
    ).fetchone()
    preference_count = int(conn.execute(
        """
        SELECT COUNT(*) FROM epg_source_preference_evidence
        WHERE epg_source_id=? AND valid=1 AND current=1
        """,
        (source_id,),
    ).fetchone()[0])
    dataset = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM epg_channels WHERE source_id=?) AS channel_count,
            (SELECT COUNT(*) FROM epg_programs WHERE source_id=?) AS programme_count
        """,
        (source_id, source_id),
    ).fetchone()
    task_id = epg_task_id(source_id)
    task = conn.execute(
        """
        SELECT enabled FROM automation_task_config WHERE task_id=?
        """,
        (task_id,),
    ).fetchone()
    affected_rows = conn.execute(
        """
        SELECT lc.id AS logical_channel_id, lc.display_name,
               b.origin, b.locked
        FROM iptv_logical_channel_epg_bindings AS b
        JOIN iptv_logical_channels AS lc ON lc.id=b.logical_channel_id
        WHERE b.epg_source_id=? AND lc.status='active'
        ORDER BY LOWER(lc.display_name), lc.id
        LIMIT ?
        """,
        (source_id, MAX_DELETE_SAMPLES),
    ).fetchall()
    protected_count = int(counts['protected_binding_count'] or 0)
    is_builtin = source['source_origin'] == 'builtin'
    return {
        "source_id": int(source['id']),
        "source_name": str(source['name'] or ''),
        "source_origin": str(source['source_origin'] or 'custom'),
        "builtin_key": str(source['builtin_key'] or ''),
        "can_delete": not is_builtin,
        "requires_confirmation": protected_count > 0,
        "binding_count": int(counts['binding_count'] or 0),
        "active_bindings_count": int(counts['active_binding_count'] or 0),
        "automatic_bindings_count": int(counts['automatic_binding_count'] or 0),
        "manual_bindings_count": int(counts['manual_binding_count'] or 0),
        "locked_bindings_count": int(counts['locked_binding_count'] or 0),
        "protected_bindings_count": protected_count,
        "preference_references": preference_count,
        "affected_active_logical_channel_count": int(
            counts['affected_active_logical_count'] or 0
        ),
        "affected_active_logical_channels": [
            {
                "logical_channel_id": str(row['logical_channel_id']),
                "display_name": str(row['display_name'] or ''),
                "origin": str(row['origin'] or ''),
                "locked": bool(row['locked']),
            }
            for row in affected_rows
        ],
        "dataset": {
            "channel_count": int(dataset['channel_count'] or 0),
            "programme_count": int(dataset['programme_count'] or 0),
        },
        "automation_task": {
            "task_id": task_id,
            "exists": task is not None,
            "enabled": bool(task['enabled']) if task is not None else False,
        },
    }


async def preview_epg_source_delete(source_id: int) -> dict[str, Any]:
    def _preview():
        conn = db._connect()
        try:
            conn.execute('BEGIN')
            result = _source_delete_impact_sync(conn, source_id)
            conn.commit()
            return result
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    async with EPG_RECONCILIATION_LOCK:
        return await asyncio.to_thread(_preview)


async def delete_custom_epg_source(
    source_id: int,
    *,
    confirm: bool = False,
) -> tuple[dict[str, Any], bool]:
    if not isinstance(confirm, bool):
        raise EpgSourceManagementError(
            "invalid_confirmation",
            "confirm 必须是布尔值",
            422,
        )

    def _delete():
        conn = db._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            impact = _source_delete_impact_sync(conn, source_id)
            if impact['source_origin'] == 'builtin':
                raise EpgSourceManagementError(
                    "builtin_delete_forbidden",
                    "内置 EPG 来源不能删除",
                    409,
                )
            if impact['requires_confirmation'] and not confirm:
                raise EpgSourceManagementError(
                    "delete_confirmation_required",
                    "该来源包含 manual 或 locked binding，需要明确确认删除",
                    409,
                    {"impact": impact},
                )
            deleted_bindings = conn.execute(
                """
                DELETE FROM iptv_logical_channel_epg_bindings
                WHERE epg_source_id=? AND locked=0 AND origin<>'manual'
                """,
                (source_id,),
            ).rowcount
            orphaned_bindings = conn.execute(
                """
                UPDATE iptv_logical_channel_epg_bindings
                SET status='orphan_target', updated_at=?
                WHERE epg_source_id=?
                """,
                (db._utc_now(), source_id),
            ).rowcount
            deleted_source = conn.execute(
                "DELETE FROM epg_sources WHERE id=?",
                (source_id,),
            ).rowcount
            if deleted_source != 1:
                raise EpgSourceManagementError(
                    "source_not_found",
                    "EPG 来源不存在",
                    404,
                )
            conn.commit()
            return {
                "source_id": source_id,
                "status": "deleted",
                "deleted_bindings_count": int(deleted_bindings),
                "orphaned_protected_bindings_count": int(orphaned_bindings),
                "retained_preference_references": int(
                    impact['preference_references']
                ),
                "deleted_channel_count": int(impact['dataset']['channel_count']),
                "deleted_programme_count": int(
                    impact['dataset']['programme_count']
                ),
                "impact": impact,
            }
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    async with EPG_RECONCILIATION_LOCK:
        try:
            result = await asyncio.to_thread(_delete)
        except sqlite3.DatabaseError as error:
            raise EpgSourceManagementError(
                "source_delete_failed",
                "EPG 来源删除失败",
                500,
            ) from error
    preference_reconciled = await _reconcile_url_evidence_safely()
    return result, preference_reconciled


__all__ = [
    "EpgSourceManagementError",
    "create_custom_epg_source",
    "delete_custom_epg_source",
    "ensure_builtin_epg_sources",
    "preview_epg_source_delete",
    "update_managed_epg_source",
]
