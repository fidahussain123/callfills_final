"""``/clients`` routes — admin-only client onboarding."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from api.auth import create_client_token, require_admin
from db import supabase_client as db
from db.models import ClientCreate

router = APIRouter(prefix="/clients", tags=["clients"])


@router.post("", status_code=201)
def create_client(
    payload: ClientCreate,
    _admin: bool = Depends(require_admin),
) -> dict[str, Any]:
    """Create a new tenant client and return it with a scoped access token.

    Requires the admin bearer token.
    """
    row = db.create_client_row(payload.model_dump())
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create client")
    token = create_client_token(str(row["id"]))
    return {"client": row, "access_token": token, "token_type": "bearer"}
