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
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from config import settings
from db import store
from db.models import Client
from processors import (
    ai_qualify,
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

# Serializes full pipeline runs across event loops/threads (scheduler sweep,
# create-pipeline kickoff thread, manual run). Runs share RUN_STATUS and do
# read-merge-write persistence, so two at once would race and lose leads.
# A threading.Lock (acquired via asyncio.to_thread) works from any loop.
_RUN_LOCK = threading.Lock()

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


def _scrapers_for(
    vertical_config: dict[str, Any], search: Optional[dict[str, Any]] = None
) -> list[BaseScraper]:
    """Instantiate the scrapers for a run (registry-driven).

    A pipeline can override the vertical's default source list via its ICP
    (``filters.sources`` — the per-pipeline tool toggles). Unknown keys are
    skipped gracefully by the registry.
    """
    override = (search or {}).get("sources")
    if isinstance(override, list) and override:
        return build_instances([str(s) for s in override])
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


def _configure_scrapers(scrapers: list[BaseScraper], vertical_config: dict[str, Any],
                        search: Optional[dict[str, Any]]) -> None:
    """Inject per-pipeline search params (category/cities) into scrapers that
    accept them (e.g. Google Maps). Sources without ``configure`` are untouched."""
    search_cfg = {**(vertical_config.get("filters") or {}), **(search or {})}
    # A pipeline's own keywords/categories ARE the Google Maps search term, and
    # must override the vertical's default `categories` (e.g. local_business's
    # default "barber"). Otherwise a "marketing agency" pipeline would inherit
    # the default category and scrape the wrong businesses.
    pipe_cats = (search or {}).get("categories") or (search or {}).get("keywords")
    if pipe_cats:
        search_cfg["categories"] = pipe_cats
    # Per-source actor overrides from the advisor's "Use this" ({key: actor_id}).
    # Applied AFTER configure so the operator's chosen actor always wins. Runs the
    # exact actor picked; fail-safe (an incompatible actor just yields 0 leads).
    overrides = (search or {}).get("actor_overrides") or {}
    for scraper in scrapers:
        if hasattr(scraper, "configure"):
            try:
                scraper.configure(search_cfg)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.error("configure failed for %s: %s", type(scraper).__name__, exc)
        key = getattr(scraper, "key", None) or getattr(scraper, "source_platform", None)
        if isinstance(overrides, dict) and overrides.get(key) and hasattr(scraper, "actor_id"):
            scraper.actor_id = overrides[key]  # type: ignore[attr-defined]
            logger.info("Actor override: %s → %s", key, overrides[key])


async def run_pipeline(
    lookback_days: Optional[int] = None,
    vertical: str = "recruitment",
    search: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Execute one full pipeline run and return summary statistics.

    Runs are serialized process-wide via ``_RUN_LOCK`` — see its comment.
    """
    await asyncio.to_thread(_RUN_LOCK.acquire)
    try:
        return await _run_pipeline_locked(lookback_days, vertical, search)
    finally:
        _RUN_LOCK.release()


async def _run_pipeline_locked(
    lookback_days: Optional[int],
    vertical: str,
    search: Optional[dict[str, Any]],
) -> dict[str, Any]:
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

    # STEP 1 — Scrape the pipeline's sources (its toggles, else the vertical's).
    scrapers = _scrapers_for(vertical_config, search)
    _configure_scrapers(scrapers, vertical_config, search)
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

    # STEP 4.5 — Company-level AI qualification: one Groq verdict per deduped
    # company (junk filter + "why it's a lead" line + HQ-city extraction).
    # Directory sources skip it; fail-open without a GROQ key.
    RUN_STATUS["stage"] = "qualifying"
    companies = await ai_qualify.qualify_companies(companies, vertical_config)
    stats["after_ai_qualify"] = len(companies)
    RUN_STATUS["stage"] = "processing"

    # STEP 5 — Firmographics for the top leads via Apollo's FREE org endpoint
    # (industry, size, HQ city — capped, no people credits). Decision-maker
    # emails stay ON-DEMAND behind the "Reveal contacts" button.
    for company in companies:
        company["enrichment"] = None
    RUN_STATUS["stage"] = "enriching"
    stats["enriched"] = await asyncio.to_thread(
        enricher.auto_enrich_orgs, companies, scorer.score, vertical_config
    )
    RUN_STATUS["stage"] = "processing"

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


async def preview_run(
    vertical: str = "local_business",
    search: Optional[dict[str, Any]] = None,
    max_results: int = 8,
) -> list[dict[str, Any]]:
    """Run a small, NON-persisted scrape and return lead-card dicts.

    Powers the Pipelines "test fetch": fetches a capped sample for the given
    vertical + search (category/cities), runs it through normalize → cross-ref →
    score → build, and returns the cards WITHOUT touching the stored pool.
    """
    vertical_config = get_vertical_config(vertical)
    scrapers = _scrapers_for(vertical_config, search)
    _configure_scrapers(
        scrapers, vertical_config, {**(search or {}), "max_results": max_results}
    )
    raw_lists = await asyncio.gather(
        *(asyncio.to_thread(s.fetch, settings.LOOKBACK_DAYS) for s in scrapers)
    )
    raw_signals = [sig for sublist in raw_lists for sig in sublist]
    normalized = normalizer.normalize_all(raw_signals)
    # NO cross-run dedup here: previews must be side-effect-free. Marking these
    # companies "seen" (24h Redis TTL) would make the create-pipeline kickoff
    # scrape that follows a preview drop every one of them → 0 leads.
    companies = cross_ref.find_overlap(normalized)
    # Skip Apollo enrichment on previews — a test fetch shouldn't spend credits.
    return lead_builder.build_lead_cards(companies, scorer.score, vertical_config)


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


async def run_all_pipelines() -> dict[str, Any]:
    """Run the pipeline once per saved pipeline (active client), not a default.

    Scheduled/automatic runs must scrape exactly what the user configured: each
    pipeline's vertical with its own source toggles, cities, and zone. With no
    pipelines saved, nothing is scraped — there is no fallback to a hardcoded
    vertical, because that re-runs sources the user never asked for.
    Duplicate pipelines (same vertical + filters) are collapsed into one run.
    """
    # Pipelines live in the Supabase ``clients`` table (read with the service
    # key) even when lead storage is JSON — so gate on that, not STORAGE_BACKEND.
    if not (settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY):
        logger.info("Supabase not configured; running default vertical")
        return await run_pipeline()
    from db import supabase_client as db

    seen: set[str] = set()
    runs: list[dict[str, Any]] = []
    for client_row in db.get_active_clients():
        vertical = client_row.get("vertical")
        if not vertical:
            continue
        search = client_row.get("filters") or None
        key = json.dumps({"v": vertical, "s": search}, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        runs.append({"vertical": vertical, "search": search})

    if not runs:
        logger.info("No active pipelines; skipping scheduled scrape")
        return {"pipelines": 0, "leads": 0}

    totals = {"pipelines": 0, "leads": 0}
    for run in runs:  # sequential: shared RUN_STATUS + Apify concurrency limits
        try:
            stats = await run_pipeline(vertical=run["vertical"], search=run["search"])
            totals["pipelines"] += 1
            totals["leads"] += stats.get("leads", 0)
        except Exception as exc:  # noqa: BLE001 - one bad pipeline must not stop the rest
            logger.error("Scheduled run failed for vertical=%s: %s", run["vertical"], exc)
    logger.info("Scheduled sweep done: %(pipelines)d pipelines, %(leads)d leads", totals)
    return totals


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
