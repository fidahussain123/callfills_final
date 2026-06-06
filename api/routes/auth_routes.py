"""Authentication routes for the dashboard — Supabase-Auth login / signup.

Server-rendered pages (not JSON): a visitor logs in at ``/login``, the session
tokens are written to httpOnly cookies, and they are redirected to the dashboard.
``/logout`` clears the cookies. ``/signup`` is gated by ``settings.ALLOW_SIGNUP``.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from api import supabase_auth as sa
from config import settings

logger = logging.getLogger("lead-intel.api.auth_routes")

_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter(tags=["auth"])


def _render(
    request: Request,
    *,
    mode: str = "login",
    error: str | None = None,
    notice: str | None = None,
    email: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """Render the shared auth page in either 'login' or 'signup' mode."""
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "mode": mode,
            "error": error,
            "notice": notice,
            "email": email,
            "allow_signup": settings.ALLOW_SIGNUP,
            "auth_ready": settings.auth_ready,
            "google_enabled": settings.GOOGLE_AUTH_ENABLED and settings.auth_ready,
        },
        status_code=status_code,
    )


def _redirect_to(request: Request) -> str:
    """Absolute URL Supabase should send the user back to after OAuth."""
    base = (settings.PUBLIC_BASE_URL or str(request.base_url)).rstrip("/")
    return f"{base}/auth/callback"


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request, error: str | None = Query(None)
) -> HTMLResponse:
    """Render the login form (redirect home if already authenticated)."""
    if sa.current_user(request):
        return RedirectResponse("/", status_code=303)
    return _render(request, mode="login", error=error)


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    """Authenticate with Supabase and set session cookies on success."""
    try:
        session = sa.sign_in(email.strip(), password)
    except sa.AuthError as exc:
        return _render(request, mode="login", error=str(exc), email=email, status_code=401)
    response = RedirectResponse("/", status_code=303)
    sa.set_session_cookies(response, session["access_token"], session["refresh_token"])
    logger.info("Login success: %s", email)
    return response


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request) -> HTMLResponse:
    """Render the signup form, or bounce to login when signups are disabled."""
    if not settings.ALLOW_SIGNUP:
        return RedirectResponse("/login", status_code=303)
    if sa.current_user(request):
        return RedirectResponse("/", status_code=303)
    return _render(request, mode="signup")


@router.post("/signup")
def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    """Create a Supabase user; log in directly or prompt for email confirmation."""
    if not settings.ALLOW_SIGNUP:
        return RedirectResponse("/login", status_code=303)
    try:
        result = sa.sign_up(email.strip(), password)
    except sa.AuthError as exc:
        return _render(request, mode="signup", error=str(exc), email=email, status_code=400)
    if result["needs_confirmation"]:
        return _render(
            request,
            mode="login",
            notice="Account created. Check your email to confirm, then sign in.",
            email=email,
        )
    session = result["session"]
    response = RedirectResponse("/", status_code=303)
    sa.set_session_cookies(response, session["access_token"], session["refresh_token"])
    logger.info("Signup + auto-login: %s", email)
    return response


@router.get("/auth/google")
def google_login(request: Request):
    """Kick off Google OAuth by redirecting to Supabase's authorize endpoint."""
    if not settings.auth_ready:
        return RedirectResponse("/login?error=Auth+not+configured", status_code=303)
    url = sa.oauth_authorize_url("google", _redirect_to(request))
    return RedirectResponse(url, status_code=303)


@router.get("/auth/callback", response_class=HTMLResponse)
def oauth_callback(request: Request) -> HTMLResponse:
    """Bridge page: reads the session from the URL fragment and posts it back.

    Supabase returns the tokens in the URL *fragment* (``#access_token=...``),
    which the server never receives. This tiny page extracts them in the browser
    and POSTs them to ``/auth/callback`` so we can set httpOnly cookies.
    """
    return templates.TemplateResponse(request, "auth_callback.html", {})


@router.post("/auth/callback")
def oauth_callback_submit(
    request: Request,
    access_token: str = Form(...),
    refresh_token: str = Form(""),
):
    """Validate the OAuth tokens from the bridge page and start the session."""
    user = sa.verify_token(access_token)
    if not user:
        return RedirectResponse("/login?error=Google+sign-in+failed", status_code=303)
    response = RedirectResponse("/", status_code=303)
    sa.set_session_cookies(response, access_token, refresh_token)
    logger.info("OAuth login success: %s", user.get("email"))
    return response


@router.get("/logout")
def logout(request: Request):
    """Clear session cookies (and best-effort revoke) then return to login."""
    sa.sign_out(request.cookies.get(sa.ACCESS_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    sa.clear_session_cookies(response)
    return response
