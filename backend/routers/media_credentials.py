from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from security import database as security_db
from security.dependencies import require_admin
from security.media_credentials import create_credential


router = APIRouter(
    prefix="/api/admin/media-credentials",
    tags=["media-credentials"],
    dependencies=[Depends(require_admin)],
)


class CreateCredentialRequest(BaseModel):
    name: str = ""
    expiresInDays: int | None = 90
    scopes: list[str] = ["stream", "playlist", "epg"]


@router.get("")
async def list_credentials() -> dict:
    return {"credentials": await security_db.list_media_credentials()}


@router.post("")
async def create_media_credential(payload: CreateCredentialRequest) -> dict:
    credential = await create_credential(payload.name, payload.expiresInDays, payload.scopes)
    return {"credential": credential}


@router.delete("/{credential_id}")
async def revoke_media_credential(credential_id: int) -> dict:
    if not await security_db.revoke_media_credential(credential_id):
        raise HTTPException(status_code=404, detail="媒体凭证不存在或已吊销")
    return {"ok": True}
