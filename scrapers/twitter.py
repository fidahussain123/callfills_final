"""Twitter/X scraper backed by the apidojo/tweet-scraper actor.

Like Reddit, tweets are free-text; company names are extracted downstream. We
classify intent from the tweet text and drop low-engagement noise (<5 likes).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from db.models import NormalizedSignal, RawSignal
from processors.extractor import detect_signal_type
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper

logger = logging.getLogger("lead-intel.scrapers.twitter")

_SEARCH_QUERIES = [
    '"hiring engineers" lang:en -is:retweet',
    '"we\'re hiring" software engineer lang:en -is:retweet',
    '"just raised" series lang:en -is:retweet',
    '"seed round" hiring lang:en -is:retweet',
    '"looking for recruitment agency" lang:en -is:retweet',
    '"need backend developer" lang:en -is:retweet',
    '"outsource development" lang:en -is:retweet',
    '"switched from" agency lang:en -is:retweet',
    '"frustrated with" agency lang:en -is:retweet',
]

_MIN_LIKES = 5


@register_scraper
class TwitterScraper(ApifyScraper):
    """Scrape tweets matching intent queries and classify them."""

    key = "twitter"
    source_platform = "twitter"
    actor_id = "apidojo/tweet-scraper"

    def build_input(self, lookback_days: int) -> dict[str, Any]:
        return {
            "searchTerms": _SEARCH_QUERIES,
            "maxItems": 1000,
            "sort": "Latest",
            "tweetLanguage": "en",
        }

    @staticmethod
    def _likes(item: dict[str, Any]) -> int:
        for key in ("likeCount", "favorite_count", "likes"):
            value = item.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        item = raw.data

        likes = self._likes(item)
        if likes < _MIN_LIKES:
            return None

        text = item.get("text") or item.get("full_text") or ""
        if not text:
            return None

        created = self.parse_iso(item.get("createdAt")) or self.parse_iso(
            item.get("created_at")
        )
        author = item.get("author") or {}
        handle = (
            item.get("username")
            or (author.get("userName") if isinstance(author, dict) else None)
            or item.get("authorHandle")
        )
        followers = (
            author.get("followers") if isinstance(author, dict) else None
        ) or item.get("authorFollowers")

        return NormalizedSignal(
            company_name="",
            company_domain=None,
            signal_type=detect_signal_type(text),
            source_platform=self.source_platform,
            raw_text=text,
            source_url=item.get("url") or item.get("twitterUrl", ""),
            detected_at=created or self.cutoff(0),
            metadata={
                "tweet_id": item.get("id") or item.get("id_str"),
                "author_handle": handle,
                "likes": likes,
                "retweets": item.get("retweetCount") or item.get("retweet_count"),
                "author_followers": followers,
            },
        )
