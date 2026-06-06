"""Indeed scraper backed by the valig/indeed-jobs-scraper actor.

This actor scopes results to a single ``country`` (ISO code, e.g. ``in`` for
India) and a free-text ``title``/``location``, with a ``datePosted`` enum that
maps to a recency window. We run one query per engineering title and keep only
fresh (≤14 day) postings to honour the hiring-recency spec.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from config import settings
from db.models import NormalizedSignal, RawSignal
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper
from signal_types import HIRING_POST

logger = logging.getLogger("lead-intel.scrapers.indeed")

_TITLES = ["software engineer", "backend developer", "full stack developer"]
_LIMIT_PER_TITLE = 50


def _date_posted_enum(lookback_days: int) -> str:
    """Map a lookback window to the actor's ``datePosted`` enum ("1"/"3"/"7"/"14")."""
    window = min(lookback_days, settings.HIRING_LOOKBACK_DAYS)
    for bound in ("1", "3", "7", "14"):
        if window <= int(bound):
            return bound
    return "14"


@register_scraper
class IndeedScraper(ApifyScraper):
    """Scrape Indeed (India) full-time postings as ``hiring_post`` signals."""

    key = "indeed"
    source_platform = "indeed"
    actor_id = "valig/indeed-jobs-scraper"

    def build_input(self, lookback_days: int) -> dict[str, Any]:  # pragma: no cover
        """Unused — :meth:`fetch` issues one run per title."""
        raise NotImplementedError("IndeedScraper builds input per title")

    def fetch(self, lookback_days: int) -> list[RawSignal]:
        """Run one search per engineering title and merge results (capped)."""
        if not self._client:
            return []
        date_posted = _date_posted_enum(lookback_days)
        raw: list[RawSignal] = []
        for title in _TITLES:
            run_input = {
                # country (ISO-2) already scopes results to India; a coarse
                # "India" location string returns nothing on Indeed, so omit it.
                "country": settings.TARGET_COUNTRY_CODE,
                "title": title,
                "limit": _LIMIT_PER_TITLE,
                "datePosted": date_posted,
            }
            for item in self.run_actor(run_input, max_items=_LIMIT_PER_TITLE):
                raw.append(RawSignal(source_platform=self.source_platform, data=item))
            if len(raw) >= settings.MAX_ITEMS_PER_SOURCE:
                return raw[: settings.MAX_ITEMS_PER_SOURCE]
        return raw

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data
        company_name = item.get("company") or item.get("companyName")
        if not company_name:
            return None

        description = item.get("description") or item.get("jobDescription") or ""
        snippet = description[:280]
        detected = (
            self.parse_iso(item.get("postedAt"))
            or self.parse_iso(item.get("date"))
            or self.parse_iso(item.get("postingDateParsed"))
            or self.cutoff(0)
        )
        role = item.get("positionName") or item.get("title", "")
        return NormalizedSignal(
            company_name=company_name,
            company_domain=item.get("companyDomain"),
            signal_type=HIRING_POST,
            source_platform=self.source_platform,
            raw_text=role,
            source_url=item.get("url") or item.get("jobUrl", ""),
            detected_at=detected,
            metadata={
                "role_title": role,
                "location": item.get("location"),
                "salary": item.get("salary"),
                "job_type": item.get("jobType"),
                "rating": item.get("rating"),
                "job_description_snippet": snippet,
            },
        )
