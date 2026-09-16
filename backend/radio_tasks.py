"""Radio catalog/programme refresh tasks owned by the shared AutomationService."""

from __future__ import annotations

import asyncio
import logging

from automation import AutomationHandlerResult, AutomationService, AutomationTaskContext, AutomationTaskDefinition


logger = logging.getLogger(__name__)

RADIO_TASK_PREFIX = "radio_refresh:"
RADIO_CATALOG_TASK_TYPE = "catalog"
RADIO_PROGRAMME_TASK_TYPE = "programme"
RADIO_CATALOG_INTERVAL_SECONDS = 2 * 60 * 60
RADIO_PROGRAMME_INTERVAL_SECONDS = 10 * 60
RADIO_MINIMUM_INTERVAL_SECONDS = 60
RADIO_MAXIMUM_INTERVAL_SECONDS = 31 * 24 * 60 * 60


def _task_id(identity: str, kind: str) -> str:
    return f"{RADIO_TASK_PREFIX}{kind}:{identity}"


def _identity_from_task_id(task_id: str) -> str | None:
    prefix = f"{RADIO_TASK_PREFIX}"
    if not isinstance(task_id, str) or not task_id.startswith(prefix):
        return None
    value = task_id[len(prefix):]
    if ":" not in value:
        return None
    _kind, identity = value.split(":", 1)
    return identity or None


def _result_error(result: dict) -> tuple[str, tuple[str, ...]]:
    raw_error = result.get("error")
    if isinstance(raw_error, dict):
        code = str(raw_error.get("code") or "").strip()
        category = str(raw_error.get("category") or "").strip()
        message = str(raw_error.get("message") or "").strip()
        error = ": ".join(value for value in (code, category, message) if value)
    else:
        error = str(raw_error or "").strip()
    raw_errors = result.get("errors") or ()
    errors = tuple(str(item).strip() for item in raw_errors if str(item).strip())
    return error, errors


def create_radio_task_definition(identity: str, kind: str, subsystem) -> AutomationTaskDefinition:
    if kind not in {RADIO_CATALOG_TASK_TYPE, RADIO_PROGRAMME_TASK_TYPE}:
        raise ValueError("unsupported Radio task kind")
    task_id = _task_id(identity, kind)
    interval = (
        RADIO_CATALOG_INTERVAL_SECONDS
        if kind == RADIO_CATALOG_TASK_TYPE
        else RADIO_PROGRAMME_INTERVAL_SECONDS
    )

    async def handler(context: AutomationTaskContext) -> AutomationHandlerResult:
        if context.stop_requested():
            return AutomationHandlerResult(status="cancelled")
        if kind == RADIO_CATALOG_TASK_TYPE:
            result = await subsystem.refresh_radio_catalog(identity)
        else:
            result = await subsystem.refresh_radio_programmes(identity)
        status = str(result.get("status") or "failed")
        error, errors = _result_error(result)
        failed_count = int(result.get("failed") or 0)
        if status == "failed" and failed_count == 0:
            failed_count = 1
        return AutomationHandlerResult(
            status=status if status in {"success", "partial", "failed", "cancelled"} else "failed",
            checked_count=int(result.get("checked") or result.get("published") or 0),
            updated_count=int(result.get("updated") or result.get("published") or 0),
            failed_count=failed_count,
            error=error,
            errors=errors[:8],
        )

    return AutomationTaskDefinition(
        task_id=task_id,
        display_name=f"Radio {kind} refresh ({identity})",
        conflict_group=f"radio:{identity}",
        default_enabled=True,
        default_interval_seconds=interval,
        handler=handler,
        allow_manual_trigger=True,
        minimum_interval_seconds=RADIO_MINIMUM_INTERVAL_SECONDS,
        maximum_interval_seconds=RADIO_MAXIMUM_INTERVAL_SECONDS,
        initial_delay_seconds=0,
        allowed_task_types=frozenset({kind}),
        scheduled_task_type=kind,
        allow_automatic_scheduling=True,
    )


