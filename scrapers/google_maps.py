"""Google Maps scraper backed by the compass/crawler-google-places actor.

Unlike the intent sources (hiring/funding signals), this scraper produces
**local-business leads**: a category + a city → a list of real businesses, each
with a name, phone, website, rating, reviews and address. It powers the
"local_business" vertical — agency lead lists ("barbers in Bangalore").

The search is *targeted* (category + location come from the pipeline's ICP, not a
fixed config), so the pipeline calls :meth:`configure` before fetching to inject
the categories / cities / cap. With no configuration it falls back to sensible
defaults.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from config import settings
from db.models import NormalizedSignal, RawSignal
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper
from signal_types import LOCAL_BUSINESS

logger = logging.getLogger("lead-intel.scrapers.google_maps")

_DEFAULT_MAX = 40       # places per (category × city)
_MAX_CITIES = 2         # cap the city fan-out to keep Apify spend predictable


@register_scraper
class GoogleMapsScraper(ApifyScraper):
    """Scrape Google Maps places as ``local_business`` leads."""

    key = "google_maps"
    source_platform = "google_maps"
    actor_id = "compass/crawler-google-places"

    def __init__(self) -> None:
        super().__init__()
        # Search params — overridden per-pipeline via configure().
        self._categories: list[str] = []
        self._cities: list[str] = (settings.TARGET_CITIES or [settings.TARGET_LOCATION])[:_MAX_CITIES]
        self._max: int = _DEFAULT_MAX
        self._scrape_contacts: bool = False  # set True to also extract emails (costs more)
        # Optional GeoJSON search area (Polygon drawn in the pipeline form).
        # When set, it replaces the city search entirely — the actor only
        # returns places inside the zone.
        self._zone: Optional[dict[str, Any]] = None

    def configure(self, cfg: dict[str, Any]) -> None:
        """Inject search params from a pipeline ICP / vertical filters.

        ``categories`` (or ``keywords`` as an alias) is what to search Google Maps
        for; ``cities`` is where; ``max_results`` caps places per search.
        """
        cats = cfg.get("categories") or cfg.get("keywords") or []
        if cats:
            self._categories = [str(c).strip() for c in cats if str(c).strip()]
        cities = cfg.get("cities")
        if cities:
            self._cities = [str(c).strip() for c in cities if str(c).strip()][:_MAX_CITIES]
        if cfg.get("max_results"):
            try:
                self._max = max(1, int(cfg["max_results"]))
            except (TypeError, ValueError):
                pass
        if cfg.get("scrape_contacts") is not None:
            self._scrape_contacts = bool(cfg.get("scrape_contacts"))
        zone = cfg.get("custom_geolocation")
        if isinstance(zone, dict) and zone.get("type") in ("Polygon", "MultiPolygon", "Point"):
            self._zone = zone

    def _run_input(self, category: str, city: Optional[str]) -> dict[str, Any]:
        """Actor input for one search: a drawn zone (preferred) or a city."""
        run_input: dict[str, Any] = {
            "searchStringsArray": [category],
            "maxCrawledPlacesPerSearch": self._max,
            "language": "en",
            "skipClosedPlaces": True,
        }
        if self._zone:
            # The drawn area IS the search location — city/country must be
            # omitted or the actor would intersect/override the polygon.
            run_input["customGeolocation"] = self._zone
        else:
            run_input["city"] = city
            run_input["countryCode"] = settings.TARGET_COUNTRY_CODE
        if self._scrape_contacts:
            run_input["scrapeContacts"] = True
        return run_input

    def fetch(self, lookback_days: int) -> list[RawSignal]:
        """Run the category × area matrix and merge places (capped).

        With a drawn zone there is exactly one area per category; otherwise we
        fan out across the configured cities.
        """
        if not self._client:
            return []
        categories = self._categories or ["restaurant"]
        areas: list[Optional[str]] = [None] if self._zone else (self._cities or [settings.TARGET_LOCATION])
        raw: list[RawSignal] = []
        for category in categories:
            for city in areas:
                logger.info("Google Maps: '%s' in %s", category, city or "custom drawn zone")
                items = self.run_actor(self._run_input(category, city), max_items=self._max)
                for item in items:
                    raw.append(RawSignal(source_platform=self.source_platform, data=item))
                if len(raw) >= settings.MAX_ITEMS_PER_SOURCE:
                    logger.info("Google Maps hit MAX_ITEMS_PER_SOURCE cap")
                    return raw[: settings.MAX_ITEMS_PER_SOURCE]
        return raw

    @staticmethod
    def _first_email(item: dict[str, Any]) -> Optional[str]:
        """Pull the first contact email if the contacts add-on was enabled."""
        emails = item.get("emails") or item.get("contactDetails", {}).get("emails")
        if isinstance(emails, list) and emails:
            return str(emails[0])
        return None

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data
        name = item.get("title") or item.get("name")
        if not name or not str(name).strip():
            return None

        category = item.get("categoryName")
        if not category:
            cats = item.get("categories")
            category = cats[0] if isinstance(cats, list) and cats else None
        address = item.get("address") or item.get("street")
        city = item.get("city")
        website = item.get("website")
        phone = item.get("phone") or item.get("phoneUnformatted")
        maps_url = item.get("url") or item.get("googleMapsUrl")
        location = item.get("location") or {}

        return NormalizedSignal(
            company_name=str(name).strip(),
            company_domain=None,
            signal_type=LOCAL_BUSINESS,
            source_platform=self.source_platform,
            raw_text=" · ".join(p for p in (category, address) if p) or str(name),
            source_url=maps_url or website or "",
            detected_at=self.cutoff(0),  # a business has no "post date"; stamp now
            metadata={
                "company_website": website,
                "location": city or address,
                "phone": phone,
                "email": self._first_email(item),
                "rating": item.get("totalScore"),
                "review_count": item.get("reviewsCount"),
                "category": category,
                "address": address,
                "maps_url": maps_url,
                "lat": location.get("lat"),
                "lng": location.get("lng"),
                "place_id": item.get("placeId"),
            },
        )
