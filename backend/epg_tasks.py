"""EPG source tasks hosted by the shared AutomationService."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

import database as db
import epg
from automation import (
    AutomationBusy,
    AutomationEventWaiter,
    AutomationHandlerResult,
    AutomationRegistry,
    AutomationRepository,
    AutomationRunRequest,
    AutomationRunResult,
    AutomationRunner,
    AutomationScheduler,
    AutomationService,
    AutomationTaskContext,
    AutomationTaskDefinition,
)
from market_tasks import register_market_task


logger = logging.getLogger(__name__)

EPG_TASK_PREFIX = "epg_refresh_source:"
EPG_CONFLICT_PREFIX = "epg_source:"
EPG_TASK_TYPE = "refresh"
EPG_DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60
EPG_MINIMUM_INTERVAL_SECONDS = 5 * 60
EPG_MAXIMUM_INTERVAL_SECONDS = 31 * 24 * 60 * 60
EPG_INITIAL_DELAY_SECONDS = 0

# Source CRUD and task projection share this short lifecycle boundary.  The
# lock never covers network I/O; it only protects the durable-source read and
# the corresponding Automation projection mutation.
EPG_RECONCILIATION_LOCK = asyncio.Lock()


def epg_task_id(source_id: int) -> str:
    source_id = _normalize_source_id(source_id)
    return f"{EPG_TASK_PREFIX}{source_id}"


def epg_conflict_group(source_id: int) -> str:
    source_id = _normalize_source_id(source_id)
    return f"{EPG_CONFLICT_PREFIX}{source_id}"


def source_id_from_task_id(task_id: str) -> int | None:
    if not isinstance(task_id, str) or not task_id.startswith(EPG_TASK_PREFIX):
        return None
    suffix = task_id[len(EPG_TASK_PREFIX):]
    try:
        return _normalize_source_id(int(suffix))
    except (TypeError, ValueError):
        return None


def _normalize_source_id(source_id: int) -> int:
    if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0:
        raise ValueError("epg_source_id 必须是正整数")
    return source_id


def _source_failure_error(source_id: int, source_result: dict) -> str:
    status = str(source_result.get("status") or "failed")
    detail = epg.sanitize_epg_error(source_result.get("error") or "来源本轮刷新失败")
    return f"EPG source {source_id} {status}: {detail}"


async def run_epg_refresh_task(
    context: AutomationTaskContext,
    *,
    source_id: int,
    client: httpx.AsyncClient,
) -> AutomationHandlerResult:
    """Translate one existing source refresh pipeline into Automation state."""
    source_id = _normalize_source_id(source_id)
    if context.stop_requested():
        return AutomationHandlerResult(status="cancelled")

    source = await db.get_epg_source(source_id)
    if source is None:
        return AutomationHandlerResult(status="success", skipped_count=1)

    source_result = await epg.refresh_epg_source(
        source,
        client,
        stop_event=context.stop_event,
    )
    status = str(source_result.get("status") or "failed")
    await context.report_progress(
        checked_count=1,
        updated_count=1 if status == "success" else 0,
        skipped_count=1 if status in {"disabled", "revision_discarded"} else 0,
        failed_count=1 if status in {"stale", "failed"} else 0,
        error=(
            _source_failure_error(source_id, source_result)
            if status in {"stale", "failed"}
            else ""
        ),
    )

    if status == "success":
        try:
            maintenance = await epg.run_epg_refresh_maintenance(trigger="epg_automation")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return AutomationHandlerResult(
                status="partial",
                checked_count=1,
                updated_count=1,
                failed_count=1,
                error=f"EPG binding maintenance failed: {type(error).__name__}",
            )
        if maintenance.get("status") != "success":
            error = epg.sanitize_epg_error(
                maintenance.get("error") or "EPG binding maintenance degraded"
            )
            return AutomationHandlerResult(
                status="partial",
                checked_count=1,
                updated_count=1,
                failed_count=1,
                error=error,
            )
        return AutomationHandlerResult(
            status="success",
            checked_count=1,
            updated_count=1,
        )
    if status in {"disabled", "revision_discarded"}:
        return AutomationHandlerResult(
            status="success",
            checked_count=1,
            skipped_count=1,
        )
    if status in {"stale", "failed"}:
        return AutomationHandlerResult(
            status="failed",
            checked_count=1,
            failed_count=1,
            error=_source_failure_error(source_id, source_result),
        )
    return AutomationHandlerResult(
        status="failed",
        checked_count=1,
        failed_count=1,
        error=f"EPG source {source_id} returned unsupported status",
    )


def create_epg_task_definition(
    source_id: int,
    client: httpx.AsyncClient,
    *,
    default_enabled: bool = True,
) -> AutomationTaskDefinition:
    source_id = _normalize_source_id(source_id)
    if not isinstance(default_enabled, bool):
        raise TypeError("default_enabled 必须是布尔值")

    async def handler(context: AutomationTaskContext) -> AutomationHandlerResult:
        return await run_epg_refresh_task(
            context,
            source_id=source_id,
            client=client,
        )

    return AutomationTaskDefinition(
        task_id=epg_task_id(source_id),
        display_name=f"EPG source {source_id} refresh",
        conflict_group=epg_conflict_group(source_id),
        default_enabled=default_enabled,
        default_interval_seconds=EPG_DEFAULT_INTERVAL_SECONDS,
        handler=handler,
        allow_manual_trigger=True,
        minimum_interval_seconds=EPG_MINIMUM_INTERVAL_SECONDS,
        maximum_interval_seconds=EPG_MAXIMUM_INTERVAL_SECONDS,
        initial_delay_seconds=EPG_INITIAL_DELAY_SECONDS,
        allowed_task_types=frozenset({EPG_TASK_TYPE}),
        scheduled_task_type=EPG_TASK_TYPE,
        allow_automatic_scheduling=True,
    )


async def reconcile_epg_automation_tasks(
    service: AutomationService,
    client: httpx.AsyncClient,
    *,
    ensure_defaults: bool = False,
) -> dict:
    """Make source tasks match durable sources without stale resurrection."""

    async def _reconcile():
        if ensure_defaults:
            await epg.ensure_default_epg_sources()
        async with EPG_RECONCILIATION_LOCK:
            return await _reconcile_epg_automation_tasks_locked(service, client)

    reconciliation_task = asyncio.ensure_future(_reconcile())
    try:
        return await asyncio.shield(reconciliation_task)
    except asyncio.CancelledError:
        # A source mutation may already have committed before its caller was
        # cancelled.  Drain projection reconciliation before propagating the
        # cancellation so the task/config state cannot be stranded halfway.
        while not reconciliation_task.done():
            try:
                await asyncio.shield(reconciliation_task)
            except asyncio.CancelledError:
                continue
        try:
            reconciliation_task.result()
        except BaseException as error:
            logger.exception(
                "EPG reconciliation failed while draining cancellation: %s",
                type(error).__name__,
            )
        raise


async def _reconcile_epg_automation_tasks_locked(
    service: AutomationService,
    client: httpx.AsyncClient,
) -> dict:
    """Reconcile under ``EPG_RECONCILIATION_LOCK``.

    The durable source table is reloaded immediately inside the shared
    lifecycle boundary.  A caller must not pass a pre-lock snapshot here.
    """
    sources = await db.get_epg_sources()
    source_by_id = {int(source["id"]): source for source in sources}
    expected_task_ids = {epg_task_id(source_id) for source_id in source_by_id}
    configs = await service.repository.list_configs()
    persisted_epg_task_ids = {
        config.task_id
        for config in configs
        if config.task_id.startswith(EPG_TASK_PREFIX)
    }
    registered_task_ids = {
        definition.task_id
        for definition in service.registry.list_definitions()
        if definition.task_id.startswith(EPG_TASK_PREFIX)
    }

    created_count = 0
    updated_count = 0
    removed_count = 0

    for task_id in sorted((persisted_epg_task_ids | registered_task_ids) - expected_task_ids):
        if task_id in registered_task_ids:
            await service.remove_definition(task_id, delete_config=True)
        else:
            await service.repository.delete_config(task_id)
        removed_count += 1

    for source_id, source in sorted(source_by_id.items()):
        task_id = epg_task_id(source_id)
        # Revalidate immediately before projection mutation.  A changed
        # revision/config is reconciled from the current durable row; a
        # deleted row is removed rather than recreated from the old snapshot.
        current_source = await db.get_epg_source(source_id)
        if current_source is None:
            if task_id in registered_task_ids:
                await service.remove_definition(task_id, delete_config=True)
                registered_task_ids.discard(task_id)
            elif task_id in persisted_epg_task_ids:
                await service.repository.delete_config(task_id)
                persisted_epg_task_ids.discard(task_id)
            removed_count += 1
            continue
        source = current_source
        if task_id not in registered_task_ids:
            definition = create_epg_task_definition(
                source_id,
                client,
                default_enabled=bool(source.get("enabled")),
            )
            config = await service.add_definition(definition)
            registered_task_ids.add(task_id)
            created_count += 1
        else:
            definition = service.registry.get(task_id)
            config = await service.repository.ensure_config(definition)

        should_enable = bool(source.get("enabled"))
        if config.enabled != should_enable:
            config = await service.repository.update_config(
                task_id,
                enabled=should_enable,
            )
            if config is None:
                raise RuntimeError(f"EPG automation config disappeared: {task_id}")
            updated_count += 1
            if service.is_started:
                service.notify_config_changed(task_id)

    return {
        "source_count": len(source_by_id),
        "enabled_source_count": sum(bool(source.get("enabled")) for source in sources),
        "expected_task_count": len(expected_task_ids),
        "created_count": created_count,
        "updated_count": updated_count,
        "removed_count": removed_count,
        "task_ids": sorted(expected_task_ids),
    }


async def create_production_automation_service(
    client: httpx.AsyncClient,
    *,
    repository: AutomationRepository | None = None,
    waiter_factory: Callable[[AutomationTaskDefinition], AutomationEventWaiter] | None = None,
    scheduler_factory: Callable[..., AutomationScheduler] | None = None,
) -> AutomationService:
    """Assemble the one production service for Market and EPG tasks."""
    registry = AutomationRegistry()
    register_market_task(registry)
    repository = repository or AutomationRepository(db)
    runner = AutomationRunner(registry, repository)
    service = AutomationService(
        registry=registry,
        repository=repository,
        runner=runner,
        waiter_factory=waiter_factory,
        scheduler_factory=scheduler_factory,
    )
    await reconcile_epg_automation_tasks(
        service,
        client,
        ensure_defaults=True,
    )
    return service


async def run_epg_refresh_now(
    service: AutomationService,
    client: httpx.AsyncClient,
) -> tuple[AutomationRunResult | AutomationBusy, ...]:
    """Run all currently enabled source tasks now; caller may fire-and-forget."""
    await reconcile_epg_automation_tasks(service, client, ensure_defaults=True)
    sources = await db.get_epg_sources()
    results: list[AutomationRunResult | AutomationBusy] = []
    for source in sorted(sources, key=lambda item: int(item["id"])):
        if not source.get("enabled"):
            continue
        results.append(await service.runner.run(AutomationRunRequest(
            task_id=epg_task_id(int(source["id"])),
            task_type=EPG_TASK_TYPE,
            trigger="manual_api",
        )))
    return tuple(results)


async def run_epg_source_refresh_now(
    service: AutomationService,
    client: httpx.AsyncClient,
    *,
    source_id: int,
) -> AutomationRunResult | AutomationBusy:
    """Run one source through the existing Automation-owned refresh handler."""
    source_id = _normalize_source_id(source_id)
    await reconcile_epg_automation_tasks(service, client)
    return await service.runner.run(AutomationRunRequest(
        task_id=epg_task_id(source_id),
        task_type=EPG_TASK_TYPE,
        trigger="manual_api",
    ))


async def list_epg_automation_status(
    service: AutomationService,
    *,
    sources: list[dict] | None = None,
) -> list[dict]:
    """Return bounded internal status without source URL or request metadata."""
    source_rows = sources if sources is not None else await db.get_epg_sources()
    sources_by_id = {int(source["id"]): source for source in source_rows}
    configs = {
        config.task_id: config
        for config in await service.repository.list_configs()
    }
    states = {
        state.task_id: state
        for state in await service.repository.list_states()
    }
    rows: list[dict] = []
    for definition in sorted(
        service.registry.list_definitions(),
        key=lambda item: item.task_id,
    ):
        source_id = source_id_from_task_id(definition.task_id)
        if source_id is None:
            continue
        config = configs.get(definition.task_id)
        state = states.get(definition.task_id)
        source = sources_by_id.get(source_id)
        next_run_at = ""
        if config and config.enabled and state and state.last_finished_at:
            try:
                finished = datetime.fromisoformat(state.last_finished_at)
                next_run_at = (finished + timedelta(seconds=config.interval_seconds)).astimezone(
                    timezone.utc
                ).isoformat()
            except (TypeError, ValueError):
                next_run_at = ""
        rows.append({
            "source_id": source_id,
            "task_id": definition.task_id,
            "enabled": bool(config and config.enabled),
            "interval_seconds": config.interval_seconds if config else 0,
            "last_started_at": state.last_started_at if state else "",
            "last_finished_at": state.last_finished_at if state else "",
            "last_status": state.status if state else "never_run",
            "last_error": epg.sanitize_epg_error(state.last_error) if state and state.last_error else "",
            "next_run_at": next_run_at,
            "source_last_success_at": str(source.get("last_success_at") or "") if source else "",
        })
    return rows


async def reconcile_epg_tasks_after_source_change(
    service: AutomationService | None,
    client: httpx.AsyncClient,
    *,
    source_id: int,
    operation: str,
) -> bool:
    """Keep a committed CRUD operation successful if scheduling reconciliation degrades."""
    if service is None:
        logger.warning(
            "epg_automation_reconcile_deferred",
            extra={"epg_automation": {"source_id": source_id, "operation": operation, "error_type": "service_unavailable"}},
        )
        return False
    try:
        await reconcile_epg_automation_tasks(service, client)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning(
            "epg_automation_reconcile_deferred",
            extra={
                "epg_automation": {
                    "source_id": source_id,
                    "operation": operation,
                    "error_type": type(error).__name__,
                }
            },
        )
        return False
