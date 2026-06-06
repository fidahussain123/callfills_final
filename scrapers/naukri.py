"""Naukri.com scraper backed by the automation-lab/naukri-scraper actor.

Naukri is India's largest job board, so it is the highest-volume hiring source
for the India recruitment vertical. The actor takes a single ``keyword`` and a
single ``location`` (city) per run, so :meth:`fetch` runs a small curated matrix
of (keyword × city) and merges the results, capping total items to keep Apify
spend predictable.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Optional

from config import settings
from db.models import NormalizedSignal, RawSignal
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper
from signal_types import HIRING_POST

logger = logging.getLogger("lead-intel.scrapers.naukri")

# Kept deliberately small: runs = keywords × cities. Each pair is one actor run.
_KEYWORDS = ["software engineer", "backend developer", "full stack developer"]
_MAX_CITIES = 3  # take the first N of settings.TARGET_CITIES
_MAX_JOBS_PER_RUN = 40

_RELATIVE_DAYS_RE = re.compile(r"(\d+)\s*day", re.IGNORECASE)
_RELATIVE_HOURS_RE = re.compile(r"(\d+)\s*hour", re.IGNORECASE)


@register_scraper
class NaukriScraper(ApifyScraper):
    """Scrape Naukri.com postings as ``hiring_post`` signals."""

    key = "naukri"
    source_platform = "naukri"
    actor_id = "automation-lab/naukri-scraper"

    def build_input(self, lookback_days: int) -> dict[str, Any]:  # pragma: no cover
        """Unused for Naukri — :meth:`fetch` builds per-run inputs instead."""
        raise NotImplementedError("NaukriScraper builds input per (keyword, city)")

    def _run_input(self, keyword: str, city: str) -> dict[str, Any]:
        """Actor input for a single keyword/city pair, sorted by date."""
        return {
            "keyword": keyword,
            "location": city,
            "maxJobs": _MAX_JOBS_PER_RUN,
            "workMode": "any",
            "sortBy": "date",
        }

    def fetch(self, lookback_days: int) -> list[RawSignal]:
        """Run the curated keyword×city matrix and merge results (capped)."""
        if not self._client:
            return []
        cities = settings.TARGET_CITIES[:_MAX_CITIES] or [settings.TARGET_LOCATION]
        raw: list[RawSignal] = []
        for keyword in _KEYWORDS:
            for city in cities:
                items = self.run_actor(
                    self._run_input(keyword, city), max_items=_MAX_JOBS_PER_RUN
                )
                for item in items:
                    raw.append(
                        RawSignal(source_platform=self.source_platform, data=item)
                    )
                if len(raw) >= settings.MAX_ITEMS_PER_SOURCE:
                    logger.info("Naukri hit MAX_ITEMS_PER_SOURCE cap")
                    return raw[: settings.MAX_ITEMS_PER_SOURCE]
        return raw

    def _relative_posted(self, value: Any) -> Optional[Any]:
        """Parse Naukri's relative 'N days ago' / 'N hours ago' posted strings."""
        if not value:
            return None
        text = str(value).lower()
        if "just now" in text or "today" in text:
            return self.cutoff(0)
        days = _RELATIVE_DAYS_RE.search(text)
        if days:
            return self.cutoff(0) - timedelta(days=int(days.group(1)))
        hours = _RELATIVE_HOURS_RE.search(text)
        if hours:
            return self.cutoff(0) - timedelta(hours=int(hours.group(1)))
        return None

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data
        company_name = (
            item.get("companyName")
            or item.get("company")
            or item.get("company_name")
        )
        if not company_name:
            return None

        posted = item.get("postedDate") or item.get("posted") or item.get("postedOn")
        detected = (
            self.parse_iso(posted)
            or self._relative_posted(posted)
            or self.cutoff(0)
        )
        role = item.get("title") or item.get("jobTitle") or item.get("designation", "")
        return NormalizedSignal(
            company_name=company_name,
            company_domain=item.get("companyUrl") or item.get("companyDomain"),
            signal_type=HIRING_POST,
            source_platform=self.source_platform,
            raw_text=role,
            source_url=item.get("jobUrl") or item.get("url") or item.get("link", ""),
            detected_at=detected,
            metadata={
                "role_title": role,
                "location": item.get("location") or item.get("jobLocation"),
                "experience": item.get("experience") or item.get("experienceRange"),
                "salary": item.get("salary") or item.get("salaryRange"),
                "skills": item.get("skills") or item.get("tags"),
                "posted_label": posted,
            },
        )
