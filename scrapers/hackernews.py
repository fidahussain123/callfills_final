"""Hacker News intent scraper — free Algolia API, no auth, near-real-time.

Powers the Signals radar: polls ``hn.algolia.com`` (search_by_date) for posts
and comments matching buying-intent queries ("looking for a recruiting agency",
"need to outsource hiring", …). Each hit becomes a free-text NormalizedSignal
on platform ``hackernews``, which the normalizer + Groq AI gate then verify
before anything reaches the feed — the "AI in between" checkpoint.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from db.models import NormalizedSignal, RawSignal
from scrapers.base import BaseScraper
from scrapers.registry import register_scraper
from signal_types import GENERAL_SIGNAL

logger = logging.getLogger("lead-intel.scrapers.hackernews")

_API = "https://hn.algolia.com/api/v1/search_by_date"

# Intent queries — each is one (free) API call per fetch cycle.
_QUERIES = [
    "recruiting agency",
    "staffing agency",
    "outsource hiring",
    "lead generation agency",
    "marketing agency recommend",
]
_MAX_PER_QUERY = 20


def _strip_html(text: str) -> str:
    """Cheap tag/entity cleanup for Algolia comment_text fields."""
    import html
    import re

    return re.sub(r"<[^>]+>", " ", html.unescape(text or "")).strip()


@register_scraper
class HackerNewsScraper(BaseScraper):
    """Poll HN for buying-intent posts/comments (free, keyless)."""

    key = "hackernews"
    source_platform = "hackernews"

    def __init__(self) -> None:
        # Unix timestamp cursor — the radar sets this so each cycle only
        # fetches posts newer than the last one seen.
        self._since: Optional[int] = None

    def configure(self, cfg: dict[str, Any]) -> None:
        """Accept a ``since`` unix-seconds cursor (radar) or custom queries."""
        if cfg.get("since"):
            try:
                self._since = int(cfg["since"])
            except (TypeError, ValueError):
                pass

    def fetch(self, lookback_days: int) -> list[RawSignal]:
        """Fetch new matching posts/comments since the cursor (or lookback)."""
        since = self._since or int(
            (datetime.now(timezone.utc).timestamp()) - lookback_days * 86400
        )
        raw: list[RawSignal] = []
        seen_ids: set[str] = set()
        with httpx.Client(timeout=20.0) as client:
            for query in _QUERIES:
                try:
                    resp = client.get(_API, params={
                        "query": query,
                        "tags": "(story,comment)",
                        "numericFilters": f"created_at_i>{since}",
                        "hitsPerPage": _MAX_PER_QUERY,
                    })
                    resp.raise_for_status()
                    hits = resp.json().get("hits", [])
                except Exception as exc:  # noqa: BLE001 - one query failing is fine
                    logger.error("HN query %r failed: %s", query, exc)
                    continue
                for hit in hits:
                    oid = str(hit.get("objectID") or "")
                    if not oid or oid in seen_ids:
                        continue
                    seen_ids.add(oid)
                    raw.append(RawSignal(source_platform=self.source_platform, data=hit))
        logger.info("HN radar: %d new items across %d queries", len(raw), len(_QUERIES))
        return raw

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        hit = raw.data
        text = _strip_html(
            hit.get("comment_text") or hit.get("story_text") or hit.get("title") or ""
        )
        if len(text) < 30:  # too short to carry real intent
            return None
        detected = self.parse_epoch(hit.get("created_at_i")) or self.cutoff(0)
        oid = hit.get("objectID")
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
        return NormalizedSignal(
            company_name="",  # extracted from text by the normalizer + AI gate
            signal_type=GENERAL_SIGNAL,
            source_platform=self.source_platform,
            raw_text=text[:1200],
            source_url=url,
            detected_at=detected,
            metadata={
                "post_id": oid,
                "author": hit.get("author"),
                "story_title": hit.get("story_title") or hit.get("title"),
                "points": hit.get("points"),
            },
        )