def _active_radio_features(service: AutomationService) -> dict[str, frozenset[str]]:
    active = getattr(getattr(service, "_active", None), "items", None)
    if not callable(active):
        return {}
    result: dict[str, frozenset[str]] = {}
    for identity, instance in active():
        state = getattr(instance, "state", "")
        if getattr(state, "value", state) != "HEALTHY_ACTIVE" or getattr(instance, "health", "") != "healthy":
            continue
        manifest = getattr(instance, "manifest", None)
        contracts = getattr(manifest, "provider_contracts", ())
        radio_contract = next(
            (contract for contract in contracts if getattr(contract, "contract", "") == "radio_provider"),
            None,
        )
        owned_schemes = getattr(manifest, "owned_schemes", ())
        def owned_contract(item) -> str:
            if hasattr(item, "contract"):
                return str(getattr(item, "contract") or "")
            if isinstance(item, (tuple, list)) and len(item) > 1:
                return str(item[1] or "")
            if isinstance(item, dict):
                return str(item.get("contract") or "")
            return ""

        if radio_contract is not None and any(
            owned_contract(item) == "radio_provider"
            for item in owned_schemes
        ):
            result[str(identity)] = frozenset(getattr(radio_contract, "features", ()))
    return result


async def reconcile_radio_automation_tasks(
    service: AutomationService, subsystem, *, retained_owner_identities: set[str] | None = None,
) -> dict:
    """Project current healthy Radio Plugin identities into Automation."""
    # Independent AutomationService lifetimes must not share an event-loop lock.
    lock = getattr(service, "_radio_reconciliation_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        service._radio_reconciliation_lock = lock

    async def reconcile() -> dict:
        async with lock:
            expected: dict[str, set[str]] = {}
            active_service = getattr(subsystem, "service", None)
            if active_service is not None:
                for identity, features in _active_radio_features(active_service).items():
                    kinds: set[str] = set()
                    if RADIO_CATALOG_TASK_TYPE in features:
                        kinds.add(RADIO_CATALOG_TASK_TYPE)
                    if RADIO_PROGRAMME_TASK_TYPE in features:
                        kinds.add(RADIO_PROGRAMME_TASK_TYPE)
                    if kinds:
                        expected[identity] = kinds
            expected_task_ids = {
                _task_id(identity, kind)
                for identity, kinds in expected.items()
                for kind in kinds
            }
            registered = {
                definition.task_id
                for definition in service.registry.list_definitions()
                if definition.task_id.startswith(RADIO_TASK_PREFIX)
            }
            configs = await service.repository.list_configs()
            persisted = {
                config.task_id
                for config in configs
                if config.task_id.startswith(RADIO_TASK_PREFIX)
            }
            removed = 0
            for task_id in sorted((registered | persisted) - expected_task_ids):
                retain_config = (
                    retained_owner_identities is None
                    or _identity_from_task_id(task_id) in retained_owner_identities
                )
                if task_id in registered:
                    await service.remove_definition(task_id, delete_config=not retain_config)
                elif task_id in persisted and not retain_config:
                    await service.repository.delete_config(task_id)
                removed += 1

            created = 0
            for identity, kinds in sorted(expected.items()):
                for kind in sorted(kinds):
                    task_id = _task_id(identity, kind)
                    if task_id not in registered:
                        await service.add_definition(create_radio_task_definition(identity, kind, subsystem))
                        created += 1
                    else:
                        await service.repository.ensure_config(service.registry.get(task_id))
            return {
                "expected_task_count": len(expected_task_ids),
                "created_count": created,
                "removed_count": removed,
                "task_ids": sorted(expected_task_ids),
            }

    task = asyncio.ensure_future(reconcile())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except BaseException:
            logger.exception("Radio automation reconciliation failed during cancellation drain")
        raise


async def run_radio_task_now(service: AutomationService, subsystem, identity: str, kind: str):
    """Run one explicit Radio refresh through Automation's runner."""
    from automation import AutomationRunRequest

    await reconcile_radio_automation_tasks(service, subsystem)
    return await service.runner.run(AutomationRunRequest(
        task_id=_task_id(identity, kind), task_type=kind, trigger="manual_api",
    ))
