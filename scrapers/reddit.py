"""Reddit scraper backed by the trudax/reddit-scraper-lite actor.

Reddit posts are unstructured, so company names are not known at fetch time —
they are extracted later by the normalizer. Here we only classify the signal
type from the post text and carry the raw text forward.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from db.models import NormalizedSignal, RawSignal
from processors.extractor import detect_signal_type
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper

logger = logging.getLogger("lead-intel.scrapers.reddit")

_SUBREDDITS = [
    "startups",
    "entrepreneur",
    "SaaS",
    "forhire",
    "recruitinghell",
    "cscareerquestions",
    "remotework",
    "learnprogramming",
    "webdev",
    "IndieHackers",
    "smallbusiness",
    "venturecapital",
]

_KEYWORDS = [
    "hiring engineers",
    "looking for developer",
    "need to hire",
    "we just raised",
    "series A",
    "series B",
    "seed round",
    "recruitment agency",
    "staffing agency",
    "outsource development",
    "need a CTO",
    "looking for agency",
    "recommendation for recruiter",
]


@register_scraper
class RedditScraper(ApifyScraper):
    """Scrape Reddit posts/comments and classify intent from text."""

    key = "reddit"
    source_platform = "reddit"
    actor_id = "trudax/reddit-scraper-lite"

    def build_input(self, lookback_days: int) -> dict[str, Any]:
        return {
            "subreddits": _SUBREDDITS,
            "searches": _KEYWORDS,
            "maxItems": 1000,
            "sort": "new",
            "time": "month" if lookback_days <= 31 else "year",
        }

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data

        # Honour the lookback window using created_utc.
        created = self.parse_epoch(item.get("created_utc") or item.get("createdUtc"))
        title = item.get("title", "") or ""
        body = item.get("body", "") or item.get("text", "") or item.get("selftext", "")
        text = f"{title} {body}".strip()
        if not text:
            return None

        return NormalizedSignal(
            # company_name resolved downstream by the normalizer's NER step.
            company_name="",
            company_domain=None,
            signal_type=detect_signal_type(text),
            source_platform=self.source_platform,
            raw_text=text,
            source_url=item.get("url") or item.get("link", ""),
            detected_at=created or self.cutoff(0),
            metadata={
                "subreddit": item.get("subreddit") or item.get("community"),
                "upvotes": item.get("upVotes") or item.get("score"),
                "num_comments": item.get("numberOfComments") or item.get("numComments"),
                "author": item.get("author") or item.get("username"),
                "post_id": item.get("id") or item.get("postId"),
            },
        )
