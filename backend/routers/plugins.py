from __future__ import annotations

import base64
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import database as db
import market
from official_plugin_distribution import OFFICIAL_PUBLISHER_ID, load_official_trust_rows
from plugin_tasks import reconcile_plugin_update_task
from radio_tasks import reconcile_radio_automation_tasks, run_radio_task_now
from plugin_runtime import LifecycleState, PluginError, validate_manifest
from security.dependencies import require_admin


router = APIRouter(prefix="/api/admin/plugins", tags=["plugins"], dependencies=[Depends(require_admin)])
IDENTIFIER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")


class PluginActionRequest(BaseModel):
    package_id: str = ""


class PublisherTrustRequest(BaseModel):
    publisher_id: str
    key_id: str
    public_key: str
    trust_level: str = "third_party"
    enabled: bool = True
    description: str = Field(default="", max_length=500)


class OwnershipRequest(BaseModel):
    mode: str
    plugin: str = ""


class PermissionActionRequest(BaseModel):
    permission: str
    package_id: str = ""


class DeveloperModeRequest(BaseModel):
    enabled: bool


class DeveloperInstallRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class DeveloperPermissionRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    permission: str = Field(min_length=1, max_length=128)


def _subsystem(request: Request):
    subsystem = getattr(request.app.state, "plugin_subsystem", None)
    if subsystem is None:
        raise HTTPException(status_code=503, detail={"code": "PLUGIN_UNAVAILABLE", "message": "Plugin subsystem is unavailable"})
    return subsystem


def _error(exc: PluginError) -> HTTPException:
    statuses = {
        "RESOURCE_NOT_FOUND": 404, "ARTIFACT_NOT_FOUND": 404,
        "PLUGIN_UNTRUSTED": 403, "ARTIFACT_SIGNATURE_INVALID": 403, "CAPABILITY_DENIED": 403,
        "SCHEME_CONFLICT": 409, "PLUGIN_CANDIDATE_CONFLICT": 409,
        "PLUGIN_UNAVAILABLE": 503,
        "PLUGIN_INCOMPATIBLE": 422, "PLATFORM_UNSUPPORTED": 422,
        "PYTHON_RUNTIME_UNSUPPORTED": 422, "DEPENDENCY_LOCK_INVALID": 422,
        "DEPENDENCY_PLATFORM_UNSUPPORTED": 422,
        "DEPENDENCY_ARTIFACT_NOT_FOUND": 404,
        "PERMISSION_APPROVAL_REQUIRED": 409,
        "DEVELOPER_MODE_REQUIRED": 403,
    }
    return HTTPException(status_code=statuses.get(exc.code, 400), detail=exc.as_contract())


