"""Dashboard routes — server-rendered lead intelligence UI (HTMX + Jinja2).

Each request is scoped to the logged-in user's tenant (see ``api/tenancy.py``):
operators see the whole lead pool; client members see only their ICP slice. It
reads from the storage facade, so it works in Apify-only JSON mode with no DB.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from api.supabase_auth import require_user
from api.tenancy import resolve_tenant, scope_leads
from db import store
from verticals.base_vertical import list_verticals

logger = logging.getLogger("lead-intel.api.dashboard")

_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter(tags=["dashboard"])


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    """Map a tri-state query string ('', 'yes', 'no') to Optional[bool]."""
    if value in (None, "", "any"):
        return None
    return value == "yes"


def _stats_for(tenant: dict[str, Any], leads: list[dict[str, Any]]) -> dict[str, Any]:
    """Stat-card data: global for operators, derived from the slice for clients."""
    if tenant.get("is_operator"):
        return store.get_stats()
    qualified = sum(1 for lead in leads if (lead.get("score") or 0) >= 60)
    signals = sum(len(lead.get("signals") or []) for lead in leads)
    return {
        "generated_at": store.get_stats().get("generated_at"),
        "counts": {"leads": len(leads), "companies": len(leads), "signals": signals},
        "stats": {"leads_meeting_threshold": qualified},
    }


def _sources_for(tenant: dict[str, Any], leads: list[dict[str, Any]]) -> list[str]:
    """Source filter options: global for operators, present-in-slice for clients."""
    if tenant.get("is_operator"):
        return store.available_sources()
    found: set[str] = set()
    for lead in leads:
        found.update(lead.get("signal_sources") or [])
    return sorted(found)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    """Render the dashboard shell, scoped to the user's tenant."""
    tenant = resolve_tenant(user)
    leads = scope_leads(tenant, store.get_leads(limit=300))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "leads": leads,
            "stats": _stats_for(tenant, leads),
            "sources": _sources_for(tenant, leads),
            "verticals": list_verticals(),
            "user": user,
            "tenant": tenant,
        },
    )


@router.get("/dashboard/leads-view", response_class=HTMLResponse)
def leads_view(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    """Dedicated full-page leads table (no stat cards) — the 'Leads' nav item."""
    tenant = resolve_tenant(user)
    leads = scope_leads(tenant, store.get_leads(limit=300))
    return templates.TemplateResponse(
        request,
        "leads.html",
        {
            "leads": leads,
            "stats": _stats_for(tenant, leads),
            "sources": _sources_for(tenant, leads),
            "verticals": list_verticals(),
            "user": user,
            "tenant": tenant,
        },
    )


@router.get("/dashboard/leads", response_class=HTMLResponse)
def leads_partial(
    request: Request,
    q: Optional[str] = Query(None),
    min_score: Optional[int] = Query(None, ge=0, le=100),
    source: Optional[str] = Query(None),
    funding: Optional[str] = Query(None),
    hiring: Optional[str] = Query(None),
    threshold: Optional[str] = Query(None),
    user: dict = Depends(require_user),
) -> HTMLResponse:
    """HTMX partial: the filtered, tenant-scoped lead table body."""
    tenant = resolve_tenant(user)
    leads = scope_leads(
        tenant,
        store.get_leads(
            min_score=min_score,
            source=source or None,
            has_funding=_parse_bool(funding),
            has_hiring=_parse_bool(hiring),
            only_threshold=threshold == "yes",
            search=q or None,
            limit=300,
        ),
    )
    return templates.TemplateResponse(
        request, "partials/lead_table.html", {"leads": leads}
    )


@router.get("/dashboard/leads/{company_name}", response_class=HTMLResponse)
def lead_detail(
    request: Request, company_name: str, user: dict = Depends(require_user)
) -> HTMLResponse:
    """HTMX partial: one Lead Card drawer (only if inside the tenant's scope)."""
    tenant = resolve_tenant(user)
    lead = store.get_lead(company_name)
    # Enforce scope: a client member can't open a lead outside their ICP.
    if lead and not scope_leads(tenant, [lead]):
        lead = None
    return templates.TemplateResponse(
        request, "partials/lead_detail.html", {"lead": lead}
    )


# Verticals with a scrape currently running — single-flight guard so concurrent
# clicks (operator or client) can't pile up duplicate paid runs / file races.
_inflight: set[str] = set()


@router.post("/dashboard/run", response_class=HTMLResponse)
async def run_now(
    request: Request,
    vertical: str = Form("recruitment"),
    user: dict = Depends(require_user),
) -> HTMLResponse:
    """Trigger a pipeline run and return a status toast.

    Operators choose the vertical; client members run their own pipeline's
    vertical (locked to it). A single-flight guard prevents duplicate concurrent
    scrapes of the same vertical.
    """
    tenant = resolve_tenant(user)
    if not tenant.get("is_operator"):
        client = tenant.get("client")
        if not client:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {"message": "No pipeline is assigned to you yet."},
            )
        vertical = client.get("vertical") or vertical  # lock clients to their vertical

    if vertical in _inflight:
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": f"A '{vertical}' scrape is already running — refresh in a few minutes."},
        )

    from pipeline import run_pipeline

    async def _job() -> None:
        try:
            await run_pipeline(vertical=vertical)
        finally:
            _inflight.discard(vertical)

    _inflight.add(vertical)
    asyncio.create_task(_job())
    logger.info("Pipeline run triggered for %s by %s", vertical, user.get("email"))
    return templates.TemplateResponse(
        request,
        "partials/toast.html",
        {"message": f"Pipeline started for '{vertical}'. Refresh in a few minutes."},
    )
