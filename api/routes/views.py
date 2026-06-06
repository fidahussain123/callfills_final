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
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from api.supabase_auth import require_user
from api.tenancy import resolve_tenant, scope_leads
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
    leads = scope_leads(tenant, store.get_leads(limit=300))
    return {normalize_company_name(l.get("company_name", "")) for l in leads}


@router.get("/dashboard/signals", response_class=HTMLResponse)
def signals_page(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    """Live signal feed — freshest intent first, with 'posted Xs/Xm ago'."""
    tenant = resolve_tenant(user)
    signals = store.get_signals()
    if not tenant.get("is_operator"):
        keys = _scoped_company_keys(tenant)
        signals = [s for s in signals if normalize_company_name(s.get("company_name", "")) in keys]

    enriched: list[dict[str, Any]] = []
    for s in signals:
        label, tier, secs = _age(s.get("detected_at"))
        enriched.append({**s, "_age": label, "_tier": tier, "_secs": secs})
    enriched.sort(key=lambda s: s["_secs"])  # freshest first
    tiers = Counter(s["_tier"] for s in enriched)

    return templates.TemplateResponse(
        request,
        "signals.html",
        {"user": user, "tenant": tenant, "signals": enriched, "tiers": tiers, "stats": store.get_stats()},
    )


@router.get("/dashboard/companies", response_class=HTMLResponse)
def companies_page(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    """Company directory — every company in the tenant's pool, with its info."""
    tenant = resolve_tenant(user)
    companies = scope_leads(tenant, store.get_leads(limit=300))
    return templates.TemplateResponse(
        request,
        "companies.html",
        {"user": user, "tenant": tenant, "companies": companies, "stats": store.get_stats()},
    )


@router.get("/dashboard/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, user: dict = Depends(require_user)) -> HTMLResponse:
    """Simple analytics over the tenant's leads — counts, sources, signal mix."""
    tenant = resolve_tenant(user)
    leads = scope_leads(tenant, store.get_leads(limit=300))
    total = len(leads)
    qualified = sum(1 for l in leads if (l.get("score") or 0) >= 60)
    warm = sum(1 for l in leads if 50 <= (l.get("score") or 0) < 60)
    src: Counter = Counter()
    typ: Counter = Counter()
    for l in leads:
        for s in (l.get("signal_sources") or []):
            src[s] += 1
        for t in (l.get("signal_types") or []):
            typ[t] += 1
    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "user": user,
            "tenant": tenant,
            "stats": store.get_stats(),
            "total": total,
            "qualified": qualified,
            "warm": warm,
            "cold": total - qualified - warm,
            "by_source": src.most_common(),
            "by_type": typ.most_common(),
            "max_source": max([c for _, c in src.most_common()], default=1),
            "max_type": max([c for _, c in typ.most_common()], default=1),
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