def _manifest_projection(
    row: dict[str, Any], *, runtime=None, runtime_available: bool | None = None,
) -> dict[str, Any]:
    try:
        manifest = json.loads(row.get("manifest_json") or "{}")
    except json.JSONDecodeError:
        manifest = {}
    manifest_runtime = manifest.get("runtime") or {}
    lock = manifest_runtime.get("dependency_lock") or {}
    dependencies = lock.get("artifacts") if isinstance(lock.get("artifacts"), list) else []
    if manifest_runtime.get("type") == "python":
        try:
            from plugin_python_runtime import select_dependency_artifacts
            dependencies = list(select_dependency_artifacts(lock))
        except PluginError:
            dependencies = []
    persisted_runtime_available = bool(
        row.get("enabled") and row.get("lifecycle_state") == "active" and not row.get("quarantined")
    )
    effective_runtime_available = persisted_runtime_available
    if runtime_available is not None:
        effective_runtime_available = effective_runtime_available and bool(runtime_available)
    if runtime is not None:
        identity = f"{row['publisher_id']}/{row['plugin_id']}"
        for item in manifest.get("owned_schemes", []):
            scheme = str(item.get("scheme") or "").strip().lower() if isinstance(item, dict) else ""
            if not scheme:
                effective_runtime_available = False
                break
            try:
                instance = runtime.registry.route(scheme)
            except PluginError:
                effective_runtime_available = False
                break
            if (instance.manifest.identity != identity
                    or instance.state != LifecycleState.HEALTHY_ACTIVE
                    or instance.health != "healthy"):
                effective_runtime_available = False
                break
    return {
        "plugin": f"{row['publisher_id']}/{row['plugin_id']}",
        "display_name": manifest.get("display_name") or row["plugin_id"],
        "version": row.get("active_version") or row.get("installed_version") or "",
        "enabled": bool(row.get("enabled")),
        "lifecycle_state": row.get("lifecycle_state") or "",
        "runtime_available": effective_runtime_available,
        "runtime_health": "healthy" if effective_runtime_available else "unavailable",
        "quarantined": bool(row.get("quarantined")),
        "owned_schemes": [item.get("scheme") for item in manifest.get("owned_schemes", []) if isinstance(item, dict)],
        "provider_contracts": manifest.get("provider_contracts") or [],
        "source_provenance": {"source_key": row.get("source_key") or "", "package_id": row.get("source_package_id") or ""},
        "trust_state": row.get("trust_state") or "",
        "trust_class": row.get("trust_class") or "official",
        "last_error": str(row.get("last_error") or "")[:1024],
        "runtime": {
            "type": manifest_runtime.get("type") or row.get("runtime_type") or "unknown",
            "python_version_range": manifest_runtime.get("python_version_range") or "",
            "environment_status": "ready" if manifest_runtime.get("type") == "python" and effective_runtime_available
                                  else "not_applicable" if manifest_runtime.get("type") != "python" else "unavailable",
            "dependency_count": len(dependencies),
            "dependencies": [{"name": item.get("name"), "version": item.get("version")}
                             for item in dependencies if isinstance(item, dict)],
        },
    }


async def _plugin_projection(
    row: dict[str, Any], *, include_permissions: bool = True,
    runtime=None, runtime_available: bool | None = None,
) -> dict[str, Any]:
    projection = _manifest_projection(
        row, runtime=runtime, runtime_available=runtime_available,
    )
    ownership_rows = await db.list_plugin_scheme_ownership()
    by_scheme = {str(item.get("scheme") or ""): item for item in ownership_rows}
    projection["ownership"] = [
        {
            "scheme": scheme,
            "mode": str(by_scheme.get(scheme, {}).get("mode") or "legacy"),
            "plugin": str(by_scheme.get(scheme, {}).get("plugin_identity") or ""),
        }
        for scheme in projection["owned_schemes"]
    ]
    package = next(
        (item for item in market.market_packages_snapshot()
         if item.get("id") == row.get("source_package_id")),
        None,
    )
    projection["market"] = {
        "package_id": str(row.get("source_package_id") or ""),
        "source_key": str(row.get("source_key") or ""),
        "available_version": str((package or {}).get("version") or ""),
        "update_available": bool(package) and market._version_status(
            str(package.get("version") or ""), projection["version"]
        ) == "upgrade",
    }
    if include_permissions:
        from plugin_permissions import permission_projection
        from plugin_runtime import validate_manifest
        try:
            projection["permissions"] = await permission_projection(validate_manifest(json.loads(row["manifest_json"])))
        except PluginError:
            projection["permissions"] = {"requested": [], "approved": [], "pending": [], "risk": {}}
    return projection


async def _packages(package_id: str = "") -> list[dict[str, Any]]:
    await market.ensure_market_loaded()
    packages = market.market_packages_snapshot()
    if package_id:
        packages = [item for item in packages if item.get("id") == package_id]
    return packages


@router.get("")
async def list_plugins(request: Request) -> dict[str, Any]:
    rows = await db.list_plugin_installations()
    subsystem = getattr(request.app.state, "plugin_subsystem", None)
    runtime = getattr(getattr(subsystem, "service", None), "runtime", None)
    available = None if subsystem is not None else False
    return {
        "plugins": [await _plugin_projection(row, runtime=runtime, runtime_available=available) for row in rows]
    }


