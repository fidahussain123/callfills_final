"""Pipelines — operator screens to manage lead feeds + user assignments.

A *pipeline* (campaign) is a named lead feed created by an operator: a vertical +
a described ICP (industries / cities / keywords / company size / signal focus) +
a min score, stored in the Supabase ``clients`` table. The operator can
**preview** the leads an ICP would fetch *before* creating it, then assign users
(who then see only that pipeline's slice). Scraping stays centralized — assigning
never lets a client trigger a paid scrape.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from api.supabase_auth import require_user
from api.tenancy import apply_icp, assign_user_to_client, resolve_tenant
from config import settings
from db import store
from verticals.base_vertical import list_verticals

logger = logging.getLogger("lead-intel.api.pipelines")

_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter(tags=["pipelines"])

# Maps the form's friendly "signal focus" checkboxes to canonical signal types.
_SIGNAL_MAP = {
    "hiring": ["hiring_post", "hiring_ad"],
    "funding": ["funding_round"],
    "intent": ["outsource_intent", "agency_switch", "growth_pain", "social_mention"],
}


def _csv(value: Optional[str]) -> list[str]:
    """Split a comma-separated form field into a clean list."""
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _to_int(value: Optional[str]) -> Optional[int]:
    """Parse an optional integer form field; None when blank/invalid."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _build_icp(
    *,
    description: str = "",
    industries: str = "",
    cities: str = "",
    keywords: str = "",
    employee_min: str = "",
    employee_max: str = "",
    signal_focus: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Assemble a pipeline's config/ICP blob (stored in ``clients.filters`` jsonb).

    ``description`` is display metadata (ignored by the lead filter); the rest are
    ICP filters consumed by :func:`api.tenancy.apply_icp`.
    """
    f: dict[str, Any] = {}
    if (description or "").strip():
        f["description"] = description.strip()
    if _csv(industries):
        f["industries"] = _csv(industries)
    if _csv(cities):
        f["cities"] = _csv(cities)
    if _csv(keywords):
        f["keywords"] = _csv(keywords)
    emn, emx = _to_int(employee_min), _to_int(employee_max)
    if emn is not None:
        f["employee_min"] = emn
    if emx is not None:
        f["employee_max"] = emx
    types = [t for s in (signal_focus or []) for t in _SIGNAL_MAP.get(s, [])]
    if types:
        f["signal_types"] = types
    return f


def _supabase_ready() -> bool:
    return bool(settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY)


def _require_operator(user: dict[str, Any]):
    """Return (tenant, None) if operator, else (None, redirect to dashboard)."""
    tenant = resolve_tenant(user)
    if not tenant.get("is_operator"):
        return None, RedirectResponse("/", status_code=303)
    return tenant, None


def _render_list(
    request: Request,
    user: dict[str, Any],
    tenant: dict[str, Any],
    *,
    notice: Optional[str] = None,
    error: Optional[str] = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the Pipelines list (create + assign + all pipelines)."""
    clients: list[dict[str, Any]] = []
    if _supabase_ready():
        try:
            from db import supabase_client as db

            clients = db.list_clients()
        except Exception as exc:  # noqa: BLE001
            error = error or f"Could not load pipelines ({exc}). Have you run migrations?"
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "user": user,
            "tenant": tenant,
            "clients": clients,
            "verticals": list_verticals(),
            "supabase_ready": _supabase_ready(),
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/pipelines", response_class=HTMLResponse)
def pipelines_page(request: Request, user: dict = Depends(require_user)):
    """List all pipelines (operators only)."""
    tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    return _render_list(request, user, tenant)


@router.post("/pipelines/preview", response_class=HTMLResponse)
def preview_pipeline(
    request: Request,
    user: dict = Depends(require_user),
    vertical: str = Form("recruitment"),
    min_score: int = Form(50),
    description: str = Form(""),
    industries: str = Form(""),
    cities: str = Form(""),
    keywords: str = Form(""),
    employee_min: str = Form(""),
    employee_max: str = Form(""),
    signal_focus: list[str] = Form([]),
):
    """Test-fetch: show the leads an ICP would return, WITHOUT saving anything."""
    tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    filters = _build_icp(
        description=description, industries=industries, cities=cities, keywords=keywords,
        employee_min=employee_min, employee_max=employee_max, signal_focus=signal_focus,
    )
    temp = {"vertical": vertical, "min_score": int(min_score), "filters": filters}
    pool = store.get_leads(limit=300)
    leads = apply_icp(pool, temp)
    return templates.TemplateResponse(
        request,
        "partials/pipeline_preview.html",
        {"leads": leads, "pool_size": len(pool), "min_score": int(min_score), "vertical": vertical},
    )


