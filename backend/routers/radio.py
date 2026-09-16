"""Radio domain API and the explicit source-selection boundary."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

import database
from plugin_runtime import PluginError
from security.dependencies import require_admin, require_browse_access


router = APIRouter(tags=["radio"])


def _radio_resolver(request: Request):
    resolver = getattr(request.app.state, "radio_resolver", None)
    if resolver is None:
        raise HTTPException(status_code=503, detail="Radio runtime is unavailable")
    return resolver


def _active_radio_owner_identities(request: Request) -> frozenset[str]:
    subsystem = getattr(request.app.state, "plugin_subsystem", None)
    active = getattr(getattr(subsystem, "service", None), "_active", {})
    if not isinstance(active, dict):
        return frozenset()
    owners: set[str] = set()
    for identity, instance in active.items():
        state = getattr(getattr(instance, "state", None), "value", getattr(instance, "state", ""))
        if str(state).upper() != "HEALTHY_ACTIVE" or getattr(instance, "health", "healthy") != "healthy":
            continue
        manifest = getattr(instance, "manifest", None)
        contracts = getattr(manifest, "provider_contracts", ())
        radio_contract = next(
            (item for item in contracts if getattr(item, "contract", "") == "radio_provider"),
            None,
        )
        if radio_contract is None or "catalog" not in set(getattr(radio_contract, "features", ())):
            continue
        owned_schemes = getattr(manifest, "owned_schemes", ())
        if any(
            isinstance(item, (tuple, list)) and len(item) > 1 and item[1] == "radio_provider"
            for item in owned_schemes
        ):
            owners.add(str(identity))
    return frozenset(owners)


def _active_radio_provider(request: Request, identity: str):
    subsystem = getattr(request.app.state, "plugin_subsystem", None)
    service = getattr(subsystem, "service", None)
    instance = getattr(service, "_active", {}).get(identity) if service is not None else None
    state = getattr(getattr(instance, "state", None), "value", getattr(instance, "state", ""))
    if (instance is None or str(state).upper() != "HEALTHY_ACTIVE"
            or getattr(instance, "health", "healthy") != "healthy"):
        raise HTTPException(status_code=404, detail="Radio provider is unavailable")
    manifest_identity = str(getattr(getattr(instance, "manifest", None), "identity", "") or "")
    if manifest_identity != identity:
        raise HTTPException(status_code=404, detail="Radio provider identity mismatch")
    manifest = getattr(instance, "manifest", None)
    contract = next(
        (item for item in getattr(manifest, "provider_contracts", ())
         if getattr(item, "contract", "") == "radio_provider"),
        None,
    )
    if contract is None or "catalog" not in set(getattr(contract, "features", ())):
        raise HTTPException(status_code=404, detail="Plugin does not expose a Radio catalog")
    if not any(
        isinstance(item, (tuple, list)) and len(item) > 1 and item[1] == "radio_provider"
        for item in getattr(manifest, "owned_schemes", ())
    ):
        raise HTTPException(status_code=404, detail="Plugin has no Radio-owned scheme")
    return service.runtime, instance


@router.get("/api/radio/stations")
async def list_radio_stations(request: Request, _access=Depends(require_browse_access)):
    owners = _active_radio_owner_identities(request)
    return {
        "stations": await database.list_radio_stations(visible_owner_identities=owners),
        "catalog_states": await database.list_radio_catalog_states(owner_identities=owners),
    }


@router.get("/api/radio/stations/{station_id}")
async def get_radio_station(station_id: str, request: Request, _access=Depends(require_browse_access)):
    station = await database.get_radio_station(
        station_id, visible_owner_identities=_active_radio_owner_identities(request),
    )
    if station is None:
        raise HTTPException(status_code=404, detail="Radio station not found")
    return station


@router.get("/api/radio/stations/{station_id}/resolve")
async def resolve_radio_station(
    station_id: str,
    request: Request,
    source_id: str = Query("", description="Explicit persisted Radio source selector"),
    _access=Depends(require_browse_access),
):
    if not source_id.strip():
        raise HTTPException(status_code=400, detail="source_id is required")
    resolver = _radio_resolver(request)
    try:
        return await resolver.resolve_source(source_id.strip(), station_id=station_id)
    except PluginError as exc:
        status = 404 if exc.code in {"RESOURCE_NOT_FOUND", "SCHEME_CONFLICT"} else 503
        raise HTTPException(status_code=status, detail=exc.as_contract()) from exc


@router.get("/api/radio/stations/{station_id}/programme")
async def get_radio_programme(
    station_id: str,
    request: Request,
    source_id: str = Query("", description="Explicit persisted Radio source selector"),
    _access=Depends(require_browse_access),
):
    if not source_id.strip():
        raise HTTPException(status_code=400, detail="source_id is required")
    resolver = _radio_resolver(request)
    try:
        return await resolver.resolve_programme(source_id.strip(), station_id=station_id)
    except PluginError as exc:
        status = 404 if exc.code in {"RESOURCE_NOT_FOUND", "SCHEME_CONFLICT"} else 503
        raise HTTPException(status_code=status, detail=exc.as_contract()) from exc


@router.post("/api/radio/providers/{plugin_identity:path}/catalog/query")
async def query_radio_provider_catalog(
    plugin_identity: str,
    request: Request,
    payload: dict = Body(default_factory=dict),
    _access=Depends(require_browse_access),
):
    """Run a bounded provider-owned catalog query without replacing the Home snapshot.

    The payload remains opaque to Radio Core. Provider-specific query semantics
    such as country, tag, text search and cursors stay in the Plugin contract;
    the runtime still validates the returned bounded Radio catalog and active
    owner identity before it reaches the caller.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Radio catalog payload must be an object")
    runtime, instance = _active_radio_provider(request, plugin_identity)
    try:
        return await runtime.request(instance, "radio.catalog", payload, timeout=30.0)
    except PluginError as exc:
        status = 404 if exc.code in {"RESOURCE_NOT_FOUND", "SCHEME_CONFLICT"} else 503
        raise HTTPException(status_code=status, detail=exc.as_contract()) from exc


@router.post("/api/radio/plugins/{plugin_identity:path}/refresh")
async def refresh_radio_catalog(
    plugin_identity: str,
    request: Request,
    _admin=Depends(require_admin),
):
    subsystem = getattr(request.app.state, "plugin_subsystem", None)
    if subsystem is None:
        raise HTTPException(status_code=503, detail="Plugin subsystem is unavailable")
    result = await subsystem.refresh_radio_catalog(plugin_identity)
    if result.get("status") != "success":
        raise HTTPException(status_code=503, detail=result)
    return result
