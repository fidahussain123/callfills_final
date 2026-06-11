"""Crunchbase funding-rounds scraper (startups vertical).

Backed by an Apify actor (``settings.CRUNCHBASE_ACTOR_ID``, default
``jungle_synthesizer/crunchbase-pro-companies-scraper`` in ``rounds`` mode):
one record per funding round with company, round type, amount, date and
location — the premium "why-now" trigger for the startups vertical.

Costs money per run ($0.10 start + $0.005/record), so it is OFF by default:
it only runs when a vertical/pipeline explicitly lists ``crunchbase`` in its
sources. The actor's field names are mapped defensively (multi-key fallbacks)
because third-party output schemas drift.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config import settings
from db.models import NormalizedSignal, RawSignal
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper
from signal_types import FUNDING_ROUND

logger = logging.getLogger("lead-intel.scrapers.crunchbase")

_MAX_ITEMS = 50


def _first(item: dict[str, Any], *keys: str) -> Optional[Any]:
    """First non-empty value among ``keys`` (tolerates schema drift)."""
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _fmt_amount(value: Any) -> Optional[str]:
    """Format a USD amount like ``$12.5M`` (passes strings through)."""
    if value in (None, "", 0):
        return None
    if isinstance(value, str):
        return value if value.startswith(("$", "₹", "€")) else f"${value}"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n / 1_000:.0f}K"
    return f"${n:.0f}"


@register_scraper
class CrunchbaseScraper(ApifyScraper):
    """Scrape recent funding rounds as ``funding_round`` signals."""

    key = "crunchbase"
    source_platform = "crunchbase"

    def __init__(self) -> None:
        super().__init__()
        self.actor_id = settings.CRUNCHBASE_ACTOR_ID
        self._locations: list[str] = [settings.TARGET_LOCATION]
        self._round_types: list[str] = []
        self._min_amount: int = settings.CRUNCHBASE_MIN_AMOUNT_USD
        self._max: int = _MAX_ITEMS

    def configure(self, cfg: dict[str, Any]) -> None:
        """Per-pipeline targeting: cities/locations, round types, min amount."""
        cities = cfg.get("cities")
        if cities:
            self._locations = [str(c).strip() for c in cities if str(c).strip()]
        if cfg.get("round_types"):
            self._round_types = [str(r).strip() for r in cfg["round_types"]]
        if cfg.get("min_amount_usd") is not None:
            try:
                self._min_amount = int(cfg["min_amount_usd"])
            except (TypeError, ValueError):
                pass
        if cfg.get("max_results"):
            try:
                self._max = max(1, min(int(cfg["max_results"]), _MAX_ITEMS))
            except (TypeError, ValueError):
                pass

    def build_input(self, lookback_days: int) -> dict[str, Any]:
        window = max(lookback_days, settings.FUNDING_LOOKBACK_DAYS)
        announced_after = (
            datetime.now(timezone.utc) - timedelta(days=window)
        ).strftime("%Y-%m-%d")
        run_input: dict[str, Any] = {
            "mode": "rounds",
            "locations": self._locations,
            "announcedAfter": announced_after,
            "minAmountUsd": self._min_amount,
            "maxItems": min(self._max, settings.MAX_ITEMS_PER_SOURCE),
            "proxyConfiguration": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
                "apifyProxyCountry": "US",
            },
            # Required free-text fields in the actor's input schema.
            "sp_intended_usage": "B2B lead generation — surfacing recently funded companies as sales signals.",
            "sp_improvement_suggestions": "n/a",
        }
        if self._round_types:
            run_input["roundTypes"] = self._round_types
        return run_input

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data
        org = item.get("organization") if isinstance(item.get("organization"), dict) else {}
        name = _first(item, "companyName", "organizationName", "orgName", "company", "name") \
            or _first(org, "name", "companyName")
        if not name or not str(name).strip():
            return None

        round_type = _first(item, "roundType", "round_type", "fundingType", "investmentType", "investment_type", "type")
        amount_raw = _first(item, "moneyRaisedUsd", "raisedAmountUsd", "amountUsd", "moneyRaised", "raised_amount_usd", "amount", "money_raised")
        announced = _first(item, "announcedOn", "announced_on", "announcedDate", "announced_at", "date")
        location = _first(item, "location", "companyLocation", "headquarters", "hqLocation", "city") \
            or _first(org, "location", "city")
        website = _first(item, "companyWebsite", "website", "homepage", "domain") \
            or _first(org, "website", "homepage")
        investors = _first(item, "investors", "leadInvestors", "investorNames")
        if isinstance(investors, list):
            investors = ", ".join(str(i.get("name", i)) if isinstance(i, dict) else str(i) for i in investors[:5])

        detected = self.parse_iso(announced) or self.cutoff(0)
        round_label = str(round_type).replace("_", " ").title() if round_type else "Funding round"
        amount_label = _fmt_amount(amount_raw)
        headline = f"{name} raised {amount_label or 'an undisclosed amount'} ({round_label})"

        return NormalizedSignal(
            company_name=str(name).strip(),
            company_domain=str(website) if website else None,
            signal_type=FUNDING_ROUND,
            source_platform=self.source_platform,
            raw_text=headline,
            source_url=_first(item, "crunchbaseUrl", "url", "permalink") or "",
            detected_at=detected,
            metadata={
                "funding_round": round_label,
                "funding_amount": amount_label,
                "amount_usd": amount_raw if isinstance(amount_raw, (int, float)) else None,
                "location": location,
                "company_website": website,
                "investors": investors,
            },
        )