@router.post("/pipelines/create", response_class=HTMLResponse)
def create_pipeline(
    request: Request,
    user: dict = Depends(require_user),
    name: str = Form(...),
    vertical: str = Form("recruitment"),
    min_score: int = Form(60),
    description: str = Form(""),
    industries: str = Form(""),
    cities: str = Form(""),
    keywords: str = Form(""),
    employee_min: str = Form(""),
    employee_max: str = Form(""),
    signal_focus: list[str] = Form([]),
    assign_me: Optional[str] = Form(None),
):
    """Create a pipeline (a client + described ICP), optionally assigning the operator."""
    tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    if not _supabase_ready():
        return _render_list(request, user, tenant, error="Supabase isn't configured — add the keys and run migrations first.", status_code=400)

    filters = _build_icp(
        description=description, industries=industries, cities=cities, keywords=keywords,
        employee_min=employee_min, employee_max=employee_max, signal_focus=signal_focus,
    )
    payload = {
        "name": name.strip(),
        "vertical": vertical,
        "min_score": int(min_score),
        "filters": filters,
        "plan": "trial",
        "active": True,
    }
    from db import supabase_client as db

    row = db.create_client_row(payload)
    if not row:
        return _render_list(request, user, tenant, error="Failed to create the pipeline.", status_code=500)

    notice = f"Pipeline “{name.strip()}” created."
    if assign_me and user.get("email"):
        ok, err = assign_user_to_client(user["email"], str(row["id"]))
        notice += " You're now assigned to it." if ok else f" (couldn't assign you: {err})"
    logger.info("Created pipeline %s (%s)", name, row.get("id"))
    return _render_list(request, user, tenant, notice=notice)


@router.post("/pipelines/assign", response_class=HTMLResponse)
def assign_from_list(
    request: Request,
    user: dict = Depends(require_user),
    email: str = Form(...),
    client_id: str = Form(...),
):
    """Assign a user to a pipeline from the list screen."""
    tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    ok, err = assign_user_to_client(email, client_id)
    if ok:
        return _render_list(request, user, tenant, notice=f"Assigned {email} to the pipeline.")
    return _render_list(request, user, tenant, error=err or "Assignment failed.", status_code=400)


@router.get("/pipelines/{client_id}", response_class=HTMLResponse)
def pipeline_detail(
    request: Request,
    client_id: str,
    user: dict = Depends(require_user),
    notice: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """Elaborate one pipeline: its targeting, its scoped leads, assigned users."""
    tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    client = None
    members: list[dict[str, Any]] = []
    if _supabase_ready():
        try:
            from db import supabase_client as db

            client = db.get_client_by_id(client_id)
            if client:
                members = db.get_profiles_for_client(client_id)
        except Exception as exc:  # noqa: BLE001
            error = error or f"Could not load this pipeline ({exc})."
    if not client:
        return RedirectResponse("/pipelines", status_code=303)

    leads = apply_icp(store.get_leads(limit=300), client)
    return templates.TemplateResponse(
        request,
        "pipeline_detail.html",
        {
            "user": user,
            "tenant": tenant,
            "client": client,
            "leads": leads,
            "members": members,
            "stats": store.get_stats(),
            "notice": notice,
            "error": error,
        },
    )


@router.post("/pipelines/{client_id}/assign", response_class=HTMLResponse)
def assign_on_detail(
    request: Request,
    client_id: str,
    user: dict = Depends(require_user),
    email: str = Form(...),
):
    """Assign a user to this pipeline, then return to its detail page (PRG)."""
    _tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    ok, err = assign_user_to_client(email, client_id)
    if ok:
        q = f"?notice=Assigned+{quote_plus(email)}"
    else:
        q = f"?error={quote_plus(err or 'Assignment failed.')}"
    return RedirectResponse(f"/pipelines/{client_id}{q}", status_code=303)
