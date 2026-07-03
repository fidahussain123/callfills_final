"""One-off: fetch every working source, qualify, and export last-30-day leads.

Pulls from each India-wide source that can actually run right now (funding RSS,
Wellfound, Indeed, Hacker News), runs the same normalize -> dedup -> cross-ref
-> AI-qualify -> score pipeline the app uses, keeps only leads whose newest
signal is within the lookback window, and writes a flat CSV for hand-off.

Skipped (and why): google_maps / indiamart need a category+city to mean
anything; reddit / twitter / facebook need API keys (unset); crunchbase is
Cloudflare-blocked; linkedin / naukri are benched as slow + costly.

Run:  python3 scripts/export_recent_leads.py
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# Make the project root importable when run as `python3 scripts/...`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
for _n in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("export")

import scrapers.registry as reg
from processors import ai_qualify, cross_ref, deduplicator, lead_builder, normalizer, scorer
from processors.geocoder import canonical_city
from verticals.base_vertical import get_vertical_config

WORKING_SOURCES = ["rss_feeds", "wellfound", "indeed", "hackernews"]
LOOKBACK_DAYS = 30
OUT_PATH = "/Users/fidahussainsp/Desktop/callfills_leads_last_30days.csv"

# Generic B2B ICP for the qualify pass (mixed sources, no single vertical).
_QUALIFY_CFG = {
    "name": "mixed",
    "display_name": "B2B leads (all sources)",
    "description": (
        "Indian companies showing a recent buying signal — funded startups, "
        "actively hiring firms, and growing businesses — sold to recruiters, "
        "dev agencies, SaaS vendors, and service providers."
    ),
}


def _infer_vertical(company: dict[str, Any]) -> str:
    types = set(company.get("signal_types") or [])
    if {"local_business", "supplier_listing"} & types:
        return "local_business"
    if company.get("has_funding"):
        return "startups"
    if company.get("has_hiring"):
        return "recruitment"
    return "startups"


def _latest_signal(card: dict[str, Any]) -> str:
    dates = [s.get("detected_at") or "" for s in (card.get("signals") or [])]
    return max(dates) if dates else ""


def _first_url(card: dict[str, Any]) -> str:
    for s in card.get("signals") or []:
        if s.get("source_url"):
            return s["source_url"]
    return ""


async def _fetch_all() -> list:
    reg.load_all()
    scrapers = reg.build_instances(WORKING_SOURCES)
    log.info("Fetching %d sources (lookback %dd)...", len(scrapers), LOOKBACK_DAYS)

    async def _one(scraper):
        name = getattr(scraper, "source_platform", type(scraper).__name__)
        try:
            res = await asyncio.to_thread(scraper.fetch, LOOKBACK_DAYS)
            log.info("  %-12s -> %d raw signals", name, len(res))
            return res
        except Exception as exc:  # noqa: BLE001
            log.warning("  %-12s FAILED: %s", name, exc)
            return []

    lists = await asyncio.gather(*(_one(s) for s in scrapers))
    return [sig for sub in lists for sig in sub]


async def main() -> None:
    raw = await _fetch_all()
    log.info("Total raw signals: %d", len(raw))

    normalized = normalizer.normalize_all(raw)
    deduped = deduplicator.filter_duplicates(normalized)
    companies = cross_ref.find_overlap(deduped)
    log.info("Normalized %d -> deduped %d -> %d companies", len(normalized), len(deduped), len(companies))

    companies = await ai_qualify.qualify_companies(companies, _QUALIFY_CFG)
    log.info("After AI qualify: %d companies", len(companies))

    # Build cards grouped by inferred vertical so labels + scoring are right.
    groups: dict[str, list] = {}
    for c in companies:
        groups.setdefault(_infer_vertical(c), []).append(c)
    cards: list[dict[str, Any]] = []
    for vertical, group in groups.items():
        cfg = get_vertical_config(vertical)
        cards.extend(lead_builder.build_lead_cards(group, scorer.score, cfg))

    # Keep only leads whose newest signal is within the window.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    recent = [c for c in cards if _latest_signal(c) >= cutoff]
    recent.sort(key=lambda c: _latest_signal(c), reverse=True)
    log.info("Leads within %dd: %d of %d", LOOKBACK_DAYS, len(recent), len(cards))

    cols = [
        "company_name", "vertical", "location", "city", "website", "company_domain",
        "industry", "employee_count", "phone", "email", "rating", "review_count",
        "category", "funding_round", "funding_amount", "has_hiring", "has_funding",
        "signal_sources", "signal_types", "latest_signal_date", "source_url",
        "ai_summary", "maps_url",
    ]
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for c in recent:
            row = dict(c)
            row["city"] = canonical_city(c.get("location") or "") or ""
            row["signal_sources"] = ", ".join(c.get("signal_sources") or [])
            row["signal_types"] = ", ".join(c.get("signal_types") or [])
            row["latest_signal_date"] = (_latest_signal(c) or "")[:10]
            row["source_url"] = _first_url(c)
            w.writerow(row)

    located = sum(1 for c in recent if c.get("location"))
    log.info("WROTE %d rows -> %s (%d located)", len(recent), OUT_PATH, located)


if __name__ == "__main__":
    asyncio.run(main())
