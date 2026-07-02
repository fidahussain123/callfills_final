"""Build rich Lead Cards from scored, cross-referenced company records.

A Lead Card is the product's user-facing unit: a company plus every signal behind
it (each with a date, source/type, link, and snippet), the score and its
breakdown, a plain-English summary, and optional contact enrichment. The dashboard
renders these and the delivery layer sends them.
"""

from __future__ import annotations

import logging
from typing import Any

from db.models import LeadCard, SignalCard
from processors.quality import quality_reject_reason
from signal_types import (
    FUNDING_ROUND,
    HIRING_AD,
    HIRING_POST,
    SOCIAL_MENTION,
)

logger = logging.getLogger("lead-intel.processors.lead_builder")

# Human-friendly labels for signal types (used in summaries + UI).
_TYPE_LABELS = {
    HIRING_POST: "hiring post",
    HIRING_AD: "recruitment ad",
    FUNDING_ROUND: "funding round",
    SOCIAL_MENTION: "social mention",
}


def _snippet(text: str, limit: int = 240) -> str:
    """Trim signal text to a short display snippet."""
    text = (text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _signal_cards(signals: list[Any]) -> list[SignalCard]:
    """Convert NormalizedSignal objects into display SignalCards, newest first."""
    cards: list[SignalCard] = []
    for sig in signals:
        detected = getattr(sig, "detected_at", None)
        cards.append(
            SignalCard(
                signal_type=getattr(sig, "signal_type", ""),
                source_platform=getattr(sig, "source_platform", ""),
                detected_at=detected.isoformat() if detected else None,
                source_url=getattr(sig, "source_url", None) or None,
                snippet=_snippet(getattr(sig, "raw_text", "")),
                metadata=getattr(sig, "metadata", {}) or {},
            )
        )
    cards.sort(key=lambda c: c.detected_at or "", reverse=True)
    return cards


def _summary(company: dict[str, Any]) -> str:
    """One-line, plain-English reason this company surfaced as a lead."""
    # Local business (Google Maps): describe the business, not intent.
    if company.get("category") or "local_business" in (company.get("signal_types") or []):
        piece = company.get("category") or "Local business"
        rating = company.get("rating")
        if rating:
            piece += f" · {rating}★"
            reviews = company.get("review_count")
            if reviews:
                piece += f" ({reviews} reviews)"
        if not company.get("website"):
            piece += " · no website"
        return piece

    parts: list[str] = []
    if company.get("has_hiring"):
        count = company.get("hiring_role_count", 0)
        fresh = company.get("hiring_recency_days")
        piece = f"Hiring {count} role(s)"
        if fresh is not None:
            piece += f" ({fresh}d ago)"
        parts.append(piece)
    if company.get("has_funding"):
        rnd = company.get("funding_round")
        amount = company.get("funding_amount")
        days = company.get("funding_recency_days")
        piece = "Funded"
        if rnd:
            piece = f"{rnd}"
        if amount:
            piece += f" {amount}"
        if days is not None:
            piece += f" ({days}d ago)"
        parts.append(piece)
    if company.get("has_social"):
        parts.append(f"{company.get('social_count', 0)} social mention(s)")
    return " · ".join(parts) or "Intent signal detected"


def build_lead_card(
    company: dict[str, Any],
    score: int,
    breakdown: dict[str, Any],
    vertical: str,
    min_score: int,
) -> LeadCard:
    """Assemble a :class:`LeadCard` from a cross-referenced company record."""
    enrichment = company.get("enrichment") or {}
    company_row = company.get("company_row") or {}

    return LeadCard(
        company_name=company.get("company_name", ""),
        company_domain=company.get("company_domain"),
        website=company.get("website") or company_row.get("domain") or enrichment.get("org_website"),
        location=company.get("location"),
        industry=company_row.get("industry") or enrichment.get("org_industry"),
        employee_count=company_row.get("employee_count") or enrichment.get("org_employees"),
        vertical=vertical,
        score=score,
        score_breakdown=breakdown,
        meets_threshold=score >= min_score,
        summary=_summary(company),
        ai_summary=company.get("ai_summary"),
        has_hiring=company.get("has_hiring", False),
        has_funding=company.get("has_funding", False),
        has_social=company.get("has_social", False),
        hiring_role_count=company.get("hiring_role_count", 0),
        hiring_recency_days=company.get("hiring_recency_days"),
        funding_recency_days=company.get("funding_recency_days"),
        funding_round=company.get("funding_round"),
        funding_amount=company.get("funding_amount"),
        signal_sources=company.get("signal_sources", []),
        signal_types=company.get("signal_types", []),
        signals=_signal_cards(company.get("signals", [])),
        contact_name=enrichment.get("contact_name"),
        contact_role=enrichment.get("contact_role"),
        email=enrichment.get("email") or company.get("email"),
        linkedin_url=enrichment.get("linkedin_url") or enrichment.get("org_linkedin") or company.get("linkedin_url"),
        phone=company.get("phone") or enrichment.get("org_phone"),
        rating=company.get("rating"),
        review_count=company.get("review_count"),
        category=company.get("category") or company.get("headline"),
        address=company.get("address"),
        maps_url=company.get("maps_url"),
        lat=company.get("lat"),
        lng=company.get("lng"),
        followers=company.get("followers"),
        status="new",
    )


def build_lead_cards(
    companies: list[dict[str, Any]],
    scorer_fn,
    vertical_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Score every company and return serializable lead-card dicts, best first.

    ``scorer_fn`` is ``processors.scorer.score`` (injected to avoid a hard import
    cycle). The returned list is plain dicts ready for JSON/Supabase persistence.
    """
    vertical = vertical_config.get("name", "recruitment")
    min_score = vertical_config.get("min_score", 60)
    freshness_days = vertical_config.get("freshness_days", 30)
    cards: list[dict[str, Any]] = []
    rejected = 0
    for company in companies:
        # Directory listings (Google Maps, IndiaMART) are real businesses, not
        # time-bound posts — the staleness/mega-corp gate doesn't apply to them.
        is_directory = bool(
            {"local_business", "supplier_listing", "linkedin_profile"} & set(company.get("signal_types") or [])
        )
        # Quality gate: skip mega-corps, staffing firms, and stale-only signals.
        if not is_directory and quality_reject_reason(company, max_hiring_days=freshness_days):
            rejected += 1
            continue
        score, breakdown = scorer_fn(company, vertical_config)
        card = build_lead_card(company, score, breakdown, vertical, min_score)
        cards.append(card.model_dump())
    cards.sort(key=lambda c: c["score"], reverse=True)
    logger.info(
        "Built %d lead cards (%d meet threshold %d); %d rejected by quality gates",
        len(cards),
        sum(1 for c in cards if c["meets_threshold"]),
        min_score,
        rejected,
    )
    return cards
