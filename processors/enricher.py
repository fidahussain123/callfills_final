"""Apollo.io enrichment.

Two tiers, because Apollo gates them differently:

1. **Organization enrichment** (``/organizations/enrich``) — available on the
   FREE plan. Fills firmographics for any company with a domain: industry,
   employee count, LinkedIn, company phone, founded year, website.
2. **People search → match** (``/mixed_people/search`` + ``/people/match``) —
   returns a named decision-maker + verified email, but is **paid-plan only**.
   It is off by default (``APOLLO_PEOPLE_ENABLED``) and auto-disables for the run
   on the first 403 so we never spam a gated endpoint.

Everything is fail-open: with no key, or on any error/gating, companies simply
get ``enrichment = None`` and the pipeline still produces leads.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import httpx

from config import settings

logger = logging.getLogger("lead-intel.processors.enricher")

_BASE = "https://api.apollo.io/api/v1"
_ORG_URL = f"{_BASE}/organizations/enrich"
_SEARCH_URL = f"{_BASE}/mixed_people/search"
_MATCH_URL = f"{_BASE}/people/match"

_PERSON_TITLES = [
    "CTO", "VP Engineering", "Head of Engineering",
    "Co-Founder", "Founder", "CEO", "Head of Talent",
]
# Lower index = higher priority when choosing among returned people.
_TITLE_PRIORITY = [t.lower() for t in _PERSON_TITLES]

_RATE_LIMIT_SLEEP = 0.4
_MAX_RETRIES = 3

# Set True for the rest of a run once the People API returns 403 (gated plan),
# so we stop calling a paid-only endpoint after the first rejection.
_people_blocked = False


def _headers() -> dict[str, str]:
    """Apollo auth header (key in the HEADER, never the URL — per their notice)."""
    return {
        "X-Api-Key": settings.APOLLO_API_KEY or "",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


def _bare_domain(value: Optional[str]) -> Optional[str]:
    """Reduce a URL/domain to the bare host Apollo expects (``razorpay.com``)."""
    if not value:
        return None
    host = re.sub(r"^https?://", "", str(value).strip().lower()).split("/")[0].split("?")[0]
    return host.removeprefix("www.") or None


def _title_rank(title: str) -> int:
    """Sort rank for a person's title (lower = more desirable)."""
    lowered = (title or "").lower()
    for i, keyword in enumerate(_TITLE_PRIORITY):
        if keyword in lowered:
            return i
    return len(_TITLE_PRIORITY)


