"""Supabase Auth (GoTrue) integration for the server-rendered dashboard.

This gates the HTML dashboard behind a real Supabase-Auth login. The flow is
cookie-based (not bearer-header), which suits server-rendered + HTMX pages:

* ``sign_in`` / ``sign_up`` call GoTrue and return a session (access + refresh).
* Tokens are stored in **httpOnly** cookies so browser JS cannot read them.
* :func:`auth_middleware` validates the access token on every request, lazily
  refreshes it when expired, and stashes the user on ``request.state.user``.
* :func:`require_user` is a FastAPI dependency that raises :class:`AuthRedirect`
  (handled in ``main.py``) to bounce anonymous visitors to ``/login``.

The legacy bearer-token auth in ``api/auth.py`` is untouched — it still guards
the JSON API + admin endpoints. This module is purely the dashboard login.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import Response
from starlette.types import ASGIApp

from config import settings

logger = logging.getLogger("lead-intel.api.supabase_auth")

# Cookie names for the access + refresh tokens.
ACCESS_COOKIE = "cf_at"
REFRESH_COOKIE = "cf_rt"

# Cookie lifetimes (seconds). Supabase access tokens are short-lived (~1h); the
# refresh token cookie outlives them so the middleware can mint fresh access
# tokens transparently.
_ACCESS_MAX_AGE = 60 * 60          # 1 hour
_REFRESH_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Paths that never require auth (static assets, the auth pages, health).
_PUBLIC_PREFIXES = (
    "/static",
    "/login",
    "/signup",
    "/logout",
    "/auth",  # OAuth start + callback
    "/health",
    "/favicon",
)

# Small in-process cache so we don't hit GoTrue on every single request for the
# same still-valid token. Maps access_token -> (user_dict, expires_at_epoch).
_USER_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_USER_CACHE_TTL = 60  # seconds


class AuthError(Exception):
    """Raised when a sign-in / sign-up call fails (bad credentials, etc.)."""


class AuthRedirect(Exception):
    """Raised by :func:`require_user`; handled in main.py to redirect to login."""

    def __init__(self, location: str = "/login") -> None:
        self.location = location
        super().__init__(location)


# ──────────────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _auth_client():
    """Return a cached Supabase client used for auth calls (anon key preferred)."""
    if not settings.auth_ready:
        raise RuntimeError("Supabase Auth not configured (SUPABASE_URL + key required)")
    from supabase import create_client

    if not settings.SUPABASE_ANON_KEY:
        logger.warning(
            "SUPABASE_ANON_KEY not set — using service key for auth. "
            "Set SUPABASE_ANON_KEY for production."
        )
    return create_client(settings.SUPABASE_URL, settings.supabase_auth_key)


def _user_to_dict(user: Any) -> dict[str, Any]:
    """Normalise a GoTrue user object/dict into a plain dict."""
    if user is None:
        return {}
    if isinstance(user, dict):
        meta = user.get("user_metadata") or {}
        return {
            "id": user.get("id"),
            "email": user.get("email"),
            "name": meta.get("name") or meta.get("full_name"),
            "metadata": meta,
        }
    meta = getattr(user, "user_metadata", None) or {}
    return {
        "id": getattr(user, "id", None),
        "email": getattr(user, "email", None),
        "name": meta.get("name") or meta.get("full_name") if isinstance(meta, dict) else None,
        "metadata": meta if isinstance(meta, dict) else {},
    }


def _session_to_dict(session: Any, user: Any) -> dict[str, Any]:
    """Extract access/refresh tokens + user from a GoTrue session."""
    if session is None:
        return {"access_token": None, "refresh_token": None, "user": _user_to_dict(user)}
    access = getattr(session, "access_token", None)
    refresh = getattr(session, "refresh_token", None)
    if access is None and isinstance(session, dict):
        access = session.get("access_token")
        refresh = session.get("refresh_token")
    return {
        "access_token": access,
        "refresh_token": refresh,
        "user": _user_to_dict(user or getattr(session, "user", None)),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Auth operations
# ──────────────────────────────────────────────────────────────────────────────
def sign_in(email: str, password: str) -> dict[str, Any]:
    """Sign in with email + password; return a session dict or raise AuthError."""
    try:
        res = _auth_client().auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean message
        logger.info("sign_in failed for %s: %s", email, exc)
        raise AuthError("Invalid email or password.") from exc
    session = _session_to_dict(getattr(res, "session", None), getattr(res, "user", None))
    if not session.get("access_token"):
        raise AuthError("Invalid email or password.")
    return session


def sign_up(email: str, password: str) -> dict[str, Any]:
    """Register a new user. If email confirmation is off, a session is returned.

    Returns ``{"session": <dict|None>, "needs_confirmation": bool}``.
    """
    try:
        res = _auth_client().auth.sign_up({"email": email, "password": password})
    except Exception as exc:  # noqa: BLE001
        logger.info("sign_up failed for %s: %s", email, exc)
        msg = str(exc)
        if "registered" in msg.lower() or "exists" in msg.lower():
            raise AuthError("That email is already registered. Try signing in.") from exc
        raise AuthError("Could not create account. Use a stronger password.") from exc
    session = _session_to_dict(getattr(res, "session", None), getattr(res, "user", None))
    return {
        "session": session if session.get("access_token") else None,
        "needs_confirmation": not session.get("access_token"),
    }


def oauth_authorize_url(provider: str, redirect_to: str) -> str:
    """Build the Supabase ``/authorize`` URL that kicks off an OAuth provider.

    The browser is redirected here; Supabase forwards to the provider (Google)
    and, after consent, back to ``redirect_to`` with the session in the URL.
    """
    from urllib.parse import urlencode

    base = (settings.SUPABASE_URL or "").rstrip("/")
    query = urlencode({"provider": provider, "redirect_to": redirect_to})
    return f"{base}/auth/v1/authorize?{query}"


def sign_out(access_token: Optional[str]) -> None:
    """Best-effort revoke of the session at GoTrue (cookies cleared separately)."""
    if not access_token:
        return
    _USER_CACHE.pop(access_token, None)
    try:
        _auth_client().auth.admin.sign_out(access_token)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - revoke is best-effort
        pass


def verify_token(access_token: str) -> Optional[dict[str, Any]]:
    """Validate an access token with GoTrue; return the user dict or None.

    Results are cached briefly to avoid a network round-trip per request.
    """
    if not access_token:
        return None
    cached = _USER_CACHE.get(access_token)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]
    try:
        res = _auth_client().auth.get_user(access_token)
    except Exception:  # noqa: BLE001 - invalid/expired token
        _USER_CACHE.pop(access_token, None)
        return None
    user = _user_to_dict(getattr(res, "user", None))
    if not user.get("id"):
        return None
    _USER_CACHE[access_token] = (user, now + _USER_CACHE_TTL)
    return user


def refresh(refresh_token: str) -> Optional[dict[str, Any]]:
    """Exchange a refresh token for a new session; return session dict or None."""
    if not refresh_token:
        return None
    try:
        res = _auth_client().auth.refresh_session(refresh_token)
    except Exception:  # noqa: BLE001 - refresh token revoked/expired
        return None
    session = _session_to_dict(getattr(res, "session", None), getattr(res, "user", None))
    return session if session.get("access_token") else None


# ──────────────────────────────────────────────────────────────────────────────
# Cookies
# ──────────────────────────────────────────────────────────────────────────────
def set_session_cookies(response: Response, access: str, refresh_tok: str) -> None:
    """Write the access + refresh tokens as httpOnly cookies on ``response``."""
    common = {
        "httponly": True,
        "secure": settings.COOKIE_SECURE,
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(ACCESS_COOKIE, access, max_age=_ACCESS_MAX_AGE, **common)
    response.set_cookie(REFRESH_COOKIE, refresh_tok, max_age=_REFRESH_MAX_AGE, **common)


def clear_session_cookies(response: Response) -> None:
    """Delete the session cookies (logout)."""
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")


# ──────────────────────────────────────────────────────────────────────────────
# Middleware + dependency
# ──────────────────────────────────────────────────────────────────────────────
def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p) for p in _PUBLIC_PREFIXES)


async def auth_middleware(request: Request, call_next):
    """Populate ``request.state.user`` and transparently refresh the session.

    Runs for every request. For public paths it just passes through. For gated
    paths it validates the access cookie, and if that fails but a refresh cookie
    exists, it mints a new session and rewrites the cookies on the response.
    """
    request.state.user = None

    if not settings.AUTH_ENABLED or _is_public(request.url.path):
        return await call_next(request)

    access = request.cookies.get(ACCESS_COOKIE)
    refresh_tok = request.cookies.get(REFRESH_COOKIE)

    user = verify_token(access) if access else None
    new_session: Optional[dict[str, Any]] = None

    if not user and refresh_tok:
        new_session = refresh(refresh_tok)
        if new_session:
            user = new_session["user"]

    request.state.user = user
    response = await call_next(request)

    # If we refreshed, persist the new tokens back to the browser.
    if new_session and new_session.get("access_token"):
        set_session_cookies(
            response, new_session["access_token"], new_session["refresh_token"]
        )
    return response


def require_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency: return the current user or raise AuthRedirect.

    When ``AUTH_ENABLED`` is False, returns a synthetic anonymous user so the
    dashboard stays usable in local/offline development.
    """
    if not settings.AUTH_ENABLED:
        return {"id": "anonymous", "email": "dev@local", "name": "Developer"}
    user = getattr(request.state, "user", None)
    if not user:
        raise AuthRedirect("/login")
    return user


def current_user(request: Request) -> Optional[dict[str, Any]]:
    """Return the current user if present, else None (no redirect)."""
    return getattr(request.state, "user", None)
