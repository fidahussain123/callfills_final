"""Storage facade — one API over the JSON and Supabase backends.

The pipeline and dashboard talk to this module instead of a concrete backend.
``save_run`` always writes JSON (for the dashboard + reproducibility) and, when
``STORAGE_BACKEND=supabase`` with credentials present, also persists to Supabase.
Reads are served from the JSON artifacts, which the pipeline always refreshes —
this keeps the dashboard fully functional in Apify-only / no-Supabase mode.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from config import settings
from db import json_store

logger = logging.getLogger("lead-intel.db.store")

_LEADS_FILE = "leads.json"
_LATEST_FILE = "latest.json"

# Short-lived cache of the DB lead pool so a single page render (which calls
# _read_leads several times) doesn't hit Supabase repeatedly. Writes invalidate
# it immediately, so leads appear right after a run.
_pool_cache: Optional[tuple[list[dict[str, Any]], float]] = None
_POOL_TTL = 10.0  # seconds


# ──────────────────────────────────────────────────────────────────────────────
# Writes
# ──────────────────────────────────────────────────────────────────────────────
def save_run(
    signals: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    leads: list[dict[str, Any]],
    stats: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    """Persist a full run. Always JSON; also Supabase when configured."""
    global _pool_cache
    paths = json_store.save_run(signals, companies, leads, stats)
    if settings.use_supabase:
        try:
            _persist_supabase(companies, leads)
            _pool_cache = None  # invalidate so the new leads show immediately
        except Exception as exc:  # noqa: BLE001 - JSON already saved; don't abort
            logger.error("Supabase persistence failed (JSON saved): %s", exc)
    return paths


def _persist_supabase(
    companies: list[dict[str, Any]], leads: list[dict[str, Any]]
) -> None:
    """Best-effort mirror of a run into Supabase (companies, signals, leads)."""
    from db import supabase_client as sb

    for company in companies:
        row = sb.upsert_company(
            {
                "name": company.get("company_name"),
                "domain": company.get("company_domain"),
            }
        )
        company_id = (row or {}).get("id")
        for sig in company.get("signals", []):
            sig = sig if isinstance(sig, dict) else sig.model_dump()
            sb.insert_signal(
                {
                    "company_id": company_id,
                    "signal_type": sig.get("signal_type"),
                    "source_platform": sig.get("source_platform"),
                    "raw_text": sig.get("raw_text"),
                    "source_url": sig.get("source_url"),
                    "detected_at": sig.get("detected_at"),
                    "metadata": sig.get("metadata", {}),
                }
            )

    # Persist the generated lead cards into the shared pool the dashboard reads,
    # so leads survive restarts / ephemeral-disk deploys (not just local JSON).
    verticals = {lead.get("vertical") for lead in leads if lead.get("vertical")}
    written = sb.replace_pool_leads(verticals, leads)
    logger.info("Mirrored %d companies + %d leads to Supabase", len(companies), written)


# ──────────────────────────────────────────────────────────────────────────────
# Reads (served from JSON artifacts the pipeline refreshes every run)
# ──────────────────────────────────────────────────────────────────────────────
def _read_leads() -> list[dict[str, Any]]:
    """Load the most recent lead cards.

    When Supabase is the backend, read the shared ``lead_pool`` (so leads survive
    restarts / ephemeral disks), with a short cache. Falls back to JSON when the
    backend is JSON, the pool is empty (e.g. before the first DB run), or the DB
    read errors — so the dashboard is never blank when JSON has data.
    """
    global _pool_cache
    if settings.use_supabase:
        now = time.time()
        if _pool_cache and _pool_cache[1] > now:
            return _pool_cache[0]
        try:
            from db import supabase_client as sb

            pool = sb.get_pool_leads(limit=2000)
            if pool:
                _pool_cache = (pool, now + _POOL_TTL)
                return pool
        except Exception as exc:  # noqa: BLE001 - fall back to JSON
            logger.error("DB lead-pool read failed, falling back to JSON: %s", exc)

    try:
        data = json_store.read_json(_LEADS_FILE)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to read leads.json: %s", exc)
        return []


def get_latest() -> dict[str, Any]:
    """Return the latest run payload (stats + counts + leads), or empty dict."""
    try:
        data = json_store.read_json(_LATEST_FILE)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to read latest.json: %s", exc)
        return {}


def get_stats() -> dict[str, Any]:
    """Return run stats + counts + generated-at from the latest run."""
    latest = get_latest()
    return {
        "generated_at": latest.get("generated_at"),
        "counts": latest.get("counts", {}),
        "stats": latest.get("stats", {}),
    }


def get_leads(
    min_score: Optional[int] = None,
    vertical: Optional[str] = None,
    source: Optional[str] = None,
    has_funding: Optional[bool] = None,
    has_hiring: Optional[bool] = None,
    only_threshold: bool = False,
    search: Optional[str] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return lead cards filtered in-memory and sorted by score descending."""
    leads = _read_leads()

    def keep(lead: dict[str, Any]) -> bool:
        if min_score is not None and lead.get("score", 0) < min_score:
            return False
        if vertical and lead.get("vertical") != vertical:
            return False
        if source and source not in (lead.get("signal_sources") or []):
            return False
        if has_funding is not None and bool(lead.get("has_funding")) != has_funding:
            return False
        if has_hiring is not None and bool(lead.get("has_hiring")) != has_hiring:
            return False
        if only_threshold and not lead.get("meets_threshold"):
            return False
        if search:
            needle = search.lower()
            haystack = " ".join(
                str(lead.get(k, "")) for k in ("company_name", "website", "location")
            ).lower()
            if needle not in haystack:
                return False
        return True

    filtered = [lead for lead in leads if keep(lead)]
    filtered.sort(key=lambda lead: lead.get("score", 0), reverse=True)
    return filtered[:limit]


def get_lead_matches(company_name: str) -> list[dict[str, Any]]:
    """All lead cards with this (case-insensitive) name, best score first.

    The same company can appear under multiple verticals (e.g. a funded startup
    scraped by both an old recruitment run and the startups pipeline), so the
    caller must pick the copy that fits its context — e.g. the one inside the
    tenant's scope.
    """
    target = (company_name or "").strip().lower()
    matches = [
        lead
        for lead in _read_leads()
        if (lead.get("company_name") or "").strip().lower() == target
    ]
    matches.sort(key=lambda lead: lead.get("score") or 0, reverse=True)
    return matches


def get_lead(company_name: str) -> Optional[dict[str, Any]]:
    """Return the best-scored lead card for a (case-insensitive) company name."""
    matches = get_lead_matches(company_name)
    return matches[0] if matches else None


def available_sources() -> list[str]:
    """Return the distinct signal sources present across current leads."""
    sources: set[str] = set()
    for lead in _read_leads():
        sources.update(lead.get("signal_sources") or [])
    return sorted(sources)


def get_signals(limit: int = 600) -> list[dict[str, Any]]:
    """Return the normalized signals from the latest run (for the Signals feed)."""
    try:
        data = json_store.read_json("signals.json")
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to read signals.json: %s", exc)
        return []
    return data[:limit] if isinstance(data, list) else []
