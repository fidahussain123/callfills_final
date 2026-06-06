"""JSON file storage backend (Apollo/Supabase-free mode).

Until real database credentials are provided, the pipeline can persist its
output to local JSON files under ``data/``. This module mirrors the small subset
of the Supabase helper API that the offline flow needs: writing signals,
companies, and leads, plus loading previously-scraped raw data.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("lead-intel.db.json_store")

# Default output directory: <project>/data
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _ensure_dir(path: str) -> None:
    """Create the parent directory for a path if it does not exist."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def data_path(filename: str) -> str:
    """Return an absolute path inside the data directory."""
    return os.path.join(_DATA_DIR, filename)


def write_json(filename: str, payload: Any) -> str:
    """Write ``payload`` as pretty JSON to ``data/<filename>`` and return the path."""
    path = filename if os.path.isabs(filename) else data_path(filename)
    _ensure_dir(path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
    logger.info("Wrote %s", path)
    return path


def read_json(filename: str) -> Any:
    """Read JSON from ``data/<filename>`` (or an absolute path)."""
    path = filename if os.path.isabs(filename) else data_path(filename)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _has_existing_leads() -> bool:
    """True when leads.json already holds a non-empty pool."""
    try:
        data = read_json("leads.json")
        return isinstance(data, list) and len(data) > 0
    except Exception:  # noqa: BLE001 - missing/corrupt file → treat as empty
        return False


def save_run(
    signals: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    leads: list[dict[str, Any]],
    stats: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Persist a full pipeline run to timestamped + latest JSON files.

    A failed/empty scrape (0 signals) must NEVER wipe a healthy pool: the run is
    still archived for the record, but ``latest.json`` / ``leads.json`` (what the
    dashboard reads) are left intact when the new run is empty and a pool exists.

    Returns a mapping of artifact name to the file path written.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats or {},
        "counts": {
            "signals": len(signals),
            "companies": len(companies),
            "leads": len(leads),
        },
        "signals": signals,
        "companies": companies,
        "leads": leads,
    }
    # Always archive the run (even an empty one — useful for debugging).
    paths = {"run": write_json(f"run_{stamp}.json", run)}

    # Guard: don't let an empty scrape clobber a healthy pool.
    if not signals and not leads and _has_existing_leads():
        logger.warning(
            "Run produced 0 signals/leads — keeping the existing pool (not "
            "overwriting latest.json/leads.json). Check Apify credits / sources."
        )
        return paths

    paths["latest"] = write_json("latest.json", run)
    paths["leads"] = write_json("leads.json", leads)
    paths["companies"] = write_json("companies.json", companies)
    paths["signals"] = write_json("signals.json", signals)
    return paths
