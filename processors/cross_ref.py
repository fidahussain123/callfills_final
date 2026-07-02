"""Cross-reference signals to find the highest-intent companies.

The core value of the product: companies that appear in BOTH a hiring signal and
a funding signal are the strongest leads. We also surface hiring-only and
funding-only companies (scored lower downstream). This module groups raw signals
per company and produces the aggregate fields the scorer and lead-builder need.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from db.models import NormalizedSignal
from processors.geocoder import geocode_city
from processors.quality import normalize_company_name
from signal_types import SOCIAL_TYPES, is_funding, is_hiring

logger = logging.getLogger("lead-intel.processors.cross_ref")


def _group_key(signal: NormalizedSignal) -> str:
    """Key signals by *normalized* company name.

    Hiring and funding signals for the same company almost never share a domain
    (funding headlines carry none) and arrive in different legal forms
    ("TrueFan" vs "TrueFan Technologies Pvt Ltd"). Normalizing the name to a bare
    core lets them merge — which is what makes the hiring+funding overlap (the
    strongest, highest-scoring lead) actually fire.
    """
    norm = normalize_company_name(signal.company_name)
    if norm:
        return norm
    if signal.company_domain:
        return signal.company_domain.strip().lower()
    return (signal.company_name or "").strip().lower()


def _recency_days(signals: list[NormalizedSignal]) -> Optional[int]:
    """Return days since the most recent of ``signals``, or ``None``."""
    if not signals:
        return None
    now = datetime.now(timezone.utc)
    most_recent = max(s.detected_at for s in signals)
    if most_recent.tzinfo is None:
        most_recent = most_recent.replace(tzinfo=timezone.utc)
    return max(0, (now - most_recent).days)


def _first_meta(signals: list[NormalizedSignal], *keys: str) -> Optional[Any]:
    """Return the first non-empty value found under any of ``keys`` in metadata."""
    for signal in signals:
        for key in keys:
            value = signal.metadata.get(key)
            if value:
                return value
    return None


def find_overlap(signals: list[NormalizedSignal]) -> list[dict[str, Any]]:
    """Group signals by company and summarize hiring/funding/social overlap.

    Returns one dict per company with the fields the scorer needs. Companies are
    returned regardless of whether they overlap; ``has_hiring``/``has_funding``
    let the scorer weight them appropriately.
    """
    groups: dict[str, list[NormalizedSignal]] = defaultdict(list)
    for signal in signals:
        groups[_group_key(signal)].append(signal)

    results: list[dict[str, Any]] = []
    overlap_count = 0
    hiring_only = 0
    funding_only = 0

    for key, group in groups.items():
        hiring = [s for s in group if is_hiring(s.signal_type)]
        funding = [s for s in group if is_funding(s.signal_type)]
        social = [s for s in group if s.signal_type in SOCIAL_TYPES]
        signal_types = {s.signal_type for s in group}
        has_hiring = bool(hiring)
        has_funding = bool(funding)

        # Choose a representative name/domain from the group (most common name).
        domain = next((s.company_domain for s in group if s.company_domain), None)
        name_counts = Counter(s.company_name for s in group if s.company_name)
        name = name_counts.most_common(1)[0][0] if name_counts else key

        if has_hiring and has_funding:
            overlap_count += 1
        elif has_hiring:
            hiring_only += 1
        elif has_funding:
            funding_only += 1

        # Map coords: prefer a source's own lat/lng (Google Maps); otherwise
        # geocode the city string so Wellfound/Crunchbase/RSS leads also pin.
        location = _first_meta(group, "location", "jobLocation")
        lat = _first_meta(group, "lat")
        lng = _first_meta(group, "lng")
        if (lat is None or lng is None) and location:
            coords = geocode_city(location)
            if coords:
                lat, lng = coords

        results.append(
            {
                "company_name": name,
                "company_domain": domain,
                "website": _first_meta(group, "company_website"),
                "location": location,
                "signals": group,
                "has_hiring": has_hiring,
                "has_funding": has_funding,
                "has_social": bool(social),
                "hiring_role_count": len(hiring),
                "social_count": len(social),
                "hiring_recency_days": _recency_days(hiring),
                "funding_recency_days": _recency_days(funding),
                "funding_round": _first_meta(funding, "funding_round"),
                "funding_amount": _first_meta(funding, "funding_amount"),
                # Local-business fields (Google Maps); None for intent signals.
                "phone": _first_meta(group, "phone"),
                "email": _first_meta(group, "email"),
                "rating": _first_meta(group, "rating"),
                "review_count": _first_meta(group, "review_count"),
                "category": _first_meta(group, "category"),
                "address": _first_meta(group, "address"),
                "maps_url": _first_meta(group, "maps_url"),
                # LinkedIn People fields (person leads); None for company leads.
                "followers": _first_meta(group, "followers"),
                "connections": _first_meta(group, "connections"),
                "linkedin_url": _first_meta(group, "linkedin_url"),
                "headline": _first_meta(group, "headline"),
                "lat": lat,
                "lng": lng,
                "signal_types": sorted(signal_types),
                "signal_sources": sorted({s.source_platform for s in group}),
            }
        )

    logger.info(
        "%d companies with hiring+funding overlap, %d hiring only, %d funding only",
        overlap_count,
        hiring_only,
        funding_only,
    )
    return results
