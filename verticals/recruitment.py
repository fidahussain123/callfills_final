"""Recruitment agency vertical configuration."""

from __future__ import annotations

from verticals.base_vertical import BaseVertical, register


@register
class RecruitmentVertical(BaseVertical):
    """Targets actively-hiring, recently-funded engineering teams."""

    name = "recruitment"
    signal_keywords = [
        "software engineer",
        "backend engineer",
        "full stack",
        "frontend",
        "data engineer",
        "ML engineer",
        "devops",
        "SRE",
        "platform engineer",
        "engineering manager",
        "VP engineering",
        "head of engineering",
    ]
    target_roles = [
        "CTO",
        "VP Engineering",
        "Head of Engineering",
        "Co-Founder",
        "Founder",
    ]
    scoring = {
        "both_hiring_and_funded": 40,
        "funding_recency_0_30_days": 30,
        "funding_recency_31_60": 20,
        "funding_recency_61_90": 10,
        "hiring_role_count_5_plus": 20,
        "hiring_role_count_2_4": 10,
        "signal_from_reddit": 15,
        "signal_from_twitter": 10,
        "signal_from_wellfound": 10,
        "contact_email_found": 10,
        "multi_platform_signal": 5,
    }
    sources = [
        "linkedin_jobs",
        "wellfound",
        "indeed",
        "reddit",
        "twitter",
        "rss_feeds",
    ]
    min_score = 60
    filters: dict = {}
