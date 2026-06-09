"""Main pipeline orchestrator.

Runs the full scrape → normalize → dedup → cross-reference → enrich → build →
score → persist → deliver flow. Scraping is parallelized across the sources a
vertical declares; everything downstream operates on the merged signal set and
produces rich Lead Cards.

The pipeline is backend-agnostic: it always persists JSON (so the dashboard works
in Apify-only mode) and, when ``STORAGE_BACKEND=supabase`` with credentials,
also mirrors to Supabase and delivers per active client.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from config import settings
from db import store
from db.models import Client
from processors import (
    ai_verify,
    cross_ref,
    deduplicator,
    enricher,
    lead_builder,
    normalizer,
    scorer,
)
from scrapers.base import BaseScraper
from scrapers.registry import build_instances
from verticals.base_vertical import get_vertical_config

logger = logging.getLogger("lead-intel.pipeline")

# Tracks the most recent run for the /health endpoint.
LAST_RUN: dict[str, Optional[datetime]] = {"last_pipeline_run": None}

# Live progress of the current/last run, surfaced to the dashboard's scrape
# animation (updated in-process as each source is fetched). Single-instance.
RUN_STATUS: dict[str, Any] = {
    "running": False,
    "stage": "idle",          # starting | scraping | processing | done | error
    "vertical": None,
    "started_at": None,
    "finished_at": None,
    "sources": {},            # platform -> {"state": queued|fetching|done|failed, "count": int}
    "leads": None,
    "qualified": None,
}


def _scrapers_for(vertical_config: dict[str, Any]) -> list[BaseScraper]:
    """Instantiate the scrapers a vertical declares (registry-driven)."""
    sources = vertical_config.get("sources") or None
    return build_instances(sources)


async def _fetch_source(scraper: BaseScraper, lookback_days: int) -> list:
    """Run one scraper's blocking fetch in a thread, reporting live status."""
    name = getattr(scraper, "source_platform", type(scraper).__name__)
    RUN_STATUS["sources"][name] = {"state": "fetching", "count": 0}
    try:
        result = await asyncio.to_thread(scraper.fetch, lookback_days)
        RUN_STATUS["sources"][name] = {"state": "done", "count": len(result)}
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("Scraper %s failed: %s", type(scraper).__name__, exc)
        RUN_STATUS["sources"][name] = {"state": "failed", "count": 0}
        return []


