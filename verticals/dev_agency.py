"""Dev agency vertical configuration (expandable placeholder).

Targets companies signalling they need outside engineering help: outsourcing
intent, dissatisfaction with a current agency, or recent funding without an
in-house team.
"""

from __future__ import annotations

from verticals.base_vertical import BaseVertical, register


@register
class DevAgencyVertical(BaseVertical):
    """Targets outsourcing/agency-switch intent among funded startups."""

    name = "dev_agency"
    signal_keywords = [
        "MVP",
        "rebuild",
        "tech debt",
        "contractor",
        "outsource development",
        "need developers",
        "looking for dev agency",
        "failed build",
        "rewrite",
        "legacy code",
        "no technical co-founder",
    ]
    target_roles = ["Founder", "CEO", "CTO", "Head of Product"]
    scoring = {
        "outsource_intent_signal": 40,
        "agency_switch_signal": 35,
        "funding_recency_0_30_days": 25,
        "signal_from_reddit": 20,
        "contact_email_found": 10,
    }
    sources = ["reddit", "twitter", "rss_feeds", "wellfound"]
    min_score = 55
    filters: dict = {}
