"""Real-time Signals radar — polls social sources and streams verified intent.

Runs on the scheduler every ``RADAR_INTERVAL_SECONDS`` (gated by
``RADAR_ENABLED``). Each cycle:

    fetch new posts since cursor  (Hacker News now; Reddit once keys exist)
        → normalize (company extraction)
        → 🤖 Groq AI verify — the "verify, then proceed" checkpoint
        → dedupe by post id
        → append to ``radar_signals.json`` (rolling, capped)

The Signals page unions this rolling file with the pipeline's ``signals.json``,
so fresh posts appear within a minute without touching the lead pool. A separate
file also means a pipeline run can never overwrite radar finds.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from config import settings
from db import json_store
from processors import ai_verify, normalizer
from verticals.base_vertical import get_vertical_config

logger = logging.getLogger("lead-intel.radar")

_RADAR_FILE = "radar_signals.json"
_CURSOR_FILE = "radar_cursor.json"
_MAX_STORED = 500
_FIRST_RUN_LOOKBACK_DAYS = 2  # backfill window on the very first cycle

# Radar sources: registry keys polled each cycle. Reddit joins here once its
# OAuth credentials are configured (scrapers/reddit.py stays pipeline-free).
_RADAR_SOURCES = ["hackernews"]


def _load_cursor() -> int:
    try:
        data = json_store.read_json(_CURSOR_FILE)
        return int(data.get("since", 0))
    except Exception:  # noqa: BLE001 - first run
        return 0


def _load_stored() -> list[dict[str, Any]]:
    try:
        data = json_store.read_json(_RADAR_FILE)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def get_radar_signals() -> list[dict[str, Any]]:
    """Rolling, AI-verified radar signals (newest first) for the Signals page."""
    return _load_stored()


async def run_radar_cycle() -> int:
    """One radar pass; returns how many new verified signals were stored."""
    from scrapers.registry import build_instances

    scrapers = build_instances(_RADAR_SOURCES)
    if not scrapers:
        return 0

    since = _load_cursor()
    if not since:
        since = int(datetime.now(timezone.utc).timestamp()) - _FIRST_RUN_LOOKBACK_DAYS * 86400

    for scraper in scrapers:
        if hasattr(scraper, "configure"):
            scraper.configure({"since": since})

    raw_lists = await asyncio.gather(
        *(asyncio.to_thread(s.fetch, _FIRST_RUN_LOOKBACK_DAYS) for s in scrapers)
    )
    raw = [r for sub in raw_lists for r in sub]
    if not raw:
        json_store.write_json(_CURSOR_FILE, {"since": int(datetime.now(timezone.utc).timestamp())})
        return 0

    # Normalize (extracts a company from the free text where possible)…
    normalized = normalizer.normalize_all(raw)
    # …then the AI gate: only real, on-niche buying signals proceed.
    vertical_config = get_vertical_config(settings.RADAR_VERTICAL)
    verified = await ai_verify.verify_signals(normalized, vertical_config)

    stored = _load_stored()
    seen_ids = {s.get("metadata", {}).get("post_id") for s in stored}
    seen_urls = {s.get("source_url") for s in stored}
    fresh: list[dict[str, Any]] = []
    for sig in verified:
        d = sig.model_dump()
        pid = (d.get("metadata") or {}).get("post_id")
        if (pid and pid in seen_ids) or d.get("source_url") in seen_urls:
            continue
        fresh.append(d)

    if fresh:
        fresh.sort(key=lambda s: str(s.get("detected_at") or ""), reverse=True)
        json_store.write_json(_RADAR_FILE, (fresh + stored)[:_MAX_STORED])
    json_store.write_json(_CURSOR_FILE, {"since": int(datetime.now(timezone.utc).timestamp())})
    logger.info(
        "Radar cycle: %d raw → %d normalized → %d AI-verified → %d new stored",
        len(raw), len(normalized), len(verified), len(fresh),
    )
    return len(fresh)
