"""``/companies`` routes — company + signal browsing."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from api.auth import require_client
from db import supabase_client as db

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("")
def list_companies(
    has_funding: Optional[bool] = None,
    has_hiring: Optional[bool] = None,
    min_score: Optional[int] = Query(None, ge=0, le=100),
    limit: int = Query(50, ge=1, le=500),
    _claims: dict[str, Any] = Depends(require_client),
) -> list[dict[str, Any]]:
    """Return companies with their attached signals, optionally filtered.

    ``min_score`` is accepted for API symmetry; scoring is client/vertical
    specific and lives on the leads resource, so it is not applied here.
    """
    return db.get_companies(
        has_funding=has_funding,
        has_hiring=has_hiring,
        limit=limit,
    )
