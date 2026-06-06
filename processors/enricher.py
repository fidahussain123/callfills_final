"""Apollo.io enrichment: resolve a company's decision-maker contact.

Given a company domain we query Apollo's people-search endpoint for senior
engineering/founder titles and return the best contact, prioritising technical
decision-makers. Rate limiting and 429 backoff are handled here.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from config import settings

logger = logging.getLogger("lead-intel.processors.enricher")

_APOLLO_URL = "https://api.apollo.io/api/v1/people/search"
_PERSON_TITLES = [
    "CTO",
    "VP Engineering",
    "Head of Engineering",
    "Co-Founder",
    "Founder",
    "CEO",
]
# Lower index = higher priority when choosing among returned people.
_TITLE_PRIORITY = [
    "cto",
    "vp engineering",
    "head of engineering",
    "co-founder",
    "founder",
    "ceo",
]

_RATE_LIMIT_SLEEP = 0.5
_MAX_RETRIES = 3


def _title_rank(title: str) -> int:
    """Return a sort rank for a person's title (lower is more desirable)."""
    lowered = (title or "").lower()
    for i, keyword in enumerate(_TITLE_PRIORITY):
        if keyword in lowered:
            return i
    return len(_TITLE_PRIORITY)


def enrich_company(company_domain: str, company_name: str) -> Optional[dict[str, Any]]:
    """Look up the best decision-maker contact for a company via Apollo.

    Returns a dict with ``contact_name``, ``contact_role``, ``email`` and
    ``linkedin_url``, or ``None`` when no contact is found or the call fails.
    """
    if not settings.APOLLO_API_KEY:
        logger.warning("APOLLO_API_KEY not set; skipping enrichment")
        return None
    if not company_domain:
        return None

    headers = {
        "X-Api-Key": settings.APOLLO_API_KEY,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }
    body = {
        "q_organization_domains": [company_domain],
        "person_titles": _PERSON_TITLES,
        "per_page": 3,
    }

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(_APOLLO_URL, headers=headers, json=body)
            if resp.status_code == 429:
                wait = _RATE_LIMIT_SLEEP * (2**attempt)
                logger.warning(
                    "Apollo 429 for %s; backing off %.1fs (attempt %d)",
                    company_domain,
                    wait,
                    attempt,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            logger.debug("Apollo response for %s: %s", company_domain, data)

            people = data.get("people") or data.get("contacts") or []
            if not people:
                return None

            best = sorted(people, key=lambda p: _title_rank(p.get("title", "")))[0]
            name = (
                best.get("name")
                or " ".join(
                    filter(None, [best.get("first_name"), best.get("last_name")])
                ).strip()
            )
            return {
                "contact_name": name or None,
                "contact_role": best.get("title"),
                "email": best.get("email"),
                "linkedin_url": best.get("linkedin_url"),
            }
        except httpx.HTTPStatusError as exc:
            logger.error("Apollo HTTP error for %s: %s", company_domain, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("Apollo call failed for %s: %s", company_domain, exc)
            return None

    logger.error("Apollo exhausted retries for %s", company_domain)
    return None


def enrich_all(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich each company dict in place with contact data from Apollo.

    Adds an ``enrichment`` key (the contact dict or ``None``) to every company.
    Sleeps between calls to respect Apollo rate limits.

    When ``APOLLO_API_KEY`` is not configured (Apify-only mode), enrichment is
    skipped entirely — every company gets ``enrichment = None`` with no network
    calls or sleeps — so the pipeline still produces leads without contacts.
    """
    if not settings.APOLLO_API_KEY:
        logger.info("APOLLO_API_KEY not set; skipping enrichment for %d companies", len(companies))
        for company in companies:
            company["enrichment"] = None
        return companies

    enriched_count = 0
    for company in companies:
        domain = company.get("company_domain")
        name = company.get("company_name", "")
        contact = enrich_company(domain, name) if domain else None
        company["enrichment"] = contact
        if contact:
            enriched_count += 1
        time.sleep(_RATE_LIMIT_SLEEP)

    logger.info("Enriched %d/%d companies with contact data", enriched_count, len(companies))
    return companies
