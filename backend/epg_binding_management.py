"""Product-facing logical EPG binding and preference write operations."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Awaitable, Callable, Mapping

import epg_bindings
import epg_maintenance
from epg_preference_evidence import (
    EpgPreferenceEvidenceError,
    build_epg_source_preference_snapshot,
    clear_subscription_manual_source_preference,
    set_subscription_manual_source_preference,
)
from epg_source_preference import EpgSourcePreferenceResolution


class EpgBindingManagementError(Exception):
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
            'code': self.code,
            'message': self.message,
            **self.details,
        }


_DOMAIN_STATUS = {
    'logical_channel_not_found': 404,
    'logical_channel_conflict': 409,
    'logical_channel_inactive': 409,
    'epg_source_not_found': 404,
    'epg_channel_not_found': 404,
    'invalid_composite_target': 422,
    'binding_conflict': 409,
    'orphan_target': 409,
    'preference_invalid': 422,
}


def _translate_domain_error(error: BaseException) -> EpgBindingManagementError:
    if isinstance(error, (
        epg_bindings.EpgBindingOperationError,
        EpgPreferenceEvidenceError,
    )):
        code = error.code
        return EpgBindingManagementError(
            code,
            error.message,
            _DOMAIN_STATUS.get(code, 400),
        )
    if isinstance(error, (TypeError, ValueError)):
        return EpgBindingManagementError(
            'invalid_request',
            '请求参数无效',
            422,
        )
    if isinstance(error, sqlite3.DatabaseError):
        return EpgBindingManagementError(
            'binding_write_failed',
            'EPG binding 操作失败',
            500,
        )
    return EpgBindingManagementError(
        'binding_write_failed',
        'EPG binding 操作失败',
        500,
    )


async def _run_domain(operation: Callable[[], Awaitable[Any]]) -> Any:
    try:
        return await operation()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise _translate_domain_error(error) from error


async def bind_manual_epg_target(
    *,
    logical_channel_id: str,
    epg_source_id: int,
    epg_channel_id: str,
) -> dict[str, Any]:
    action, binding = await _run_domain(lambda: epg_bindings.set_manual_epg_binding(
        logical_channel_id,
        epg_source_id,
        epg_channel_id,
    ))
    return {
        'operation': 'manual_bind',
        'action': action,
        'binding': binding.as_dict(),
        'policy': {
            'mode': 'manual',
            'explicit': True,
        },
    }


async def set_logical_epg_binding_lock(
    *,
    logical_channel_id: str,
    locked: bool,
) -> dict[str, Any]:
    binding = await _run_domain(lambda: epg_bindings.set_epg_binding_locked(
        logical_channel_id,
        locked=locked,
    ))
    return {
        'operation': 'lock' if locked else 'unlock',
        'binding': binding.as_dict(),
        'restore_automatic_required': not locked,
    }


def _management_state_projection(result: Mapping[str, Any]) -> dict[str, Any]:
    policy = result.get('policy')
    previous = result.get('previous_binding')
    return {
        'logical_channel_id': str(result.get('logical_channel_id') or ''),
        'mode': str(result.get('mode') or ''),
        'binding_removed': bool(result.get('binding_removed')),
        'previous_target': (
            previous.target.as_dict()
            if isinstance(previous, epg_bindings.EpgLogicalChannelBinding)
            else None
        ),
        'policy': (
            policy.as_dict()
            if isinstance(policy, epg_bindings.EpgLogicalChannelPolicy)
            else None
        ),
    }


def _maintenance_projection(result: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(result or {})
    shadow = result.get('shadow_run') or {}
    apply = result.get('apply') or {}
    status = str(result.get('status') or 'failed')
    return {
        'status': status,
        'code': '' if status == 'success' else 'maintenance_degraded',
        'run_id': str(shadow.get('run_id') or ''),
        'run_status': str(shadow.get('status') or ''),
        'applied_count': int(apply.get('applied_count') or 0),
        'already_bound_count': int(apply.get('already_bound_count') or 0),
    }


async def restore_logical_epg_automatic(
    *,
    logical_channel_id: str,
) -> dict[str, Any]:
    state = await _run_domain(lambda: epg_bindings.set_epg_binding_management_mode(
        logical_channel_id,
        'automatic',
    ))
    try:
        maintenance = await epg_maintenance.run_epg_binding_maintenance(
            sync_logical=False,
            trigger='epg_manual_restore',
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        maintenance = {'status': 'failed'}
    binding = await epg_bindings.get_epg_binding(logical_channel_id)
    maintenance_projection = _maintenance_projection(maintenance)
    return {
        'operation': 'restore_automatic',
        **_management_state_projection(state),
        'binding': binding.as_dict() if binding else None,
        'maintenance': maintenance_projection,
        'warning': (
            {
                'code': 'maintenance_degraded',
                'message': '已恢复自动管理，自动匹配维护将在后续重试',
            }
            if maintenance_projection['status'] != 'success'
            else None
        ),
    }


async def disable_logical_epg(
    *,
    logical_channel_id: str,
) -> dict[str, Any]:
    state = await _run_domain(lambda: epg_bindings.set_epg_binding_management_mode(
        logical_channel_id,
        'no_epg',
    ))
    return {
        'operation': 'disable_epg',
        **_management_state_projection(state),
        'binding': None,
    }


def _preference_projection(
    resolution: EpgSourcePreferenceResolution,
) -> dict[str, Any]:
    return {
        'status': resolution.status,
        'preferred_source_id': resolution.preferred_source_id,
        'origin': resolution.origin or '',
        'competing_source_ids': sorted({
            item.epg_source_id for item in resolution.competing_preferences
        }),
    }


async def _subscription_preference_resolution(
    subscription_id: int,
) -> EpgSourcePreferenceResolution:
    snapshots = await build_epg_source_preference_snapshot()
    item = next((
        row for row in snapshots
        if row.subscription_id == subscription_id
        and not row.logical_channel_id
    ), None)
    return (
        item.resolution
        if item is not None
        else EpgSourcePreferenceResolution(None, 'none')
    )


async def set_subscription_epg_source_preference(
    *,
    subscription_id: int,
    epg_source_id: int,
) -> dict[str, Any]:
    write = await _run_domain(
        lambda: set_subscription_manual_source_preference(
            subscription_id=subscription_id,
            epg_source_id=epg_source_id,
        )
    )
    resolution = await _subscription_preference_resolution(subscription_id)
    return {
        'operation': 'set_manual_preference',
        'context': {
            'type': 'subscription',
            'subscription_id': subscription_id,
        },
        'epg_source_id': epg_source_id,
        'replaced_manual_count': int(write['replaced_manual_count']),
        'resolution': _preference_projection(resolution),
        'binding_changed': False,
    }


async def clear_subscription_epg_source_preference(
    *,
    subscription_id: int,
) -> dict[str, Any]:
    write = await _run_domain(
        lambda: clear_subscription_manual_source_preference(
            subscription_id=subscription_id,
        )
    )
    resolution = await _subscription_preference_resolution(subscription_id)
    return {
        'operation': 'clear_manual_preference',
        'context': {
            'type': 'subscription',
            'subscription_id': subscription_id,
        },
        'cleared_manual_count': int(write['cleared_manual_count']),
        'preserved_derived_count': int(write['preserved_derived_count']),
        'resolution': _preference_projection(resolution),
        'binding_changed': False,
    }


__all__ = [
    'EpgBindingManagementError',
    'bind_manual_epg_target',
    'clear_subscription_epg_source_preference',
    'disable_logical_epg',
    'restore_logical_epg_automatic',
    'set_logical_epg_binding_lock',
    'set_subscription_epg_source_preference',
]
