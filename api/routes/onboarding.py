"""Pipelines — operator screens to manage lead feeds + user assignments.

A *pipeline* (campaign) is a named lead feed created by an operator: a vertical +
a described ICP (industries / cities / keywords / company size / signal focus) +
a min score, stored in the Supabase ``clients`` table. The operator can
**preview** the leads an ICP would fetch *before* creating it, then assign users
(who then see only that pipeline's slice). Scraping stays centralized — assigning
never lets a client trigger a paid scrape.
"""

from __future__ import annotations

import json
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
from verticals.base_vertical import get_vertical_config, list_verticals

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

# Every tool a pipeline can toggle on: (registry key, label, one-line note).
# The selected vertical pre-checks its defaults; the operator can override.
_SOURCE_CATALOG: list[tuple[str, str, str]] = [
    ("google_maps", "Google Maps", "local businesses · map + draw-a-zone"),
    ("wellfound", "Wellfound", "startup hiring (VC-backed)"),
    ("rss_feeds", "Funding news", "funding rounds via RSS — free"),
    ("crunchbase", "Crunchbase", "funding rounds — paid · best-effort (actor flaky)"),
    ("indiamart", "IndiaMART", "India B2B suppliers — opt-in"),
    ("indeed", "Indeed", "job postings — fast & cheap"),
    ("naukri", "Naukri", "India job board — multi-run, costlier"),
    ("linkedin_jobs", "LinkedIn Jobs", "tech hiring — slower actor"),
    ("twitter", "Twitter / X", "social intent — AI-filtered"),
    ("facebook_ads", "Facebook Ads", "ad activity — AI-filtered"),
]
# NOTE: Reddit is deliberately NOT a pipeline tool. It powers the Signals page
# radar (auto-polling watcher) once the Reddit OAuth API keys are configured.
_SOURCE_KEYS = {key for key, _, _ in _SOURCE_CATALOG}


def _known_cities() -> list[str]:
    """City list for the create-form location autocomplete."""
    from processors.geocoder import known_cities

    return known_cities()


def _vertical_sources() -> dict[str, list[str]]:
    """Default (pre-checked) sources per vertical, for the form's JS sync."""
    from verticals.base_vertical import get_vertical_config

    return {v: list(get_vertical_config(v).get("sources") or []) for v in list_verticals()}