def _company_filters_match(company: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Apply a client's company filters (country, industry, employee band)."""
    if not filters:
        return True
    company_row = company.get("company_row") or {}
    industries = filters.get("industries") or []
    countries = filters.get("countries") or []
    min_emp = filters.get("min_employees")
    max_emp = filters.get("max_employees")

    if industries and company_row.get("industry") not in industries:
        return False
    if countries and company_row.get("hq_country") not in countries:
        return False
    emp = company_row.get("employee_count")
    if min_emp is not None and emp is not None and emp < min_emp:
        return False
    if max_emp is not None and emp is not None and emp > max_emp:
        return False
    return True


async def run_pipeline(
    lookback_days: Optional[int] = None, vertical: str = "recruitment"
) -> dict[str, Any]:
    """Execute one full pipeline run and return summary statistics."""
    lookback_days = lookback_days or settings.LOOKBACK_DAYS
    vertical_config = get_vertical_config(vertical)
    stats: dict[str, Any] = {
        "vertical": vertical,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "raw_signals": 0,
        "normalized": 0,
        "after_ai_verify": 0,
        "after_dedup": 0,
        "companies": 0,
        "enriched": 0,
        "leads": 0,
        "leads_meeting_threshold": 0,
        "leads_delivered": 0,
    }
    logger.info("Pipeline run starting (vertical=%s, lookback=%d)", vertical, lookback_days)

    # STEP 1 — Scrape the vertical's sources in parallel.
    scrapers = _scrapers_for(vertical_config)
    RUN_STATUS.update({
        "running": True, "stage": "scraping", "vertical": vertical,
        "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
        "leads": None, "qualified": None,
        "sources": {
            getattr(s, "source_platform", type(s).__name__): {"state": "queued", "count": 0}
            for s in scrapers
        },
    })
    raw_lists = await asyncio.gather(
        *(_fetch_source(s, lookback_days) for s in scrapers)
    )
    raw_signals = [sig for sublist in raw_lists for sig in sublist]
    stats["raw_signals"] = len(raw_signals)
    RUN_STATUS["stage"] = "processing"

    # STEP 2 — Normalize → canonical signals.
    normalized = normalizer.normalize_all(raw_signals)
    stats["normalized"] = len(normalized)

    # STEP 2.5 — AI verification gate. Filters noisy free-text signals (Reddit,
    # X, Facebook) down to real, on-niche companies (no-op without GROQ_API_KEY).
    RUN_STATUS["stage"] = "verifying"
    normalized = await ai_verify.verify_signals(normalized, vertical_config)
    stats["after_ai_verify"] = len(normalized)
    RUN_STATUS["stage"] = "processing"

    # STEP 3 — Deduplicate (24h window per company; no-op without Redis).
    deduped = deduplicator.filter_duplicates(normalized)
    stats["after_dedup"] = len(deduped)

    # STEP 4 — Cross-reference into per-company records.
    companies = cross_ref.find_overlap(deduped)
    stats["companies"] = len(companies)

    # STEP 5 — Enrich with Apollo (no-op without APOLLO_API_KEY).
    companies = enricher.enrich_all(companies)
    stats["enriched"] = sum(1 for c in companies if c.get("enrichment"))

    # STEP 6 — Build rich Lead Cards (scored against the vertical).
    lead_cards = lead_builder.build_lead_cards(companies, scorer.score, vertical_config)
    stats["leads"] = len(lead_cards)
    stats["leads_meeting_threshold"] = sum(
        1 for c in lead_cards if c.get("meets_threshold")
    )

    # STEP 7 — Persist (JSON always; Supabase when configured).
    signals_out = [s.model_dump() for s in deduped]
    companies_out = _companies_serializable(companies)
    store.save_run(signals_out, companies_out, lead_cards, stats)

    # STEP 8 — Deliver to active clients (Supabase-backed multi-tenant mode).
    if settings.use_supabase:
        stats["leads_delivered"] = await _deliver_to_clients(lead_cards)

    LAST_RUN["last_pipeline_run"] = datetime.now(timezone.utc)
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    RUN_STATUS.update({
        "running": False,
        "stage": "done",
        "finished_at": stats["finished_at"],
        "leads": len(lead_cards),
        "qualified": stats["leads_meeting_threshold"],
    })
    logger.info("Pipeline run complete: %s", stats)
    return stats


def _companies_serializable(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert NormalizedSignal objects inside company dicts to plain dicts."""
    out: list[dict[str, Any]] = []
    for company in companies:
        copy = {k: v for k, v in company.items() if k != "signals"}
        copy["signals"] = [s.model_dump() for s in company.get("signals", [])]
        out.append(copy)
    return out


async def _deliver_to_clients(lead_cards: list[dict[str, Any]]) -> int:
    """Filter + deliver lead cards to each active client's channels."""
    from db import supabase_client as db
    from delivery import slack, telegram

    total = 0
    for client_row in db.get_active_clients():
        client = Client(**client_row)
        leads = [
            card
            for card in lead_cards
            if card.get("vertical") == client.vertical
            and card.get("score", 0) >= client.min_score
        ]
        if not leads:
            continue
        if client.slack_webhook:
            total += await asyncio.to_thread(slack.send_batch, leads, client)
        if client.telegram_chat_id:
            total += await telegram.send_batch(leads, client)
    return total


async def run_all_digests() -> int:
    """Send the daily email digest to every active client with an email_to."""
    if not settings.use_supabase:
        logger.info("Supabase not configured; skipping digests")
        return 0
    from db import supabase_client as db
    from delivery import email_digest

    sent = 0
    for client_row in db.get_active_clients():
        client = Client(**client_row)
        if not client.email_to or not client.id:
            continue
        recent = db.get_recent_leads_for_client(str(client.id), hours=24)
        if await asyncio.to_thread(email_digest.send_daily_digest, recent, client):
            sent += 1
    logger.info("Sent %d daily digests", sent)
    return sent


if __name__ == "__main__":
    asyncio.run(run_pipeline(lookback_days=settings.HIRING_LOOKBACK_DAYS))