@router.get("/trust")
async def list_trust() -> dict[str, Any]:
    rows = [row for row in await db.list_plugin_publisher_trust()
            if row.get("publisher_id") != OFFICIAL_PUBLISHER_ID]
    official = load_official_trust_rows()
    return {"publishers": [
        {
            **{k: row[k] for k in ("publisher_id", "key_id", "trust_level", "enabled", "description")},
            "builtin": row in official,
        }
        for row in [*official, *rows]
    ]}


@router.get("/developer/mode")
async def get_developer_mode(request: Request) -> dict[str, Any]:
    return {"enabled": await _subsystem(request).developer_mode_enabled()}


@router.put("/developer/mode")
async def set_developer_mode(body: DeveloperModeRequest, request: Request) -> dict[str, Any]:
    return await _subsystem(request).set_developer_mode(body.enabled)


@router.post("/developer/local/install")
async def install_developer_plugin(body: DeveloperInstallRequest, request: Request) -> dict[str, Any]:
    subsystem = _subsystem(request)
    try:
        result = await subsystem.install_developer_local(body.path)
        automation = getattr(request.app.state, "automation_service", None)
        if automation is not None:
            await reconcile_radio_automation_tasks(automation, subsystem)
            identity = str(result.get("plugin") or "")
            if identity and "/" in identity:
                publisher, plugin_id = identity.split("/", 1)
                row = await db.get_plugin_installation(publisher, plugin_id)
                manifest = validate_manifest(json.loads(row["manifest_json"])) if row else None
                radio_features = {
                    feature
                    for contract in (manifest.provider_contracts if manifest else ())
                    if contract.contract == "radio_provider"
                    for feature in contract.features
                }
                refresh = {}
                if "catalog" in radio_features:
                    refresh["catalog"] = await run_radio_task_now(
                        automation, subsystem, identity, "catalog",
                    )
                result = {**result, "radio_refresh": refresh}
        return result
    except PluginError as exc:
        raise _error(exc) from exc


@router.post("/developer/local/permission")
async def approve_developer_plugin_permission(
    body: DeveloperPermissionRequest, request: Request,
) -> dict[str, Any]:
    try:
        return await _subsystem(request).approve_developer_local_permission(
            body.path, body.permission, "developer_local_api",
        )
    except PluginError as exc:
        raise _error(exc) from exc


