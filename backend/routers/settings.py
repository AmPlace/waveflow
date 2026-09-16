from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from core.settings_service import SettingsValidationError, list_runtime_settings, update_runtime_settings
from security.dependencies import require_admin


router = APIRouter(
    prefix="/api/admin/settings",
    tags=["settings"],
)


@router.get("/security")
async def get_security_settings(admin: dict = Depends(require_admin)) -> dict:
    return await list_runtime_settings()


@router.put("/security")
async def put_security_settings(payload: dict, admin: dict = Depends(require_admin)) -> dict:
    try:
        return await update_runtime_settings(payload or {}, updated_by=admin.get("id"))
    except SettingsValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
