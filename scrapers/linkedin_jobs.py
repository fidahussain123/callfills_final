"""LinkedIn Jobs scraper backed by the curious_coder/linkedin-jobs-scraper actor.

This actor takes one or more *public* LinkedIn jobs search URLs (generated in an
incognito window) plus a result ``count`` and an optional ``scrapeCompany`` flag
that enriches each job with company details (website, employee count, industry).

We build search URLs for engineering roles, full-time, posted in the lookback
window, then keep only companies in the 10–500 employee band — the sweet spot for
actively-scaling startups.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

from config import settings
from db.models import NormalizedSignal, RawSignal
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper
from signal_types import HIRING_POST

logger = logging.getLogger("lead-intel.scrapers.linkedin")

# Engineering role keywords; one search URL is generated per keyword.
_SEARCH_QUERIES = [
    "Software Engineer",
    "Backend Engineer",
    "Full Stack Engineer",
    "Data Engineer",
    "ML Engineer",
    "DevOps Engineer",
    "Platform Engineer",
    "Frontend Engineer",
]

_RESULTS_PER_QUERY = 25


def _date_param(lookback_days: int) -> str:
    """Map a lookback window to LinkedIn's ``f_TPR`` recency filter (seconds)."""
    seconds = max(1, lookback_days) * 86_400
    return f"r{seconds}"


def _build_search_url(keyword: str, lookback_days: int, location: str) -> str:
    """Construct a public LinkedIn jobs search URL for a keyword (full-time)."""
    return (
        "https://www.linkedin.com/jobs/search/?"
        f"keywords={quote(keyword)}"
        f"&location={quote(location)}"
        f"&f_TPR={_date_param(lookback_days)}"
        "&f_JT=F"  # full-time
    )


@register_scraper
class LinkedInJobsScraper(ApifyScraper):
    """Scrape LinkedIn job postings as ``hiring_post`` signals."""

    key = "linkedin_jobs"
    source_platform = "linkedin"
    actor_id = "curious_coder/linkedin-jobs-scraper"

    def build_input(self, lookback_days: int) -> dict[str, Any]:
        """Build the actor input: one search URL per engineering keyword.

        Hiring posts must be fresh, so the window is clamped to the hiring
        lookback (spec: 10–20 days) regardless of the pipeline-wide default.
        """
        window = min(lookback_days, settings.HIRING_LOOKBACK_DAYS)
        urls = [
            _build_search_url(kw, window, settings.TARGET_LOCATION)
            for kw in _SEARCH_QUERIES
        ]
        return {
            "urls": urls,
            "count": _RESULTS_PER_QUERY * len(urls),
            "scrapeCompany": True,
        }

    @staticmethod
    def _employee_count(item: dict[str, Any]) -> Optional[int]:
        """Extract a numeric employee count from the company details."""
        value = item.get("companyEmployeesCount")
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            digits = "".join(ch for ch in value if ch.isdigit())
            if digits:
                return int(digits)
        return None

    @staticmethod
    def _domain_from_website(website: Optional[str]) -> Optional[str]:
        """Reduce a company website URL to a bare domain."""
        if not website:
            return None
        from urllib.parse import urlparse

        host = urlparse(website if "//" in website else f"https://{website}").netloc
        return host.lower().removeprefix("www.") or None

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data
        company_name = item.get("companyName") or item.get("company")
        if not company_name:
            return None

        size = self._employee_count(item)
        lo, hi = settings.LINKEDIN_MIN_EMPLOYEES, settings.LINKEDIN_MAX_EMPLOYEES
        if size is not None and not (lo <= size <= hi):
            logger.debug("Dropping %s — size %s outside %d–%d band", company_name, size, lo, hi)
            return None

        detected = self.parse_iso(item.get("postedAt"))
        if detected is None:
            ts = item.get("postedAtTimestamp")
            if isinstance(ts, (int, float)):
                detected = self.parse_epoch(ts / 1000)

        return NormalizedSignal(
            company_name=company_name,
            company_domain=self._domain_from_website(item.get("companyWebsite")),
            signal_type=HIRING_POST,
            source_platform=self.source_platform,
            raw_text=item.get("title", ""),
            source_url=item.get("link") or item.get("applyUrl", ""),
            detected_at=detected or self.cutoff(0),
            metadata={
                "role_title": item.get("title"),
                "seniority": item.get("seniorityLevel"),
                "employment_type": item.get("employmentType"),
                "job_function": item.get("jobFunction"),
                "industries": item.get("industries"),
                "location": item.get("location"),
                "country": item.get("country"),
                "company_website": item.get("companyWebsite"),
                "ats_url": item.get("applyUrl"),
                "salary": item.get("salary"),
                "applicants_count": item.get("applicantsCount"),
                "workplace_types": item.get("workplaceTypes"),
                "company_size": size,
            },
        )