@router.put("/trust")
async def put_trust(body: PublisherTrustRequest, request: Request) -> dict[str, Any]:
    if not IDENTIFIER_RE.fullmatch(body.publisher_id) or not IDENTIFIER_RE.fullmatch(body.key_id):
        raise HTTPException(status_code=422, detail={"code": "INVALID_PLUGIN_RESPONSE", "message": "Invalid publisher identity"})
    if body.trust_level not in {"official", "third_party"}:
        raise HTTPException(status_code=422, detail={"code": "INVALID_PLUGIN_RESPONSE", "message": "Invalid trust level"})
    if body.publisher_id == OFFICIAL_PUBLISHER_ID or body.trust_level == "official":
        raise HTTPException(status_code=409, detail={
            "code": "PLUGIN_UNTRUSTED",
            "message": "Official publisher trust is managed by the WaveFlow distribution",
        })
    try:
        decoded = base64.b64decode(body.public_key, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_PLUGIN_RESPONSE", "message": "Invalid Ed25519 public key"}) from exc
    if len(decoded) != 32:
        raise HTTPException(status_code=422, detail={"code": "INVALID_PLUGIN_RESPONSE", "message": "Invalid Ed25519 public key"})
    row = await db.upsert_plugin_publisher_trust(**body.model_dump())
    await _subsystem(request).reload_trust()
    return {k: row[k] for k in ("publisher_id", "key_id", "trust_level", "enabled", "description")}


@router.get("/dependencies/{package_id:path}")
async def content_dependencies(package_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _subsystem(request).service.installed_content_dependency_projection(package_id)
    except PluginError as exc:
        raise _error(exc) from exc


@router.get("/{publisher_id}/{plugin_id}")
async def plugin_detail(publisher_id: str, plugin_id: str, request: Request) -> dict[str, Any]:
    row = await db.get_plugin_installation(publisher_id, plugin_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": "Plugin is not installed"})
    subsystem = getattr(request.app.state, "plugin_subsystem", None)
    runtime = getattr(getattr(subsystem, "service", None), "runtime", None)
    available = None if subsystem is not None else False
    return await _plugin_projection(row, runtime=runtime, runtime_available=available)


@router.get("/{publisher_id}/{plugin_id}/permissions")
async def plugin_permissions(publisher_id: str, plugin_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _subsystem(request).service.permission_projection(f"{publisher_id}/{plugin_id}")
    except PluginError as exc:
        raise _error(exc) from exc


@router.post("/{publisher_id}/{plugin_id}/permissions/approve")
async def approve_plugin_permission(publisher_id: str, plugin_id: str, body: PermissionActionRequest, request: Request) -> dict[str, Any]:
    try:
        packages = await _packages(body.package_id)
        return await _subsystem(request).approve_permission(
            f"{publisher_id}/{plugin_id}", packages, body.permission, "admin_api",
        )
    except PluginError as exc:
        raise _error(exc) from exc


@router.post("/{publisher_id}/{plugin_id}/permissions/revoke")
async def revoke_plugin_permission(publisher_id: str, plugin_id: str, body: PermissionActionRequest, request: Request) -> dict[str, Any]:
    try:
        return await _subsystem(request).revoke_permission(
            f"{publisher_id}/{plugin_id}", body.permission, "admin_api",
        )
    except PluginError as exc:
        raise _error(exc) from exc


@router.post("/{publisher_id}/{plugin_id}/install")
async def install_plugin(publisher_id: str, plugin_id: str, body: PluginActionRequest, request: Request) -> dict[str, Any]:
    identity = f"{publisher_id}/{plugin_id}"
    packages = await _packages(body.package_id)
    try:
        result = await _subsystem(request).install(identity, packages)
        automation = getattr(request.app.state, "automation_service", None)
        if automation is not None:
            await reconcile_plugin_update_task(automation, _subsystem(request))
        return result
    except PluginError as exc:
        raise _error(exc) from exc


@router.post("/{publisher_id}/{plugin_id}/update")
async def update_plugin(publisher_id: str, plugin_id: str, body: PluginActionRequest, request: Request) -> dict[str, Any]:
    return await install_plugin(publisher_id, plugin_id, body, request)


@router.post("/{publisher_id}/{plugin_id}/enable")
async def enable_plugin(publisher_id: str, plugin_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _subsystem(request).service.enable(f"{publisher_id}/{plugin_id}")
    except PluginError as exc:
        raise _error(exc) from exc


@router.post("/{publisher_id}/{plugin_id}/disable")
async def disable_plugin(publisher_id: str, plugin_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _subsystem(request).disable(f"{publisher_id}/{plugin_id}")
    except PluginError as exc:
        raise _error(exc) from exc


@router.post("/{publisher_id}/{plugin_id}/recover")
async def recover_plugin(publisher_id: str, plugin_id: str, request: Request) -> dict[str, Any]:
    try:
        return await _subsystem(request).service.recover_quarantine(f"{publisher_id}/{plugin_id}")
    except PluginError as exc:
        raise _error(exc) from exc


@router.delete("/{publisher_id}/{plugin_id}")
async def uninstall_plugin(publisher_id: str, plugin_id: str, request: Request) -> dict[str, Any]:
    try:
        removed = await _subsystem(request).uninstall(f"{publisher_id}/{plugin_id}")
        automation = getattr(request.app.state, "automation_service", None)
        if automation is not None:
            await reconcile_plugin_update_task(automation, _subsystem(request))
        return {"removed": removed}
    except PluginError as exc:
        raise _error(exc) from exc


@router.put("/ownership/{scheme}")
async def set_ownership(scheme: str, body: OwnershipRequest, request: Request) -> dict[str, Any]:
    try:
        return await _subsystem(request).set_ownership(scheme, body.mode, body.plugin)
    except PluginError as exc:
        raise _error(exc) from exc