def _csv(value: Optional[str]) -> list[str]:
    """Split a comma-separated form field into a clean list."""
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _to_int(value: Optional[str]) -> Optional[int]:
    """Parse an optional integer form field; None when blank/invalid."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_zone(geo_zone: Optional[str]) -> Optional[dict[str, Any]]:
    """Parse the zone-picker's hidden GeoJSON field; None when blank/invalid.

    The form submits a GeoJSON geometry (Polygon drawn on the map) that maps
    1:1 onto the Apify actor's ``customGeolocation`` input.
    """
    if not geo_zone or not geo_zone.strip():
        return None
    try:
        data = json.loads(geo_zone)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("type") not in ("Polygon", "MultiPolygon", "Point"):
        return None
    if data.get("type") == "Polygon":
        rings = data.get("coordinates")
        if not (isinstance(rings, list) and rings and isinstance(rings[0], list) and len(rings[0]) >= 4):
            return None  # a closed triangle is the smallest valid ring
    return data


def _build_icp(
    *,
    description: str = "",
    industries: str = "",
    cities: str = "",
    keywords: str = "",
    employee_min: str = "",
    employee_max: str = "",
    signal_focus: Optional[list[str]] = None,
    geo_zone: str = "",
    sources: Optional[list[str]] = None,
    actor_id: str = "",
) -> dict[str, Any]:
    """Assemble a pipeline's config/ICP blob (stored in ``clients.filters`` jsonb).

    ``description`` is display metadata (ignored by the lead filter); the rest are
    ICP filters consumed by :func:`api.tenancy.apply_icp`. ``geo_zone`` (GeoJSON
    drawn on the map) becomes ``custom_geolocation``, the targeted search area
    used by the Google Maps scraper.
    """
    f: dict[str, Any] = {}
    if (description or "").strip():
        f["description"] = description.strip()
        # The description DRIVES the fetch: AI-parse it into structured filters
        # (job titles, max_followers, locations…) that the scrapers then use.
        try:
            from processors.brief_parser import parse_brief

            brief = parse_brief(description)
            for k in ("job_titles", "max_followers", "min_followers", "recently_posted"):
                if brief.get(k) is not None:
                    f[k] = brief[k]
            if brief.get("locations") and not _csv(cities):
                cities = ", ".join(brief["locations"])
            if brief.get("industries") and not _csv(industries):
                industries = ", ".join(brief["industries"])
        except Exception:  # noqa: BLE001 - parsing is best-effort
            pass
    zone = _parse_zone(geo_zone)
    if zone:
        f["custom_geolocation"] = zone
    # Per-pipeline tool toggles — only known registry keys are stored. Empty =
    # the vertical's defaults (so old pipelines keep working unchanged).
    chosen = [s for s in (sources or []) if s in _SOURCE_KEYS]
    if chosen:
        f["sources"] = chosen
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
    # Actor chosen via the AI Scraper Advisor (Apify actor id, e.g.
    # "compass/crawler-google-places") — the preferred scraper for this pipeline.
    if (actor_id or "").strip():
        f["actor_id"] = actor_id.strip()
    return f


def advisor_result(query: str) -> dict[str, Any]:
    """Run the AI Scraper Advisor for a source/need query (Apify store + Groq)."""
    from processors.actor_advisor import advise

    q = (query or "").strip()
    return advise(q, limit=6) if q else {"query": "", "actors": [], "note": ""}


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
            "source_catalog": _SOURCE_CATALOG,
            "vertical_sources": _vertical_sources(),
            "known_cities": _known_cities(),
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
async def preview_pipeline(
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
    geo_zone: str = Form(""),
    sources: list[str] = Form([]),
):
    """Test-fetch: show the leads an ICP would return, WITHOUT saving anything.

    Intent verticals filter the existing shared pool. When the pipeline's
    enabled tools include Google Maps, we run a small LIVE fetch for the given
    category + city/zone, since each Maps pipeline is its own targeted search.
    """
    tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    filters = _build_icp(
        description=description, industries=industries, cities=cities, keywords=keywords,
        employee_min=employee_min, employee_max=employee_max, signal_focus=signal_focus,
        geo_zone=geo_zone, sources=sources,
    )
    vcfg = get_vertical_config(vertical)
    effective_sources = filters.get("sources") or (vcfg.get("sources") or [])
    if "google_maps" in effective_sources:
        from pipeline import preview_run  # lazy import avoids a startup cycle

        search = {
            "categories": filters.get("keywords") or [],
            "cities": filters.get("cities") or [],
            "custom_geolocation": filters.get("custom_geolocation"),
            "sources": filters.get("sources"),
        }
        try:
            leads = await preview_run(vertical=vertical, search=search, max_results=8)
        except Exception as exc:  # noqa: BLE001
            logger.error("Maps preview failed: %s", exc)
            leads = []
        pool_size = len(leads)
    elif "linkedin_people" in effective_sources:
        from pipeline import preview_run  # live LinkedIn people search + follower filter

        search = {
            "job_titles": filters.get("job_titles") or filters.get("keywords") or [],
            "cities": filters.get("cities") or [],
            "industries": filters.get("industries") or [],
            "max_followers": filters.get("max_followers"),
            "employee_max": filters.get("employee_max"),
            "actor_id": filters.get("actor_id"),
            "sources": filters.get("sources"),
        }
        try:
            leads = await preview_run(vertical=vertical, search=search, max_results=10)
        except Exception as exc:  # noqa: BLE001
            logger.error("LinkedIn People preview failed: %s", exc)
            leads = []
        pool_size = len(leads)
    else:
        temp = {"vertical": vertical, "min_score": int(min_score), "filters": filters}
        pool = store.get_leads(limit=300)
        leads = apply_icp(pool, temp)
        pool_size = len(pool)
    return templates.TemplateResponse(
        request,
        "partials/pipeline_preview.html",
        {
            "leads": leads, "pool_size": pool_size, "min_score": int(min_score),
            "vertical": vertical, "geo_zone": filters.get("custom_geolocation"),
        },
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
    geo_zone: str = Form(""),
    sources: list[str] = Form([]),
    actor_id: str = Form(""),
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
        geo_zone=geo_zone, sources=sources, actor_id=actor_id,
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


@router.post("/pipelines/advisor", response_class=HTMLResponse)
def pipeline_advisor(request: Request, q: str = Form(""), user: dict = Depends(require_user)):
    """AI Scraper Advisor — best Apify actors + real costs for a source/need."""
    _tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request, "partials/actor_advisor.html", {"r": advisor_result(q)}
    )


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
