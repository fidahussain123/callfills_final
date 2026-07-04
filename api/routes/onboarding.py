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
import threading
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
    ("linkedin_people", "LinkedIn People", "founders/CEOs/CTOs — decision-makers + emails"),
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


def _parse_overrides(raw: Optional[str]) -> dict[str, str]:
    """Parse the form's ``actor_overrides`` JSON → {source_key: apify_actor_id}.

    Only known registry source keys are kept, so a specific actor can be swapped
    in per source (the "Use this" picks). Blank/invalid → {}.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for key, actor in data.items():
            if key in _SOURCE_KEYS and isinstance(actor, str) and actor.strip():
                out[key] = actor.strip()
    return out


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
    actor_overrides: str = "",
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
            def _clean(vals):
                return [str(v).strip() for v in vals if str(v).strip().lower() not in ("", "null", "none")]
            if _clean(brief.get("locations") or []) and not _csv(cities):
                cities = ", ".join(_clean(brief["locations"]))
            if _clean(brief.get("industries") or []) and not _csv(industries):
                industries = ", ".join(_clean(brief["industries"]))
        except Exception:  # noqa: BLE001 - parsing is best-effort
            pass
    zone = _parse_zone(geo_zone)
    if zone:
        f["custom_geolocation"] = zone
    # Per-pipeline tool toggles — only known registry keys are stored. Empty =
    # the vertical's defaults (so old pipelines keep working unchanged).
    chosen = list(dict.fromkeys(s for s in (sources or []) if s in _SOURCE_KEYS))
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
    # "compass/crawler-google-places") — kept for back-compat (single actor).
    if (actor_id or "").strip():
        f["actor_id"] = actor_id.strip()
    # Per-source actor overrides from the advisor's multi-select "Use this":
    # {source_key: actor_id}. Each selected actor also implies its source is on,
    # so union those keys into the scraped sources.
    overrides = _parse_overrides(actor_overrides)
    if overrides:
        f["actor_overrides"] = overrides
        merged = list(dict.fromkeys((f.get("sources") or []) + list(overrides)))
        f["sources"] = [s for s in merged if s in _SOURCE_KEYS]
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


def _edit_context(edit_client: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Pre-fill helpers for the form when editing a pipeline (else empty)."""
    if not edit_client:
        return {"edit_client": None, "active_signals": [], "edit_seed": None}
    f = edit_client.get("filters") or {}
    have = set(f.get("signal_types") or [])
    active_signals = [name for name, types in _SIGNAL_MAP.items() if have & set(types)]
    edit_seed = {
        "sources": f.get("sources") or [],
        "actors": f.get("actor_overrides") or {},
        "actor_id": f.get("actor_id"),
        "geo": f.get("custom_geolocation"),
    }
    return {"edit_client": edit_client, "active_signals": active_signals, "edit_seed": edit_seed}


def _render_list(
    request: Request,
    user: dict[str, Any],
    tenant: dict[str, Any],
    *,
    notice: Optional[str] = None,
    error: Optional[str] = None,
    status_code: int = 200,
    edit_client: Optional[dict[str, Any]] = None,
) -> HTMLResponse:
    """Render the Pipelines list (create + assign + all pipelines).

    With ``edit_client`` the create form renders in EDIT mode: pre-filled and
    posting to the update route (the list/assign panels are hidden).
    """
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
            **_edit_context(edit_client),
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
    actor_id: str = Form(""),
    actor_overrides: str = Form(""),
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
        geo_zone=geo_zone, sources=sources, actor_id=actor_id, actor_overrides=actor_overrides,
    )
    vcfg = get_vertical_config(vertical)
    # Only sources the operator EXPLICITLY enabled ever live-scrape — never a
    # vertical default, so an empty selection can't surprise-scrape IndiaMART.
    explicit_sources = filters.get("sources") or []
    # Live test-fetch scrapes every enabled source AT ONCE (parallel), like a
    # real run — but ONLY those that honor a preview cap (max_results). The rest
    # (multi-run/uncapped actors like Naukri/Twitter) would spend a full run's
    # money on a test click, so they stay pool-backed in previews.
    _PREVIEWABLE = {"google_maps", "linkedin_people", "indiamart", "crunchbase",
                    "indeed", "rss_feeds", "hackernews"}
    live_sources = [s for s in explicit_sources if s in _PREVIEWABLE]
    skipped = [s for s in explicit_sources if s not in _PREVIEWABLE]
    if skipped:
        logger.info("Preview: skipping uncapped sources %s (pool-backed only)", skipped)
    if live_sources:
        from pipeline import preview_run  # lazy import avoids a startup cycle

        search = {
            "categories": filters.get("keywords") or [],
            "keywords": filters.get("keywords") or [],
            "cities": filters.get("cities") or [],
            "custom_geolocation": filters.get("custom_geolocation"),
            "job_titles": filters.get("job_titles") or [],
            "industries": filters.get("industries") or [],
            "max_followers": filters.get("max_followers"),
            "employee_max": filters.get("employee_max"),
            "actor_id": filters.get("actor_id"),
            "actor_overrides": filters.get("actor_overrides"),
            "sources": live_sources,
        }
        # LinkedIn People casts a wider net — the follower cap drops most profiles.
        cap = 30 if "linkedin_people" in live_sources else 8
        try:
            leads = await preview_run(vertical=vertical, search=search, max_results=cap)
        except Exception as exc:  # noqa: BLE001
            logger.error("Live preview failed: %s", exc)
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
    actor_overrides: str = Form(""),
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
        geo_zone=geo_zone, sources=sources, actor_id=actor_id, actor_overrides=actor_overrides,
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

    # Creating the pipeline also STARTS it — but ONLY when the operator enabled
    # at least one tool. With nothing selected we do NOT fall back to a vertical
    # default (that silently scraped IndiaMART); we save the pipeline and tell
    # the operator to add a tool.
    enabled = filters.get("sources") or []
    if enabled:
        def _kickoff(v: str = vertical, f: Optional[dict[str, Any]] = filters or None) -> None:
            import asyncio as aio

            from pipeline import run_pipeline

            try:
                aio.run(run_pipeline(vertical=v, search=f))
            except Exception as exc:  # noqa: BLE001
                logger.error("First scrape for new pipeline failed: %s", exc)

        threading.Thread(target=_kickoff, name="pipeline-kickoff", daemon=True).start()
        notice = f"Pipeline “{name.strip()}” created — first scrape is running now ({', '.join(enabled)})."
        logger.info("Created pipeline %s (%s); kickoff scrape started for %s", name, row.get("id"), enabled)
    else:
        notice = f"Pipeline “{name.strip()}” created. Add a tool (Google Maps or an actor) to start scraping."
        logger.info("Created pipeline %s (%s); no tool enabled — no scrape", name, row.get("id"))
    if assign_me and user.get("email"):
        ok, err = assign_user_to_client(user["email"], str(row["id"]))
        notice += " You're now assigned to it." if ok else f" (couldn't assign you: {err})"
    return _render_list(request, user, tenant, notice=notice)


