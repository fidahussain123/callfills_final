"""RSS funding-news scraper using feedparser (no Apify, these feeds are free).

We parse a handful of India-focused startup publications plus a Google News
query, keep only recent entries whose titles mention funding, and emit
``funding_round`` signals. The company name is extracted from the headline by the
normalizer's NER step. Funding uses a wider window than hiring because a freshly
funded company is *about to* hire (spec: 2–3 months).
"""

from __future__ import annotations

import logging
import re
import ssl
import urllib.request
from typing import Any, Optional

import feedparser

try:  # Use certifi's CA bundle so feed fetches don't fail on installs (e.g. the
    import certifi  # python.org macOS build) whose default SSL store is empty.
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - certifi ships via deps; degrade gracefully
    _SSL_CONTEXT = ssl.create_default_context()

# Some publications 403 a bare urllib User-Agent; present a browser-like one.
_FEED_UA = "Mozilla/5.0 (compatible; CallfillsBot/1.0; +https://callfills.com)"

from config import settings
from db.models import NormalizedSignal, RawSignal
from processors.extractor import extract_funding_company, extract_hq_city
from scrapers.base import BaseScraper
from scrapers.registry import register_scraper
from signal_types import FUNDING_ROUND

logger = logging.getLogger("lead-intel.scrapers.rss")

# India-focused funding feeds + Google News queries scoped to Indian rounds.
# The per-city queries are the Geo Pack: a headline matched by a city query
# almost always *names* that city, so the HQ-city extractor (regex, then the
# AI-qualify pass) can attach a location — generic queries rarely give one.
_GEO_QUERY_CITIES = [
    "Delhi", "Bengaluru", "Mumbai", "Hyderabad", "Pune", "Chennai",
    "Gurugram", "Noida", "Ahmedabad", "Jaipur", "Kolkata",
]

def _city_feed(city: str) -> str:
    q = f'startup+funding+raised+"{city}"+when:90d'.replace(" ", "+").replace('"', "%22")
    return f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"

_FEEDS = [
    "https://inc42.com/feed/",
    "https://yourstory.com/feed",
    "https://economictimes.indiatimes.com/tech/funding/rssfeeds/82496341.cms",
    "https://entrackr.com/feed",
    "https://news.google.com/rss/search?q=startup+funding+India+raised+when:90d&hl=en-IN&gl=IN&ceid=IN:en",
] + [_city_feed(c) for c in _GEO_QUERY_CITIES]

_FUNDING_KEYWORDS = [
    "raises",
    "funding",
    "series a",
    "series b",
    "seed",
    "million",
    "closes round",
    "secures investment",
    "backed by",
]

_ROUND_PATTERN = re.compile(
    r"\b(seed|pre-seed|series\s+[a-e]|angel)\b", re.IGNORECASE
)
_AMOUNT_PATTERN = re.compile(
    r"(\$|€|£|₹)\s?\d[\d.,]*\s?(million|billion|m|bn|b|k|crore|cr)?",
    re.IGNORECASE,
)


def _read_feed(url: str) -> Any:
    """Fetch a feed over HTTPS with a verified CA bundle, then parse it.

    feedparser's built-in fetching uses Python's default SSL context, which is
    empty on some installs (notably the python.org macOS build) and fails every
    HTTPS feed with ``CERTIFICATE_VERIFY_FAILED``. We fetch the bytes ourselves
    with certifi and hand them to feedparser so funding signals actually flow.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _FEED_UA})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CONTEXT) as resp:
        return feedparser.parse(resp.read())


@register_scraper
class RSSFeedsScraper(BaseScraper):
    """Parse India funding-news RSS feeds into ``funding_round`` signals."""

    key = "rss_feeds"
    source_platform = "rss"

    def fetch(self, lookback_days: int) -> list[RawSignal]:
        # Funding gets a wider window than hiring (freshly funded → about to hire).
        cutoff = self.cutoff(max(lookback_days, settings.FUNDING_LOOKBACK_DAYS))
        raw_signals: list[RawSignal] = []
        for feed_url in _FEEDS:
            try:
                parsed = _read_feed(feed_url)
            except Exception as exc:  # noqa: BLE001 - isolate per-feed failures
                logger.error("Failed to parse feed %s: %s", feed_url, exc)
                continue

            for entry in parsed.entries:
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                lower = title.lower()
                if not any(kw in lower for kw in _FUNDING_KEYWORDS):
                    continue

                published = self.parse_iso(entry.get("published")) or self._struct_time(
                    entry.get("published_parsed")
                )
                if published and published < cutoff:
                    continue

                raw_signals.append(
                    RawSignal(
                        source_platform=self.source_platform,
                        data={
                            "title": title,
                            "summary": entry.get("summary", ""),
                            "link": entry.get("link", ""),
                            "published": entry.get("published", ""),
                            "published_parsed": published.isoformat() if published else None,
                            "feed_source": feed_url,
                        },
                    )
                )
        logger.info("RSS produced %d candidate funding signals", len(raw_signals))
        return raw_signals

    @staticmethod
    def _struct_time(value: Any):
        """Convert a feedparser time.struct_time tuple to a UTC datetime."""
        if not value:
            return None
        try:
            from calendar import timegm
            from datetime import datetime, timezone

            return datetime.fromtimestamp(timegm(value), tz=timezone.utc)
        except Exception:  # noqa: BLE001
            return None

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data
        title = item.get("title", "")
        if not title:
            return None

        # Resolve the funded company from the headline structure. Drop headlines
        # that aren't about a single company raising (market round-ups, etc.).
        company = extract_funding_company(title)
        if not company:
            return None

        summary = item.get("summary", "")
        round_match = _ROUND_PATTERN.search(title)
        amount_match = _AMOUNT_PATTERN.search(title)
        published = self.parse_iso(item.get("published_parsed")) or self.parse_iso(
            item.get("published")
        )
        # HQ city (from "Bengaluru-based"/"headquartered in …") so funding leads
        # can be plotted on the map; None when the article doesn't state it.
        hq_city = extract_hq_city(f"{title} {summary}")

        return NormalizedSignal(
            company_name=company,
            company_domain=None,
            signal_type=FUNDING_ROUND,
            source_platform=self.source_platform,
            raw_text=f"{title} {summary}".strip(),
            source_url=item.get("link", ""),
            detected_at=published or self.cutoff(0),
            metadata={
                "headline": title,
                "location": hq_city,
                "feed_source": item.get("feed_source"),
                "funding_round": round_match.group(0) if round_match else None,
                "funding_amount": amount_match.group(0) if amount_match else None,
            },
        )
