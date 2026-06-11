"""IndiaMART B2B supplier scraper (b2b_suppliers vertical).

Backed by ``thirdwatch/indiamart-supplier-scraper`` ($0.002/result, 99.7%
reliability): a product/category search + optional city → supplier listings
with company name, location, phone and product details. This is the India
B2B moat — the suppliers global databases (Apollo/ZoomInfo) simply don't have.

⚠ Compliance: the vertical is opt-in. We extract business (not personal)
fields only; raw contact lists must not be resold. Field names are mapped
defensively because third-party output schemas drift.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from config import settings
from db.models import NormalizedSignal, RawSignal
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper
from signal_types import SUPPLIER_LISTING

logger = logging.getLogger("lead-intel.scrapers.indiamart")

_DEFAULT_MAX = 25


def _first(item: dict[str, Any], *keys: str) -> Optional[Any]:
    """First non-empty value among ``keys`` (tolerates schema drift)."""
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


@register_scraper
class IndiaMartScraper(ApifyScraper):
    """Scrape IndiaMART suppliers as ``supplier_listing`` leads."""

    key = "indiamart"
    source_platform = "indiamart"
    actor_id = "thirdwatch/indiamart-supplier-scraper"

    def __init__(self) -> None:
        super().__init__()
        self._queries: list[str] = []
        self._location: str = ""
        self._max: int = _DEFAULT_MAX

    def configure(self, cfg: dict[str, Any]) -> None:
        """Per-pipeline targeting: what to search for + which city/state."""
        queries = cfg.get("categories") or cfg.get("keywords") or []
        if queries:
            self._queries = [str(q).strip() for q in queries if str(q).strip()]
        cities = cfg.get("cities") or []
        if cities:
            self._location = str(cities[0]).strip()
        if cfg.get("max_results"):
            try:
                self._max = max(1, int(cfg["max_results"]))
            except (TypeError, ValueError):
                pass

    def build_input(self, lookback_days: int) -> dict[str, Any]:
        return {
            "queries": self._queries or ["packaging machines"],
            "category": "all",
            "location": self._location,
            "maxResultsPerQuery": min(self._max, settings.MAX_ITEMS_PER_SOURCE),
        }

    def fetch(self, lookback_days: int) -> list[RawSignal]:
        if not self._client:
            return []
        items = self.run_actor(self.build_input(lookback_days), max_items=self._max)
        return [RawSignal(source_platform=self.source_platform, data=i) for i in items]

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        # Real actor fields (verified via live smoke run) are snake_case:
        # company_name, product_name, city, state, location, phone, price, moq,
        # supplier_rating, rating_count, gst_number, member_since, product_url,
        # catalog_url. CamelCase fallbacks kept for schema drift.
        item = raw.data
        name = _first(
            item, "company_name", "companyName", "supplierName", "company",
            "sellerName", "seller", "name", "firm",
        )
        if not name or not str(name).strip():
            return None

        product = _first(item, "product_name", "productName", "product", "title")
        location = _first(item, "location") or ", ".join(
            str(p) for p in (_first(item, "city"), _first(item, "state")) if p
        ) or None
        phone = _first(item, "phone", "mobile", "contactNumber", "contact")
        rating = _first(item, "supplier_rating", "rating", "supplierRating")
        reviews = _first(item, "rating_count", "ratingCount", "reviews", "reviewCount")
        url = _first(item, "product_url", "catalog_url", "url", "supplierUrl", "link") or ""
        gst = _first(item, "gst_number", "gst", "gstNumber", "gstin")

        try:
            rating = float(rating) if rating not in (None, "") else None
        except (TypeError, ValueError):
            rating = None
        try:
            reviews = int(reviews) if reviews not in (None, "") else None
        except (TypeError, ValueError):
            reviews = None

        return NormalizedSignal(
            company_name=str(name).strip(),
            company_domain=None,
            signal_type=SUPPLIER_LISTING,
            source_platform=self.source_platform,
            raw_text=" · ".join(str(p) for p in (product, location) if p) or str(name),
            source_url=str(url),
            detected_at=self.cutoff(0),  # a listing has no post date; stamp now
            metadata={
                "location": location,
                "phone": str(phone) if phone else None,
                "category": str(product) if product else None,
                "address": location,
                "rating": rating,
                "review_count": reviews,
                "gst": gst,
                "moq": _first(item, "moq"),
                "price": _first(item, "price"),
                "member_since": _first(item, "member_since"),
                "company_website": _first(item, "website", "companyWebsite"),
            },
        )
