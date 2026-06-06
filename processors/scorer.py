"""Score companies 0–100 using vertical-specific weights.

The scorer is vertical-agnostic: it reads weight keys from
``vertical_config["scoring"]`` and credits each that applies to the company's
signal profile. This lets a new vertical change scoring with config only.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lead-intel.processors.scorer")

_MAX_SCORE = 100


def _funding_recency_bucket(days: int | None) -> str | None:
    """Map funding recency in days to the matching scoring weight key."""
    if days is None:
        return None
    if days <= 30:
        return "funding_recency_0_30_days"
    if days <= 60:
        return "funding_recency_31_60"
    if days <= 90:
        return "funding_recency_61_90"
    return None


def _hiring_recency_bucket(days: int | None) -> str | None:
    """Map hiring-post recency in days to the matching scoring weight key.

    Fresh hiring posts (spec: 10–20 days) are worth more than stale ones.
    """
    if days is None:
        return None
    if days <= 10:
        return "hiring_recency_0_10_days"
    if days <= 20:
        return "hiring_recency_11_20_days"
    return None


def score(company: dict[str, Any], vertical_config: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Score a company and return ``(total, breakdown)``.

    ``breakdown`` maps each credited weight key to the points it contributed, so
    the delivery layer can explain *why* a lead scored the way it did. The total
    is capped at 100.
    """
    weights: dict[str, int] = vertical_config.get("scoring", {})
    breakdown: dict[str, int] = {}

    signals = company.get("signals", [])
    signal_types = {getattr(s, "signal_type", None) for s in signals}
    sources = set(company.get("signal_sources") or [])
    has_hiring = company.get("has_hiring", False)
    has_funding = company.get("has_funding", False)
    hiring_role_count = company.get("hiring_role_count", 0)
    funding_days = company.get("funding_recency_days")
    hiring_days = company.get("hiring_recency_days")
    has_email = bool((company.get("enrichment") or {}).get("email"))

    def credit(key: str) -> None:
        """Add the weight for ``key`` to the breakdown if the vertical defines it."""
        if key in weights:
            breakdown[key] = weights[key]

    # --- Combined hiring + funding (the strongest signal) ---
    if has_hiring and has_funding:
        credit("both_hiring_and_funded")

    # --- Funding recency ---
    bucket = _funding_recency_bucket(funding_days)
    if bucket:
        credit(bucket)

    # --- Hiring recency (fresh posts are worth more) ---
    hiring_bucket = _hiring_recency_bucket(hiring_days)
    if hiring_bucket:
        credit(hiring_bucket)

    # --- Hiring volume ---
    if hiring_role_count >= 5:
        credit("hiring_role_count_5_plus")
    elif 2 <= hiring_role_count <= 4:
        credit("hiring_role_count_2_4")

    # --- Generic per-signal-type credits (signal_<type>) ---
    # Lets a vertical reward any signal type by adding a weight key, e.g.
    # ``signal_hiring_ad`` or ``signal_product_launch`` — no code change here.
    for signal_type in signal_types:
        if signal_type:
            credit(f"signal_{signal_type}")

    # --- Intent signals specific to outsourcing/agency-switch verticals ---
    if "outsource_intent" in signal_types:
        credit("outsource_intent_signal")
    if "agency_switch" in signal_types:
        credit("agency_switch_signal")

    # --- Source-based confidence ---
    if "reddit" in sources:
        credit("signal_from_reddit")
    if "twitter" in sources:
        credit("signal_from_twitter")
    if "wellfound" in sources:
        credit("signal_from_wellfound")

    # --- Enrichment + multi-platform corroboration ---
    if has_email:
        credit("contact_email_found")
    if len(sources) >= 2:
        credit("multi_platform_signal")

    total = min(_MAX_SCORE, sum(breakdown.values()))
    logger.debug("Scored %s -> %d %s", company.get("company_name"), total, breakdown)
    return total, breakdown
