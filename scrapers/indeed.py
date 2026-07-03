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


def _domain(url: Optional[str]) -> Optional[str]:
    """Bare domain from a website URL (``https://www.x.com/a`` → ``x.com``)."""
    if not url or not str(url).strip():
        return None
    from urllib.parse import urlparse

    netloc = urlparse(str(url).strip()).netloc or str(url).strip()
    netloc = netloc.split("/")[0].lower()
    return netloc[4:] if netloc.startswith("www.") else (netloc or None)


@register_scraper
class IndeedScraper(ApifyScraper):
    """Scrape Indeed (India) full-time postings as ``hiring_post`` signals."""

    key = "indeed"
    source_platform = "indeed"
    actor_id = "valig/indeed-jobs-scraper"

    def __init__(self) -> None:
        super().__init__()
        self._titles: list[str] = list(_TITLES)
        self._location: Optional[str] = None
        self._max: Optional[int] = None

    def configure(self, cfg: dict[str, Any]) -> None:
        """Inject per-pipeline search params: keywords → job titles, city, cap."""
        kw = cfg.get("keywords") or cfg.get("categories") or cfg.get("job_titles")
        if kw:
            titles = [str(k).strip() for k in kw if str(k).strip()]
            if titles:
                self._titles = titles
        cities = cfg.get("cities") or []
        if cities and str(cities[0]).strip():
            self._location = str(cities[0]).strip()
        if cfg.get("max_results"):
            try:
                self._max = max(1, int(cfg["max_results"]))
            except (TypeError, ValueError):
                pass

    def build_input(self, lookback_days: int) -> dict[str, Any]:  # pragma: no cover
        """Unused — :meth:`fetch` issues one run per title."""
        raise NotImplementedError("IndeedScraper builds input per title")

    def fetch(self, lookback_days: int) -> list[RawSignal]:
        """Run one search per job title and merge results (capped)."""
        if not self._client:
            return []
        date_posted = _date_posted_enum(lookback_days)
        cap = min(self._max or settings.MAX_ITEMS_PER_SOURCE, settings.MAX_ITEMS_PER_SOURCE)
        raw: list[RawSignal] = []
        for title in self._titles:
            run_input = {
                # country (ISO-2) scopes results; a specific city (from the
                # pipeline's Cities field) narrows further. A coarse "India"
                # location string returns nothing on Indeed, so never send it.
                "country": settings.TARGET_COUNTRY_CODE,
                "title": title,
                "limit": min(_LIMIT_PER_TITLE, cap),
                "datePosted": date_posted,
            }
            if self._location and self._location.strip().lower() != "india":
                run_input["location"] = self._location
            for item in self.run_actor(run_input, max_items=min(_LIMIT_PER_TITLE, cap)):
                raw.append(RawSignal(source_platform=self.source_platform, data=item))
            if len(raw) >= cap:
                return raw[:cap]
        return raw

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data
        # The valig actor nests firmographics under ``employer``; older shapes
        # used flat ``company``/``companyName``. Support both.
        employer = item.get("employer") if isinstance(item.get("employer"), dict) else {}
        company_name = item.get("company") or item.get("companyName") or employer.get("name")
        if not company_name:
            return None

        description = item.get("description")
        if isinstance(description, dict):
            description = description.get("text") or ""
        description = description or item.get("jobDescription") or ""
        snippet = description[:280]
        detected = (
            self.parse_iso(item.get("postedAt"))
            or self.parse_iso(item.get("date"))
            or self.parse_iso(item.get("postingDateParsed"))
            or self.parse_iso(item.get("datePublished"))
            or self.parse_iso(item.get("dateOnIndeed"))
            or self.cutoff(0)
        )
        location = item.get("location")
        if isinstance(location, dict):
            location = location.get("city") or location.get("countryName")
        role = item.get("positionName") or item.get("title", "")
        return NormalizedSignal(
            company_name=company_name,
            company_domain=item.get("companyDomain") or _domain(employer.get("corporateWebsite")),
            signal_type=HIRING_POST,
            source_platform=self.source_platform,
            raw_text=role,
            source_url=item.get("url") or item.get("jobUrl", ""),
            detected_at=detected,
            metadata={
                "role_title": role,
                "location": location,
                "salary": item.get("salary"),
                "job_type": item.get("jobType"),
                "rating": item.get("rating") or employer.get("ratingsValue"),
                "industry": employer.get("industry") or None,
                "job_description_snippet": snippet,
            },
        )
