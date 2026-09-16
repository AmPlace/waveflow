"""Market 自动检查与更新的单轮业务编排。"""

from __future__ import annotations

from typing import Any, Callable

import database as db
import market
from automation import (
    AutomationEventWaiter,
    AutomationHandlerResult,
    AutomationRegistry,
    AutomationRepository,
    AutomationRunner,
    AutomationScheduler,
    AutomationService,
    AutomationTaskContext,
    AutomationTaskDefinition,
)


MARKET_TASK_ID = "market_auto_update"
MARKET_CONFLICT_GROUP = "market"
MARKET_TASK_TYPES = frozenset({"check", "auto_update", "update_all"})
MARKET_DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
MARKET_INITIAL_DELAY_SECONDS = 5 * 60
MARKET_MINIMUM_INTERVAL_SECONDS = 5 * 60
MARKET_MAXIMUM_INTERVAL_SECONDS = 31 * 24 * 60 * 60
SOURCE_FAILURE_STATUSES = frozenset({"stale", "failed", "revision_discarded"})


def create_market_task_definition() -> AutomationTaskDefinition:
    return AutomationTaskDefinition(
        task_id=MARKET_TASK_ID,
        display_name="Market 自动更新",
        conflict_group=MARKET_CONFLICT_GROUP,
        default_enabled=True,
        default_interval_seconds=MARKET_DEFAULT_INTERVAL_SECONDS,
        handler=run_market_task,
        allow_manual_trigger=True,
        minimum_interval_seconds=MARKET_MINIMUM_INTERVAL_SECONDS,
        maximum_interval_seconds=MARKET_MAXIMUM_INTERVAL_SECONDS,
        initial_delay_seconds=MARKET_INITIAL_DELAY_SECONDS,
        allowed_task_types=MARKET_TASK_TYPES,
        scheduled_task_type="auto_update",
        allow_automatic_scheduling=True,
    )


def register_market_task(registry: AutomationRegistry) -> AutomationTaskDefinition:
    definition = create_market_task_definition()
    registry.register(definition)
    return definition


def create_market_automation_service(
    *,
    repository: AutomationRepository | None = None,
    waiter_factory: Callable[[AutomationTaskDefinition], AutomationEventWaiter] | None = None,
    scheduler_factory: Callable[..., AutomationScheduler] | None = None,
) -> AutomationService:
    registry = AutomationRegistry()
    register_market_task(registry)
    repository = repository or AutomationRepository(db)
    runner = AutomationRunner(registry, repository)
    return AutomationService(
        registry=registry,
        repository=repository,
        runner=runner,
        waiter_factory=waiter_factory,
        scheduler_factory=scheduler_factory,
    )


def _package_source_key(package_id: str) -> str:
    if "::" in package_id:
        return package_id.split("::", 1)[0]
    return market.OFFICIAL_MARKET_SOURCE_KEY


def _source_error(source_result: dict) -> str:
    source_name = str(
        source_result.get("source_name")
        or source_result.get("source_key")
        or source_result.get("source_id")
        or "unknown"
    )
    status = str(source_result.get("status") or "failed")
    error = str(source_result.get("error") or "来源本轮不可用于更新")
    return f"Market 来源 {source_name} {status}: {error}"


def _package_error(package_id: str, message: Any) -> str:
    return f"Market 包 {package_id}: {message}"


def _result_status(*, failed_count: int, successful_source_count: int, checked_count: int, updated_count: int) -> str:
    if failed_count == 0:
        return "success"
    if successful_source_count == 0 and checked_count == 0 and updated_count == 0:
        return "failed"
    return "partial"


def _handler_result(
    *,
    status: str,
    checked_count: int,
    updated_count: int,
    skipped_count: int,
    failed_count: int,
    errors: list[str],
) -> AutomationHandlerResult:
    return AutomationHandlerResult(
        status=status,
        checked_count=checked_count,
        updated_count=updated_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        errors=tuple(errors),
    )


