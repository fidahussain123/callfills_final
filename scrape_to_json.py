"""Apify-only runner: scrape (or re-process) sources and write Lead Cards to JSON.

Modes
-----
* **live**    — run the full pipeline against Apify for the chosen vertical's
                sources (``asyncio.run(run_pipeline(...))``).
* **offline** — re-process a previously saved raw dataset
                (``data/linkedin_jobs_raw.json``) through the same
                normalize → cross-ref → enrich → build-card → score flow,
                without calling Apify. Auto-selected when no token is configured.

Usage
-----
    python scrape_to_json.py                 # live if token set, else offline
    python scrape_to_json.py --offline       # force offline from saved JSON
    python scrape_to_json.py --lookback 20   # lookback window for live scrape
    python scrape_to_json.py --vertical recruitment
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

from config import settings  # noqa: E402  (must load env first)
from db import json_store, store  # noqa: E402
from db.models import RawSignal  # noqa: E402
from processors import cross_ref, enricher, lead_builder, normalizer, scorer  # noqa: E402
from verticals.base_vertical import get_vertical_config  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lead-intel.scrape_to_json")

_RAW_FILE = "linkedin_jobs_raw.json"


def _load_offline_raw() -> list[RawSignal]:
    """Load saved raw LinkedIn items into RawSignal objects for normalization."""
    payload = json_store.read_json(_RAW_FILE)
    items = payload.get("items", [])
    logger.info("Offline mode: loaded %d raw items from %s", len(items), _RAW_FILE)
    return [RawSignal(source_platform="linkedin", data=item) for item in items]


def _run_offline(vertical: str) -> dict:
    """Re-process saved raw data through the full lead-card flow."""
    raw_signals = _load_offline_raw()
    vertical_config = get_vertical_config(vertical)

    normalized = normalizer.normalize_all(raw_signals)
    companies = cross_ref.find_overlap(normalized)
    companies = enricher.enrich_all(companies)
    lead_cards = lead_builder.build_lead_cards(companies, scorer.score, vertical_config)

    signals_out = [s.model_dump() for s in normalized]
    companies_out = []
    for company in companies:
        copy = {k: v for k, v in company.items() if k != "signals"}
        copy["signals"] = [s.model_dump() for s in company.get("signals", [])]
        companies_out.append(copy)

    stats = {
        "mode": "offline",
        "vertical": vertical,
        "raw_signals": len(raw_signals),
        "normalized": len(normalized),
        "companies": len(companies),
        "leads": len(lead_cards),
        "leads_meeting_threshold": sum(1 for c in lead_cards if c["meets_threshold"]),
        "employee_band": [settings.LINKEDIN_MIN_EMPLOYEES, settings.LINKEDIN_MAX_EMPLOYEES],
    }
    store.save_run(signals_out, companies_out, lead_cards, stats)
    return stats


def main() -> int:
    """Entry point: scrape (or load), process, score, and persist to JSON."""
    parser = argparse.ArgumentParser(description="Scrape sources to JSON lead cards")
    parser.add_argument("--offline", action="store_true", help="reprocess saved raw JSON")
    parser.add_argument("--lookback", type=int, default=settings.HIRING_LOOKBACK_DAYS)
    parser.add_argument("--vertical", default="recruitment")
    args = parser.parse_args()

    use_offline = args.offline or not settings.APIFY_API_TOKEN
    if use_offline and not args.offline:
        logger.warning("APIFY_API_TOKEN not set — falling back to offline mode")

    if use_offline:
        stats = _run_offline(args.vertical)
    else:
        from pipeline import run_pipeline

        stats = asyncio.run(run_pipeline(args.lookback, args.vertical))

    print("\n=== Scrape-to-JSON summary ===")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
