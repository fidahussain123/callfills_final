"""Facebook/Meta Ad Library scraper backed by apify/facebook-ads-scraper.

Companies running paid recruitment ads on Meta are aggressively hiring — a strong
hiring-intent signal that complements organic job-board posts. We query the Meta
Ad Library for hiring-related terms scoped to the target country and emit
``hiring_ad`` signals.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

from config import settings
from db.models import NormalizedSignal, RawSignal
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper
from signal_types import HIRING_AD

logger = logging.getLogger("lead-intel.scrapers.facebook_ads")

_AD_TERMS = ["hiring", "we are hiring", "join our team", "now hiring engineers"]
_RESULTS_LIMIT = 80


def _ad_library_url(term: str, country_code: str) -> str:
    """Build a Meta Ad Library search URL for a term in a country."""
    return (
        "https://www.facebook.com/ads/library/?"
        "active_status=active&ad_type=all"
        f"&country={country_code.upper()}"
        f"&q={quote(term)}"
        "&search_type=keyword_unordered"
    )


@register_scraper
class FacebookAdsScraper(ApifyScraper):
    """Scrape Meta Ad Library recruitment ads as ``hiring_ad`` signals."""

    key = "facebook_ads"
    source_platform = "facebook"
    actor_id = "apify/facebook-ads-scraper"

    def build_input(self, lookback_days: int) -> dict[str, Any]:
        start_urls = [
            {"url": _ad_library_url(term, settings.TARGET_COUNTRY_CODE)}
            for term in _AD_TERMS
        ]
        return {
            "startUrls": start_urls,
            "resultsLimit": min(_RESULTS_LIMIT, settings.MAX_ITEMS_PER_SOURCE),
            "activeStatus": "active",
        }

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data
        snapshot = item.get("snapshot") or {}
        company_name = (
            item.get("pageName")
            or item.get("page_name")
            or snapshot.get("page_name")
        )
        if not company_name:
            return None

        body = (
            snapshot.get("body", {})
            if isinstance(snapshot.get("body"), dict)
            else {}
        )
        text = (
            body.get("text")
            or item.get("adText")
            or item.get("ad_creative_body")
            or ""
        )
        detected = (
            self.parse_iso(item.get("startDate"))
            or self.parse_iso(item.get("ad_delivery_start_time"))
            or self.parse_epoch(item.get("startDateFormatted"))
            or self.cutoff(0)
        )
        return NormalizedSignal(
            company_name=company_name,
            company_domain=snapshot.get("link_url") or item.get("pageUrl"),
            signal_type=HIRING_AD,
            source_platform=self.source_platform,
            raw_text=(text or "Recruitment ad")[:280],
            source_url=item.get("url") or item.get("snapshotUrl") or item.get("adArchiveID", ""),
            detected_at=detected,
            metadata={
                "page_name": company_name,
                "ad_id": item.get("adArchiveID") or item.get("ad_archive_id"),
                "cta_text": snapshot.get("cta_text"),
                "publisher_platforms": item.get("publisherPlatform")
                or item.get("publisher_platforms"),
                "page_url": item.get("pageUrl"),
            },
        )