async def run_market_task(context: AutomationTaskContext) -> AutomationHandlerResult:
    checked_count = 0
    updated_count = 0
    skipped_count = 0
    failed_count = 0
    errors: list[str] = []

    def cancelled() -> AutomationHandlerResult:
        return _handler_result(
            status="cancelled",
            checked_count=checked_count,
            updated_count=updated_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            errors=errors,
        )

    async def report_progress() -> None:
        await context.report_progress(
            checked_count=checked_count,
            updated_count=updated_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            error=errors[-1] if errors else "",
        )

    if context.stop_requested():
        return cancelled()

    refresh_result = await market.refresh_market()
    package_snapshot = market.market_packages_snapshot()
    source_results = list(refresh_result.get("source_results") or [])
    source_by_key = {
        str(result.get("source_key") or ""): result
        for result in source_results
        if str(result.get("source_key") or "")
    }
    successful_source_count = 0
    for source_result in source_results:
        status = str(source_result.get("status") or "")
        if source_result.get("usable_for_update") is True and status == "success":
            successful_source_count += 1
        elif status in SOURCE_FAILURE_STATUSES:
            failed_count += 1
            errors.append(_source_error(source_result))

    packages_by_id = {
        str(package.get("id") or ""): package
        for package in package_snapshot
        if str(package.get("id") or "")
    }
    await report_progress()
    if context.stop_requested():
        return cancelled()

    installs = await db.list_market_installs()
    update_targets: list[dict] = []
    for install in installs:
        if context.stop_requested():
            return cancelled()

        package_id = str(install.get("package_id") or "").strip()
        if not package_id:
            failed_count += 1
            errors.append("Market 安装记录缺少 package_id")
            await report_progress()
            continue

        source_result = source_by_key.get(_package_source_key(package_id))
        if not source_result or source_result.get("usable_for_update") is not True:
            skipped_count += 1
            await report_progress()
            continue

        accepted_package_ids = {
            str(value or "")
            for value in (source_result.get("package_ids") or [])
            if str(value or "")
        }
        package = packages_by_id.get(package_id)
        if package_id not in accepted_package_ids:
            remote_version = ""
            version_status = "unknown"
        elif package is None:
            failed_count += 1
            errors.append(_package_error(package_id, "本轮来源结果与 package 快照不一致"))
            await report_progress()
            continue
        else:
            remote_version = str(package.get("version") or "").strip()
            version_status = market._version_status(
                remote_version,
                str(install.get("installed_version") or ""),
            )

        try:
            persisted = await db.update_market_install_check_state(
                package_id,
                remote_version=remote_version,
                version_status=version_status,
            )
        except Exception as exc:
            failed_count += 1
            errors.append(_package_error(package_id, f"检查状态保存失败: {exc}"))
            await report_progress()
            continue
        if not persisted:
            failed_count += 1
            errors.append(_package_error(package_id, "检查状态保存时安装记录不存在"))
            await report_progress()
            continue

        checked_count += 1
        if context.task_type == "check":
            await report_progress()
            continue
        if version_status != "upgrade":
            skipped_count += 1
            await report_progress()
            continue
        if context.task_type == "auto_update" and not bool(install.get("auto_update")):
            skipped_count += 1
            await report_progress()
            continue
        update_targets.append(install)
        await report_progress()

    if context.task_type == "check":
        await report_progress()
        return _handler_result(
            status=_result_status(
                failed_count=failed_count,
                successful_source_count=successful_source_count,
                checked_count=checked_count,
                updated_count=updated_count,
            ),
            checked_count=checked_count,
            updated_count=updated_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            errors=errors,
        )

    for install in update_targets:
        if context.stop_requested():
            return cancelled()

        package_id = str(install.get("package_id") or "").strip()
        try:
            started = await db.mark_market_install_update_started(
                package_id,
                run_token=context.run_token,
            )
        except Exception as exc:
            started = False
            start_error = f"更新开始状态保存失败: {exc}"
        else:
            start_error = "更新开始时安装记录不存在"
        if not started:
            failed_count += 1
            errors.append(_package_error(package_id, start_error))
            await report_progress()
            continue

        try:
            await market.update_installed_package(package_id)
        except Exception as exc:
            error = str(exc)
            try:
                completed = await db.complete_market_install_update(
                    package_id,
                    run_token=context.run_token,
                    status="failed",
                    error=error,
                )
            except Exception as state_exc:
                completed = False
                completion_error = f"更新失败，且状态完成保存失败: {state_exc}"
            else:
                completion_error = f"{error}; 更新失败状态所有权已变化"
            failed_count += 1
            if completed:
                errors.append(_package_error(package_id, error))
            else:
                errors.append(_package_error(package_id, completion_error))
            await report_progress()
            continue

        updated_count += 1
        try:
            completed = await db.complete_market_install_update(
                package_id,
                run_token=context.run_token,
                status="success",
            )
        except Exception as exc:
            completed = False
            completion_error = f"更新状态完成保存失败: {exc}"
        else:
            completion_error = "更新成功，但状态完成所有权已变化"
        if not completed:
            failed_count += 1
            errors.append(_package_error(package_id, completion_error))
        await report_progress()

    await report_progress()
    return _handler_result(
        status=_result_status(
            failed_count=failed_count,
            successful_source_count=successful_source_count,
            checked_count=checked_count,
            updated_count=updated_count,
        ),
        checked_count=checked_count,
        updated_count=updated_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        errors=errors,
    )
