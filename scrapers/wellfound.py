"""Wellfound (AngelList Talent) scraper backed by crawlerbros/wellfound-scraper.

Wellfound listings already carry the startup's funding stage, so each posting is
a combined hiring + VC-context signal: we emit a ``hiring_post`` and stash the
funding stage in metadata so the scorer can credit venture-backed companies.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from config import settings
from db.models import NormalizedSignal, RawSignal
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper
from signal_types import HIRING_POST

logger = logging.getLogger("lead-intel.scrapers.wellfound")

_KEYWORD = "engineer"
_MAX_ITEMS = 60


@register_scraper
class WellfoundScraper(ApifyScraper):
    """Scrape Wellfound startup job posts as VC-context hiring signals."""

    key = "wellfound"
    source_platform = "wellfound"
    actor_id = "crawlerbros/wellfound-scraper"

    def build_input(self, lookback_days: int) -> dict[str, Any]:
        """Build input: engineering roles in the target location, newest first."""
        # startUrls are plain strings per the actor schema; the `location` field
        # is a client-side substring filter that "India" never matches (jobs list
        # cities or "Remote"), so we omit it rather than zero out all results.
        return {
            "startUrls": ["https://wellfound.com/jobs"],
            "keyword": _KEYWORD,
            "jobType": "full-time",
            "sort": "newest",
            "includeNoSalary": True,
            "maxItems": min(_MAX_ITEMS, settings.MAX_ITEMS_PER_SOURCE),
        }

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data
        company_name = (
            item.get("companyName")
            or item.get("startupName")
            or item.get("company")
        )
        if not company_name:
            return None

        detected = (
            self.parse_iso(item.get("postedAt"))
            or self.parse_iso(item.get("createdAt"))
            or self.parse_iso(item.get("publishedAt"))
            or self.cutoff(0)
        )
        role = item.get("title") or item.get("roleTitle", "")
        return NormalizedSignal(
            company_name=company_name,
            company_domain=item.get("companyWebsite") or item.get("companyDomain"),
            signal_type=HIRING_POST,
            source_platform=self.source_platform,
            raw_text=role,
            source_url=item.get("jobUrl") or item.get("url", ""),
            detected_at=detected,
            metadata={
                "role_title": role,
                "funding_stage": item.get("stage")
                or item.get("fundingStage")
                or item.get("companyStage"),
                "team_size": item.get("companySize") or item.get("teamSize"),
                "location": item.get("location"),
                "remote": item.get("remote"),
                "salary_range": item.get("salary") or item.get("salaryRange"),
                "company_website": item.get("companyWebsite"),
            },
        )
