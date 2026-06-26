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
    """Stat-card data derived from the (already tenant-scoped) live lead list.

    Derived from ``leads`` rather than the last run's ``latest.json`` so the cards
    always match the leads actually on screen and stay correct when leads are read
    from the database (where ``latest.json`` may not exist, e.g. on Render).
    """
    qualified = sum(1 for lead in leads if (lead.get("score") or 0) >= 60)
    signals = sum(len(lead.get("signals") or []) for lead in leads)
    companies = len({(lead.get("company_name") or "").strip().lower() for lead in leads})
    return {
        "generated_at": store.get_stats().get("generated_at"),
        "counts": {"leads": len(leads), "companies": companies, "signals": signals},
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
    """Overview: stat cards + map + compact qualified/top lead lists.

    The full, filterable, paginated table lives on the Leads page — the
    dashboard is a glanceable summary, not the working list.
    """
    tenant = resolve_tenant(user)
    leads = scope_leads(tenant, store.get_leads(limit=300))
    top_leads = leads[:8]  # store returns best-first (internal rank, no visible score)

    def _latest(lead: dict[str, Any]) -> str:
        sigs = lead.get("signals") or []
        return (sigs[0].get("detected_at") or "") if sigs else ""

    recent_leads = sorted(leads, key=_latest, reverse=True)[:8]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "leads": leads,
            "top_leads": top_leads,
            "recent_leads": recent_leads,
            "stats": _stats_for(tenant, leads),
            "sources": _sources_for(tenant, leads),
            "verticals": list_verticals(),
            "user": user,
            "tenant": tenant,
        },
    )


# Leads page shows a manageable page at a time, not the whole pool.
_PAGE_SIZE = 25


@router.get("/dashboard/leads-view", response_class=HTMLResponse)
def leads_view(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    """Dedicated leads page: filters + per-source counts + paginated table."""
    tenant = resolve_tenant(user)
    all_leads = scope_leads(tenant, store.get_leads(limit=1000))
    source_counts: dict[str, int] = {}
    for lead in all_leads:
        for src in lead.get("signal_sources") or []:
            source_counts[src] = source_counts.get(src, 0) + 1
    return templates.TemplateResponse(
        request,
        "leads.html",
        {
            "leads": all_leads[:_PAGE_SIZE],
            "total": len(all_leads),
            "offset": 0,
            "page_size": _PAGE_SIZE,
            "source_counts": sorted(source_counts.items(), key=lambda kv: -kv[1]),
            "stats": _stats_for(tenant, all_leads),
            "sources": _sources_for(tenant, all_leads),
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
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_user),
) -> HTMLResponse:
    """HTMX partial: one page of the filtered, tenant-scoped lead table."""
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
            limit=1000,
        ),
    )
    total = len(leads)
    offset = min(offset, max(0, total - 1)) if total else 0
    return templates.TemplateResponse(
        request,
        "partials/lead_table.html",
        {
            "leads": leads[offset : offset + _PAGE_SIZE],
            "total": total,
            "offset": offset,
            "page_size": _PAGE_SIZE,
        },
    )


@router.get("/dashboard/leads/{company_name}", response_class=HTMLResponse)
def lead_detail(
    request: Request, company_name: str, user: dict = Depends(require_user)
) -> HTMLResponse:
    """HTMX partial: one Lead Card drawer (only if inside the tenant's scope)."""
    tenant = resolve_tenant(user)
    # A company can exist under several verticals (old + new pipelines); show
    # the copy inside the tenant's scope — the same one their lists render.
    in_scope = scope_leads(tenant, store.get_lead_matches(company_name))
    lead = in_scope[0] if in_scope else None
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
    search: Optional[dict[str, Any]] = None
    if not tenant.get("is_operator"):
        client = tenant.get("client")
        if not client:
            return templates.TemplateResponse(
                request,
                "partials/toast.html",
                {"message": "No pipeline is assigned to you yet."},
            )
        vertical = client.get("vertical") or vertical  # lock clients to their vertical
        search = client.get("filters") or None  # targeted search (e.g. Maps category/city)

    if vertical in _inflight:
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": f"A '{vertical}' scrape is already running — refresh in a few minutes."},
        )

    from pipeline import RUN_STATUS, run_pipeline

    # Seed a "starting" status so the first poll shows the panel immediately.
    RUN_STATUS.update({
        "running": True, "stage": "starting", "vertical": vertical,
        "sources": {}, "leads": None, "qualified": None,
    })

    async def _job() -> None:
        try:
            await run_pipeline(vertical=vertical, search=search)
        except Exception as exc:  # noqa: BLE001
            logger.error("Pipeline run failed: %s", exc)
            RUN_STATUS.update({"running": False, "stage": "error"})
        finally:
            _inflight.discard(vertical)
            if RUN_STATUS.get("running"):
                RUN_STATUS.update({"running": False, "stage": "error"})

    _inflight.add(vertical)
    asyncio.create_task(_job())
    logger.info("Pipeline run triggered for %s by %s", vertical, user.get("email"))
    return templates.TemplateResponse(request, "partials/run_panel.html", {"run": RUN_STATUS})


@router.get("/dashboard/run-status", response_class=HTMLResponse)
def run_status(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    """Live scrape-progress partial — polled by the run panel every ~1.5s."""
    from pipeline import RUN_STATUS

    return templates.TemplateResponse(request, "partials/run_status.html", {"run": RUN_STATUS})