def _post(client: httpx.Client, url: str, body: dict[str, Any]) -> tuple[int, Optional[dict[str, Any]]]:
    """POST with 429 backoff. Returns ``(status_code, json|None)``; (0, None) on error."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = client.post(url, headers=_headers(), json=body)
        except Exception as exc:  # noqa: BLE001 - network: fail open
            logger.error("Apollo call failed (%s): %s", url, exc)
            return (0, None)
        if resp.status_code == 429:
            time.sleep(_RATE_LIMIT_SLEEP * (2 ** attempt))
            continue
        try:
            return (resp.status_code, resp.json())
        except Exception:  # noqa: BLE001
            return (resp.status_code, None)
    return (429, None)


def _enrich_org(client: httpx.Client, domain: str) -> Optional[dict[str, Any]]:
    """Organization firmographics (free plan). Returns ``org_*`` fields or None."""
    code, data = _post(client, _ORG_URL, {"domain": domain})
    if code != 200 or not data:
        return None
    org = data.get("organization") or {}
    if not org:
        return None
    phone = org.get("phone")
    if not phone and isinstance(org.get("primary_phone"), dict):
        phone = org["primary_phone"].get("number")
    return {
        "org_name": org.get("name"),
        "org_industry": org.get("industry"),
        "org_employees": org.get("estimated_num_employees"),
        "org_linkedin": org.get("linkedin_url"),
        "org_website": org.get("website_url") or org.get("primary_domain"),
        "org_phone": phone,
        "org_founded": org.get("founded_year"),
        "org_city": org.get("city"),
        "org_state": org.get("state"),
        "org_country": org.get("country"),
    }


def _enrich_person(client: httpx.Client, domain: str, company_name: str) -> Optional[dict[str, Any]]:
    """Decision-maker + verified email (paid plan). Auto-disables on 403."""
    global _people_blocked
    if _people_blocked:
        return None

    code, data = _post(
        client, _SEARCH_URL,
        {"q_organization_domains": [domain], "person_titles": _PERSON_TITLES, "per_page": 3},
    )
    if code == 403:
        _people_blocked = True
        logger.warning(
            "Apollo People API gated on this plan (403) — contact emails disabled "
            "for this run. Upgrade Apollo + set APOLLO_PEOPLE_ENABLED=true to enable."
        )
        return None
    if code != 200 or not data:
        return None
    people = data.get("people") or data.get("contacts") or []
    if not people:
        return None
    best = sorted(people, key=lambda p: _title_rank(p.get("title", "")))[0]

    # Reveal the verified email via People Match (1 credit).
    code, mdata = _post(client, _MATCH_URL, {
        "first_name": best.get("first_name"),
        "last_name": best.get("last_name"),
        "domain": domain,
        "organization_name": best.get("organization_name") or company_name,
        "reveal_personal_emails": True,
    })
    if code == 403:
        _people_blocked = True
        return None
    person = (mdata or {}).get("person") or best
    email = person.get("email")
    if settings.APOLLO_VERIFIED_ONLY and person.get("email_status") != "verified":
        email = None
    name = person.get("name") or " ".join(
        filter(None, [person.get("first_name"), person.get("last_name")])
    ).strip()
    return {
        "contact_name": name or None,
        "contact_role": person.get("title"),
        "email": email,
        "linkedin_url": person.get("linkedin_url"),
    }


# Senior decision-makers surfaced by the on-demand "Reveal contacts" button.
_SENIOR_TITLES = [
    "CEO", "Founder", "Co-Founder", "CTO", "COO", "CRO", "CMO", "CFO",
    "President", "VP Engineering", "Head of Engineering", "VP Sales",
    "Head of Talent", "Director",
]


def _phone_of(person: dict[str, Any]) -> Optional[str]:
    """Pull the first usable phone number from an Apollo person record."""
    nums = person.get("phone_numbers")
    if isinstance(nums, list) and nums and isinstance(nums[0], dict):
        return nums[0].get("sanitized_number") or nums[0].get("raw_number")
    return None


def _enrich_people(
    client: httpx.Client, domain: str, company_name: str, limit: int
) -> tuple[list[dict[str, Any]], str]:
    """Return ([leaders], status) for a company: CEO/Founder/CTO/… + emails.

    status: "ok" (people found) · "gated" (plan blocks the People API, 403) ·
    "none" (plan allows it but no people matched).
    """
    global _people_blocked
    if _people_blocked:
        return ([], "gated")

    code, data = _post(client, _SEARCH_URL, {
        "q_organization_domains": [domain],
        "person_titles": _SENIOR_TITLES,
        "per_page": max(limit, 5),
    })
    if code == 403:
        _people_blocked = True
        return ([], "gated")
    if code != 200 or not data:
        return ([], "none")
    people = data.get("people") or data.get("contacts") or []
    if not people:
        return ([], "none")

    leaders: list[dict[str, Any]] = []
    for p in sorted(people, key=lambda x: _title_rank(x.get("title", "")))[:limit]:
        # Reveal the verified email via People Match (1 credit each).
        code, mdata = _post(client, _MATCH_URL, {
            "first_name": p.get("first_name"),
            "last_name": p.get("last_name"),
            "domain": domain,
            "organization_name": p.get("organization_name") or company_name,
            "reveal_personal_emails": True,
        })
        if code == 403:
            _people_blocked = True
            person = p
        else:
            person = (mdata or {}).get("person") or p
        email = person.get("email")
        if settings.APOLLO_VERIFIED_ONLY and person.get("email_status") != "verified":
            email = None
        name = person.get("name") or " ".join(
            filter(None, [person.get("first_name"), person.get("last_name")])
        ).strip()
        leaders.append({
            "name": name or None,
            "title": person.get("title"),
            "email": email,
            "phone": _phone_of(person),
            "linkedin_url": person.get("linkedin_url"),
        })
    return (leaders, "ok")


def enrich_on_demand(company_domain: Optional[str], company_name: str) -> dict[str, Any]:
    """One-company lookup for the "Reveal contacts" button: firmographics + leaders.

    Returns {"org": {...}|None, "people": [...], "people_status": str, "domain": str|None}.
    Always tries the People API so it works the instant the plan is upgraded;
    on a gated plan it returns the (free) org profile + people_status="gated".
    """
    if not settings.APOLLO_API_KEY:
        return {"org": None, "people": [], "people_status": "no_key", "domain": None}
    domain = _bare_domain(company_domain)
    if not domain:
        return {"org": None, "people": [], "people_status": "no_domain", "domain": None}
    with httpx.Client(timeout=30.0) as client:
        org = _enrich_org(client, domain)
        people, status = _enrich_people(client, domain, company_name, settings.APOLLO_MAX_PEOPLE)
    return {"org": org, "people": people, "people_status": status, "domain": domain}


def enrich_company(company_domain: str, company_name: str) -> Optional[dict[str, Any]]:
    """Enrich one company (org firmographics + optional decision-maker). None on miss."""
    if not settings.APOLLO_API_KEY:
        return None
    domain = _bare_domain(company_domain)
    if not domain:
        return None
    with httpx.Client(timeout=30.0) as client:
        out: dict[str, Any] = {}
        org = _enrich_org(client, domain)
        if org:
            out.update(org)
        if settings.APOLLO_PEOPLE_ENABLED:
            person = _enrich_person(client, domain, company_name)
            if person:
                out.update(person)
    return out or None


_CLEARBIT_SUGGEST = "https://autocomplete.clearbit.com/v1/companies/suggest"


def _normalize_name(name: str) -> str:
    """Lowercase alphanumerics only — for exact name↔domain comparison."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _resolve_domain(client: httpx.Client, company_name: str) -> Optional[str]:
    """Best-effort company-name → domain via Clearbit's free autocomplete.

    Funding-news leads carry no domain, and Apollo's org endpoint needs one.
    Only an EXACT normalized match is accepted ("Ethereal Machines" ↔
    etherealmachines.com) — close-but-different suggestions ("TrueFan" →
    truefanz.com, a different company) are rejected, because enriching the
    wrong company is worse than not enriching. Returns None on any miss/error.
    """
    target = _normalize_name(company_name)
    if len(target) < 4:  # too short to match safely
        return None
    try:
        resp = client.get(_CLEARBIT_SUGGEST, params={"query": company_name}, timeout=10)
        if resp.status_code != 200:
            return None
        for hit in resp.json() or []:
            domain = (hit.get("domain") or "").lower()
            root = domain.split(".")[0] if domain else ""
            if root and (root == target or _normalize_name(hit.get("name", "")) == target):
                return domain
    except Exception:  # noqa: BLE001 - resolution is strictly best-effort
        return None
    return None


