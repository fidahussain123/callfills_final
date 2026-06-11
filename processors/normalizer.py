"""Normalize raw signals from every scraper into the canonical schema.

The normalizer owns the dispatch table mapping ``source_platform`` to the scraper
that knows how to normalize it, and applies the NLP extraction step for the
free-text sources (Reddit, Twitter, RSS).
"""

from __future__ import annotations

import logging
from datetime import timezone
from typing import Optional

from db.models import NormalizedSignal, RawSignal
from processors.extractor import detect_signal_type, extract_company_name
from scrapers.base import BaseScraper
from scrapers.registry import build_instances
from signal_types import GENERAL_SIGNAL, canonical

logger = logging.getLogger("lead-intel.processors.normalizer")

# Platforms whose company name and signal type must be inferred from free text.
_FREE_TEXT_PLATFORMS = {"reddit", "twitter", "rss", "hackernews"}


def _build_scraper_map() -> dict[str, BaseScraper]:
    """Return a fresh map of platform id -> scraper instance for normalization.

    Built from the scraper registry, so any newly-registered scraper is picked up
    automatically with no edit here. Keyed by ``source_platform`` because that is
    the tag carried on each :class:`RawSignal`.
    """
    return {scraper.source_platform: scraper for scraper in build_instances()}


def _clean_domain(domain: Optional[str]) -> Optional[str]:
    """Lowercase and strip a domain, returning ``None`` for empty values."""
    if not domain:
        return None
    cleaned = domain.strip().lower().removeprefix("www.")
    return cleaned or None


def normalize_all(raw_signals: list[RawSignal]) -> list[NormalizedSignal]:
    """Normalize a flat list of raw signals into canonical signals.

    - Dispatches each raw signal to the matching scraper's ``normalize``.
    - For free-text platforms, fills in company name and (re)classifies type.
    - Cleans domains, ensures UTC datetimes, and drops nameless signals.
    """
    scrapers = _build_scraper_map()
    normalized: list[NormalizedSignal] = []
    sources_seen: set[str] = set()

    for raw in raw_signals:
        scraper = scrapers.get(raw.source_platform)
        if scraper is None:
            logger.debug("No scraper for platform %s; skipping", raw.source_platform)
            continue
        try:
            signal = scraper.normalize(raw)
        except Exception as exc:  # noqa: BLE001 - one bad record shouldn't abort
            logger.error("normalize failed for %s: %s", raw.source_platform, exc)
            continue
        if signal is None:
            continue

        # Free-text sources: resolve company + signal type from the text.
        if signal.source_platform in _FREE_TEXT_PLATFORMS:
            if not signal.company_name:
                extracted = extract_company_name(signal.raw_text, signal.source_url)
                if not extracted:
                    continue  # drop: no resolvable company
                signal.company_name = extracted
            if signal.signal_type in ("", GENERAL_SIGNAL):
                signal.signal_type = detect_signal_type(signal.raw_text)

        if not signal.company_name or not signal.company_name.strip():
            continue

        # Normalize the signal type to its canonical form (maps legacy aliases).
        signal.signal_type = canonical(signal.signal_type)

        signal.company_domain = _clean_domain(signal.company_domain)

        # Ensure detected_at is timezone-aware UTC.
        if signal.detected_at.tzinfo is None:
            signal.detected_at = signal.detected_at.replace(tzinfo=timezone.utc)
        else:
            signal.detected_at = signal.detected_at.astimezone(timezone.utc)

        normalized.append(signal)
        sources_seen.add(signal.source_platform)

    logger.info(
        "Normalized %d signals from %s",
        len(normalized),
        ", ".join(sorted(sources_seen)) or "no sources",
    )
    return normalized
