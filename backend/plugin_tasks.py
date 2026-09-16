from __future__ import annotations

from automation import AutomationHandlerResult, AutomationTaskContext, AutomationTaskDefinition
import database as db
import market


PLUGIN_UPDATE_TASK_ID = "market_plugin_auto_update"


def create_plugin_update_task_definition(plugin_subsystem) -> AutomationTaskDefinition:
    async def handler(context: AutomationTaskContext) -> AutomationHandlerResult:
        return await run_plugin_update_task(context, plugin_subsystem)

    return AutomationTaskDefinition(
        task_id=PLUGIN_UPDATE_TASK_ID,
        display_name="Market Plugin 自动更新",
        conflict_group="market",
        default_enabled=True,
        default_interval_seconds=24 * 60 * 60,
        handler=handler,
        allow_manual_trigger=True,
        minimum_interval_seconds=5 * 60,
        maximum_interval_seconds=31 * 24 * 60 * 60,
        initial_delay_seconds=6 * 60,
        allowed_task_types=frozenset({"check", "auto_update", "update_all"}),
        scheduled_task_type="auto_update",
        allow_automatic_scheduling=True,
    )


def register_plugin_update_task(registry, plugin_subsystem) -> AutomationTaskDefinition:
    definition = create_plugin_update_task_definition(plugin_subsystem)
    registry.register(definition)
    return definition


async def reconcile_plugin_update_task(service, plugin_subsystem) -> None:
    installed = bool(await db.list_plugin_installations())
    registered = PLUGIN_UPDATE_TASK_ID in service.registry
    if installed and not registered:
        await service.add_definition(create_plugin_update_task_definition(plugin_subsystem))
    elif not installed and registered:
        await service.remove_definition(PLUGIN_UPDATE_TASK_ID, delete_config=True)


async def run_plugin_update_task(context: AutomationTaskContext, plugin_subsystem) -> AutomationHandlerResult:
    checked = updated = skipped = failed = 0
    errors: list[str] = []
    refresh = await market.refresh_market()
    packages = {str(item.get("id") or ""): item for item in market.market_packages_snapshot()}
    source_results = {
        str(item.get("source_key") or ""): item
        for item in refresh.get("source_results") or []
        if str(item.get("source_key") or "")
    }
    for installation in await db.list_plugin_installations():
        if context.stop_requested():
            return AutomationHandlerResult(
                status="cancelled", checked_count=checked, updated_count=updated,
                skipped_count=skipped, failed_count=failed, errors=tuple(errors),
            )
        package_id = str(installation.get("source_package_id") or "")
        source = source_results.get(str(installation.get("source_key") or ""))
        package = packages.get(package_id)
        accepted = {str(value or "") for value in (source or {}).get("package_ids", []) if str(value or "")}
        if not source or source.get("usable_for_update") is not True or package_id not in accepted or not package:
            skipped += 1
            continue
        checked += 1
        status = market._version_status(
            str(package.get("version") or ""), str(installation.get("active_version") or ""),
        )
        if context.task_type == "check" or status != "upgrade":
            skipped += 1
            continue
        identity = f"{installation['publisher_id']}/{installation['plugin_id']}"
        try:
            await plugin_subsystem.install(identity, [package])
        except Exception as exc:
            if getattr(exc, "code", "") == "PERMISSION_APPROVAL_REQUIRED":
                skipped += 1
                errors.append(f"Plugin {identity}: permission approval required")
            else:
                failed += 1
                errors.append(f"Plugin {identity}: update failed")
        else:
            updated += 1
        await context.report_progress(
            checked_count=checked, updated_count=updated,
            skipped_count=skipped, failed_count=failed,
            error=errors[-1] if errors else "",
        )
    status = "success" if failed == 0 else ("partial" if checked > failed or skipped or updated else "failed")
    return AutomationHandlerResult(
        status=status, checked_count=checked, updated_count=updated,
        skipped_count=skipped, failed_count=failed, errors=tuple(errors),
    )
