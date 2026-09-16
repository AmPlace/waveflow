"""Production lifecycle maintenance for logical EPG bindings.

The maintenance chain is deliberately separate from dataset refresh and raw
channel writes.  A maintenance failure is retryable and must not undo either
already-committed dataset or channel data.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import epg_match_shadow
import iptv_logical_gc
import iptv_channels


logger = logging.getLogger(__name__)
_maintenance_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(error: BaseException) -> str:
    return f'{type(error).__name__}: {str(error).replace(chr(0), "")[:512]}'


async def run_epg_binding_maintenance(
    *,
    sync_logical: bool = True,
    trigger: str = 'lifecycle',
) -> dict[str, Any]:
    """Run one serialized, idempotent logical binding maintenance pass.

    ``sync_logical`` is enabled for channel and EPG lifecycle events so the
    matcher always sees the latest logical projection. No binding is changed
    except through the existing safe apply operation.
    """
    started_at = _now()
    result: dict[str, Any] = {
        'trigger': trigger,
        'status': 'success',
        'started_at': started_at,
        'finished_at': '',
        'logical_sync': None,
        'logical_gc': None,
        'shadow_run': None,
        'apply': None,
        'error': '',
    }
    async with _maintenance_lock:
        try:
            if sync_logical:
                result['logical_sync'] = await iptv_channels.sync_iptv_logical_channels()
            result['logical_gc'] = await iptv_logical_gc.garbage_collect_iptv_logical_channels()
            result['shadow_run'] = await epg_match_shadow.run_epg_match_shadow()
            shadow_run = result['shadow_run']
            run_id = shadow_run.get('run_id') if isinstance(shadow_run, dict) else None
            shadow_status = shadow_run.get('status') if isinstance(shadow_run, dict) else ''
            if run_id and shadow_status == 'success':
                result['apply'] = await epg_match_shadow.apply_epg_match_shadow_run(run_id)
            else:
                result['apply'] = {
                    'run_id': run_id,
                    'skipped': True,
                    'reason': 'shadow_run_not_success',
                }
                result['status'] = 'failed'
                result['error'] = f'shadow run status: {shadow_status or "unknown"}'
                return result
            apply_result = result['apply'] or {}
            if apply_result.get('rollback') or apply_result.get('error'):
                result['status'] = 'failed'
                result['error'] = str(apply_result.get('error') or 'binding apply failed')[:512]
        except asyncio.CancelledError:
            raise
        except Exception as error:
            result['status'] = 'failed'
            result['error'] = _safe_error(error)
            logger.warning(
                'epg_binding_maintenance_failed',
                extra={
                    'epg_binding_maintenance': {
                        'trigger': trigger,
                        'error_type': type(error).__name__,
                    }
                },
            )
        finally:
            result['finished_at'] = _now()
    return result
