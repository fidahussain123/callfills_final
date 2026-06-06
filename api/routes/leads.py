"""``/leads`` routes — client-scoped lead retrieval."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import require_client
from db import supabase_client as db

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("")
def list_leads(
    status: Optional[str] = Query(None, description="new, contacted, replied, qualified, closed"),
    min_score: Optional[int] = Query(None, ge=0, le=100),
    vertical: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    claims: dict[str, Any] = Depends(require_client),
) -> list[dict[str, Any]]:
    """Return leads for the authenticated client, sorted by score descending.

    Admin tokens may pass ``client_id`` via the ``vertical`` filter is not used
    for scoping; admins see all clients only when a client_id is encoded in the
    JWT. For Phase 1 a client JWT is required to scope results.
    """
    client_id = claims.get("client_id")
    if not client_id:
        raise HTTPException(
            status_code=400,
            detail="A client-scoped token is required to list leads",
        )
    return db.get_leads(
        client_id=client_id,
        status=status,
        min_score=min_score,
        vertical=vertical,
        limit=limit,
        offset=offset,
    )