@router.get("/pipelines/{client_id}/edit", response_class=HTMLResponse)
def edit_pipeline_form(request: Request, client_id: str, user: dict = Depends(require_user)):
    """Render the pipeline form pre-filled for editing an existing pipeline."""
    tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    if not _supabase_ready():
        return RedirectResponse("/pipelines", status_code=303)
    from db import supabase_client as db

    client = db.get_client_by_id(client_id)
    if not client:
        return RedirectResponse("/pipelines", status_code=303)
    return _render_list(request, user, tenant, edit_client=client)


@router.post("/pipelines/{client_id}/edit", response_class=HTMLResponse)
def update_pipeline(
    request: Request,
    client_id: str,
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
    actor_overrides: str = Form(""),
):
    """Save edits to an existing pipeline (no auto-scrape — use Run scrape)."""
    tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    if not _supabase_ready():
        return _render_list(request, user, tenant, error="Supabase isn't configured.", status_code=400)
    filters = _build_icp(
        description=description, industries=industries, cities=cities, keywords=keywords,
        employee_min=employee_min, employee_max=employee_max, signal_focus=signal_focus,
        geo_zone=geo_zone, sources=sources, actor_id=actor_id, actor_overrides=actor_overrides,
    )
    from db import supabase_client as db

    # NOTE: min_score is intentionally NOT written — the form has no min_score
    # field, so including it would clobber the stored threshold with the Form
    # default (60) on every save. Only the edited fields are updated.
    row = db.update_client_row(client_id, {
        "name": name.strip(),
        "vertical": vertical,
        "filters": filters,
    })
    if not row:
        return _render_list(request, user, tenant, error="Couldn't save the pipeline.", status_code=500)
    logger.info("Updated pipeline %s (%s)", name, client_id)
    return RedirectResponse(
        f"/pipelines/{client_id}?notice=" + quote_plus(f"Pipeline “{name.strip()}” updated."),
        status_code=303,
    )


@router.post("/pipelines/advisor", response_class=HTMLResponse)
def pipeline_advisor(request: Request, q: str = Form(""), user: dict = Depends(require_user)):
    """AI Scraper Advisor — best Apify actors + real costs for a source/need."""
    _tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request, "partials/actor_advisor.html", {"r": advisor_result(q)}
    )


@router.post("/pipelines/actor-schema", response_class=HTMLResponse)
def actor_schema(
    request: Request,
    id: str = Form(""),
    source: str = Form(""),
    user: dict = Depends(require_user),
):
    """Show an actor's live input fields so the operator sees what it searches on
    (and where their keywords go) before scraping."""
    _tenant, redirect = _require_operator(user)
    if redirect:
        return redirect
    from processors.actor_advisor import fetch_actor_schema

    return templates.TemplateResponse(
        request, "partials/actor_schema.html",
        {"r": fetch_actor_schema(id), "source": source},
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