def auto_enrich_orgs(
    companies: list[dict[str, Any]],
    score_fn: Any,
    vertical_config: dict[str, Any],
) -> int:
    """Auto-enrich the TOP-scored companies with the FREE org endpoint only.

    Runs on every pipeline scrape (capped at ``APOLLO_AUTO_ENRICH_TOP``) so the
    best leads carry firmographics — industry, size, website, and HQ city —
    without spending people-search credits. Companies without a domain are
    skipped (the org endpoint needs one). Fills ``location``/``lat``/``lng``
    from Apollo's HQ city when the lead has none, so it pins on the map.
    Returns the number of companies enriched. Fail-open: errors leave
    ``enrichment`` as None and the run continues.
    """
    if not (settings.APOLLO_API_KEY and settings.APOLLO_AUTO_ENRICH):
        return 0
    candidates = [c for c in companies if not c.get("enrichment")]
    if not candidates:
        return 0
    try:
        candidates.sort(key=lambda c: score_fn(c, vertical_config)[0], reverse=True)
    except Exception:  # noqa: BLE001 - ranking is best-effort; order is fine
        pass
    top = candidates[: max(0, settings.APOLLO_AUTO_ENRICH_TOP)]

    from processors.geocoder import geocode_city

    enriched = 0
    resolved = 0
    with httpx.Client(timeout=30.0) as client:
        for company in top:
            domain = _bare_domain(company.get("company_domain") or company.get("website"))
            if not domain:
                # Funding-news leads have no domain; try the free name→domain
                # resolver so Apollo has something to enrich.
                domain = _resolve_domain(client, company.get("company_name") or "")
                if domain:
                    company["company_domain"] = domain
                    resolved += 1
            if not domain:
                continue
            try:
                org = _enrich_org(client, domain)
            except Exception as exc:  # noqa: BLE001 - one miss must not stop the run
                logger.warning("Org enrich failed for %s: %s", domain, exc)
                org = None
            if org:
                company["enrichment"] = org
                enriched += 1
                city = org.get("org_city")
                if city and not company.get("location"):
                    company["location"] = city
                    if company.get("lat") is None or company.get("lng") is None:
                        coords = geocode_city(city)
                        if coords:
                            company["lat"], company["lng"] = coords
            time.sleep(_RATE_LIMIT_SLEEP)
    logger.info(
        "Auto org-enrich: %d/%d top companies enriched, %d domains resolved "
        "(cap %d, free endpoints)",
        enriched, len(top), resolved, settings.APOLLO_AUTO_ENRICH_TOP,
    )
    return enriched


def enrich_all(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich each company in place with an ``enrichment`` dict (or None).

    Org firmographics run on every company with a domain (free); decision-maker
    lookup runs only when ``APOLLO_PEOPLE_ENABLED`` and the plan allows it. The
    number of companies hit is capped at ``APOLLO_MAX_ENRICH`` to protect credits
    and rate limits. With no key, everything gets ``enrichment = None`` and no
    network calls happen — the Apify-only contract.
    """
    global _people_blocked
    if not settings.APOLLO_API_KEY:
        logger.info("APOLLO_API_KEY not set; skipping enrichment for %d companies", len(companies))
        for company in companies:
            company["enrichment"] = None
        return companies

    _people_blocked = False
    cap = settings.APOLLO_MAX_ENRICH
    attempted = enriched = 0
    with httpx.Client(timeout=30.0) as client:
        for company in companies:
            domain = _bare_domain(company.get("company_domain") or company.get("website"))
            if not domain or attempted >= cap:
                company["enrichment"] = None
                continue
            attempted += 1
            out: dict[str, Any] = {}
            org = _enrich_org(client, domain)
            if org:
                out.update(org)
            if settings.APOLLO_PEOPLE_ENABLED:
                person = _enrich_person(client, domain, company.get("company_name", ""))
                if person:
                    out.update(person)
            company["enrichment"] = out or None
            if out:
                enriched += 1
            time.sleep(_RATE_LIMIT_SLEEP)

    logger.info(
        "Apollo: enriched %d/%d companies (cap %d, people=%s)",
        enriched, attempted, cap, settings.APOLLO_PEOPLE_ENABLED,
    )
    return companies
