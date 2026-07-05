"""Extra dashboard pages — Signals feed, Companies directory, Analytics, Export.

All tenant-scoped: operators see the whole pool; client members see only the
companies/signals inside their pipeline's ICP slice. The Signals feed surfaces
the freshest intent first ("posted Xs/Xm ago") — the speed-to-lead view.
"""

from __future__ import annotations

import csv
import io
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from api.supabase_auth import require_user
from api.tenancy import resolve_tenant, scope_leads
from config import settings
from db import store
from processors.quality import normalize_company_name

logger = logging.getLogger("lead-intel.api.views")

_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter(tags=["views"])


def _age(detected_at: Any) -> tuple[str, str, float]:
    """Return (label, tier, seconds) for a signal's recency.

    Tiers drive the freshness colour: live (<1 min), fresh (<1 h), recent
    (<24 h), ok (<30 d), stale (older). This is the "posted Xs/Xm ago" view.
    """
    if not detected_at:
        return ("—", "stale", 1e12)
    try:
        dt = datetime.fromisoformat(str(detected_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return ("—", "stale", 1e12)
    secs = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return (f"{int(secs)}s ago", "live", secs)
    if secs < 3600:
        return (f"{int(secs // 60)}m ago", "fresh", secs)
    if secs < 86400:
        return (f"{int(secs // 3600)}h ago", "recent", secs)
    if secs < 2592000:
        return (f"{int(secs // 86400)}d ago", "ok", secs)
    return (dt.strftime("%Y-%m-%d"), "stale", secs)


def _scoped_company_keys(tenant: dict[str, Any]) -> set[str]:
    """Normalized company names the tenant is allowed to see (clients only)."""
    leads = scope_leads(tenant, store.get_leads(limit=2000))
    return {normalize_company_name(l.get("company_name", "")) for l in leads}


# The Signals page is the SOCIAL radar: raw posts scraped from social media.
# Job-board/funding evidence lives on the Leads/Companies pages instead.
_SOCIAL_PLATFORMS = ("reddit", "twitter", "facebook", "hackernews")


@router.get("/dashboard/signals", response_class=HTMLResponse)
def signals_page(
    request: Request,
    platform: Optional[str] = Query(None),
    user: dict = Depends(require_user),
) -> HTMLResponse:
    """Social intent radar — posts from Reddit, X, Facebook…, freshest first."""
    tenant = resolve_tenant(user)
    signals = [
        s for s in store.get_signals()
        if s.get("source_platform") in _SOCIAL_PLATFORMS
    ]
    # Union in the real-time radar feed (AI-verified, stored separately so a
    # pipeline run can never overwrite it). Dedupe by source_url.
    from radar import get_radar_signals

    seen_urls = {s.get("source_url") for s in signals}
    for s in get_radar_signals():
        if s.get("source_url") not in seen_urls:
            signals.append(s)
            seen_urls.add(s.get("source_url"))
    if not tenant.get("is_operator"):
        keys = _scoped_company_keys(tenant)
        signals = [s for s in signals if normalize_company_name(s.get("company_name", "")) in keys]

    platform_counts = Counter(s.get("source_platform") for s in signals)
    if platform in _SOCIAL_PLATFORMS:
        signals = [s for s in signals if s.get("source_platform") == platform]

    enriched: list[dict[str, Any]] = []
    for s in signals:
        label, tier, secs = _age(s.get("detected_at"))
        enriched.append({**s, "_age": label, "_tier": tier, "_secs": secs})
    enriched.sort(key=lambda s: s["_secs"])  # freshest first
    tiers = Counter(s["_tier"] for s in enriched)

    return templates.TemplateResponse(
        request,
        "signals.html",
        {
            "user": user, "tenant": tenant, "signals": enriched, "tiers": tiers,
            "stats": store.get_stats(), "platform": platform if platform in _SOCIAL_PLATFORMS else None,
            "platform_counts": platform_counts,
            "radar_enabled": settings.RADAR_ENABLED,
        },
    )


@router.get("/dashboard/companies", response_class=HTMLResponse)
def companies_page(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    """Company directory — every company in the tenant's pool, with its info."""
    tenant = resolve_tenant(user)
    companies = scope_leads(tenant, store.get_leads(limit=2000))
    return templates.TemplateResponse(
        request,
        "companies.html",
        {"user": user, "tenant": tenant, "companies": companies, "stats": store.get_stats()},
    )


@router.get("/dashboard/companies/{company_name}", response_class=HTMLResponse)
def company_detail(
    request: Request, company_name: str, user: dict = Depends(require_user)
) -> HTMLResponse:
    """HTMX drawer: one company — where its signals come from, details, profiles."""
    tenant = resolve_tenant(user)
    # Same-name copies can exist across verticals; show the in-scope one.
    in_scope = scope_leads(tenant, store.get_lead_matches(company_name))
    company = in_scope[0] if in_scope else None

    sources: list[dict[str, Any]] = []
    max_count = 1
    if company:
        by_src: dict[str, dict[str, Any]] = {}
        for sig in company.get("signals") or []:
            key = sig.get("source_platform") or "other"
            entry = by_src.setdefault(
                key, {"platform": key, "count": 0, "latest": "", "url": None, "types": set()}
            )
            entry["count"] += 1
            detected = sig.get("detected_at") or ""
            if detected >= entry["latest"]:
                entry["latest"] = detected
                entry["url"] = sig.get("source_url")
            if sig.get("signal_type"):
                entry["types"].add(sig["signal_type"])
        sources = sorted(by_src.values(), key=lambda e: -e["count"])
        for entry in sources:
            entry["types"] = sorted(entry["types"])
        max_count = max((e["count"] for e in sources), default=1)

    return templates.TemplateResponse(
        request,
        "partials/company_detail.html",
        {"company": company, "sources": sources, "max_count": max_count},
    )


@router.get("/dashboard/companies/{company_name}/contacts", response_class=HTMLResponse)
def company_contacts(
    request: Request, company_name: str, user: dict = Depends(require_user)
) -> HTMLResponse:
    """On-demand Apollo lookup (the 'Reveal contacts' button): profile + leaders.

    This is the ONLY place Apollo is called — never on a scrape — so credits are
    spent only when a client explicitly asks for one company's contacts.
    """
    from processors import enricher

    tenant = resolve_tenant(user)
    # Same-name copies can exist across verticals; use the in-scope one.
    in_scope = scope_leads(tenant, store.get_lead_matches(company_name))
    lead = in_scope[0] if in_scope else None
    result = None
    if lead:
        domain = lead.get("company_domain") or lead.get("website")
        result = enricher.enrich_on_demand(domain, lead.get("company_name", ""))
    return templates.TemplateResponse(
        request, "partials/company_contacts.html", {"company": lead, "r": result}
    )


@router.get("/dashboard/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    """Analytics over the tenant's leads — coverage by source, signal types,
    and the intent profile (no scoring; just where leads come from)."""
    tenant = resolve_tenant(user)
    leads = scope_leads(tenant, store.get_leads(limit=2000))
    total = len(leads)

    src: Counter = Counter()
    typ: Counter = Counter()
    hiring = funding = social = 0
    signals_total = 0
    for l in leads:
        for s in (l.get("signal_sources") or []):
            src[s] += 1
        for t in (l.get("signal_types") or []):
            typ[t] += 1
        if l.get("has_hiring"):
            hiring += 1
        if l.get("has_funding"):
            funding += 1
        if l.get("has_social"):
            social += 1
        signals_total += len(l.get("signals") or [])

    # Most-active companies (by evidence volume) — replaces the old score leaderboard.
    most_active = sorted(leads, key=lambda l: len(l.get("signals") or []), reverse=True)[:10]
    top_companies = [
        {"name": (l.get("company_name") or "—")[:26], "signals": len(l.get("signals") or [])}
        for l in most_active if l.get("signals")
    ]

    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "user": user,
            "tenant": tenant,
            "stats": store.get_stats(),
            "total": total,
            "companies": total,
            "sources_count": len(src),
            "signals_total": signals_total,
            "by_source": src.most_common(),
            "by_type": typ.most_common(),
            "signal_profile": {"Hiring": hiring, "Funding": funding, "Social": social},
            "top_companies": top_companies,
        },
    )


@router.get("/dashboard/export")
def export_csv(user: dict = Depends(require_user)) -> Response:
    """Download the tenant's leads as CSV."""
    tenant = resolve_tenant(user)
    leads = scope_leads(tenant, store.get_leads(limit=1000))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["company", "website", "location", "score", "status", "hiring", "funding", "sources", "signals", "latest"]
    )
    for l in leads:
        score = l.get("score") or 0
        sigs = l.get("signals") or []
        latest = (sigs[0].get("detected_at") or "")[:10] if sigs else ""
        writer.writerow([
            l.get("company_name", ""),
            l.get("website") or l.get("company_domain") or "",
            l.get("location") or "",
            score,
            "qualified" if score >= 60 else ("warm" if score >= 50 else "cold"),
            "yes" if l.get("has_hiring") else "no",
            "yes" if l.get("has_funding") else "no",
            "|".join(l.get("signal_sources") or []),
            len(sigs),
            latest,
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="callfills-leads.csv"'},
    )
