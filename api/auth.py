"""Authentication helpers for the FastAPI layer.

Two auth modes for Phase 1:

* **Admin token** — a static bearer token (from env) gates admin-only routes.
* **Client JWT** — a signed token carrying ``client_id`` scopes lead/company
  reads to a single tenant.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from config import settings

logger = logging.getLogger("lead-intel.api.auth")

_bearer = HTTPBearer(auto_error=True)


def create_client_token(client_id: str, expires_hours: int = 720) -> str:
    """Mint a client-scoped JWT (default 30-day expiry)."""
    payload = {
        "sub": client_id,
        "client_id": client_id,
        "scope": "client",
        "exp": datetime.now(timezone.utc) + timedelta(hours=expires_hours),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def require_admin(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> bool:
    """Dependency that allows only the static admin token."""
    if creds.credentials != settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin token required"
        )
    return True


def require_client(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict[str, Any]:
    """Dependency that validates a client JWT and returns its claims.

    The admin token is also accepted and resolves to an unscoped admin claim so
    operators can inspect any client's data.
    """
    token = creds.credentials
    if token == settings.ADMIN_TOKEN:
        return {"client_id": None, "scope": "admin"}
    try:
        claims = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc
    if not claims.get("client_id"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing client_id"
        )
    return claims
