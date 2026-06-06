"""Standalone smoke test for lead-intel.

Validates connectivity to each external dependency, then runs the full pipeline
with a short lookback. Each step prints PASS/FAIL and the script exits non-zero
if any step fails, so it doubles as a CI gate.

Run with:  python test_pipeline.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lead-intel.test")

_results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    """Record and print a step result."""
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    _results.append((name, ok, detail))


def test_apify() -> None:
    """Run the LinkedIn Jobs actor with a tiny result cap."""
    try:
        from scrapers.linkedin_jobs import LinkedInJobsScraper

        scraper = LinkedInJobsScraper()
        if scraper._client is None:  # noqa: SLF001 - intentional smoke check
            _record("Apify connection", False, "APIFY_API_TOKEN not configured")
            return
        items = scraper.run_actor(scraper.build_input(7), max_items=5)
        _record("Apify connection", True, f"{len(items)} items returned")
    except Exception as exc:  # noqa: BLE001
        _record("Apify connection", False, str(exc))


def test_apollo() -> None:
    """Search Apollo for a CTO at stripe.com."""
    try:
        from processors.enricher import enrich_company

        result = enrich_company("stripe.com", "Stripe")
        _record(
            "Apollo connection",
            result is not None,
            (result or {}).get("contact_role", "no contact found"),
        )
    except Exception as exc:  # noqa: BLE001
        _record("Apollo connection", False, str(exc))


def test_supabase() -> None:
    """Insert and delete a throwaway company row."""
    try:
        from db.supabase_client import get_client

        client = get_client()
        domain = f"test-{uuid.uuid4().hex[:8]}.example.com"
        ins = client.table("companies").insert(
            {"name": "Test Co", "domain": domain}
        ).execute()
        row_id = ins.data[0]["id"]
        client.table("companies").delete().eq("id", row_id).execute()
        _record("Supabase connection", True, "insert + delete ok")
    except Exception as exc:  # noqa: BLE001
        _record("Supabase connection", False, str(exc))


def test_redis() -> None:
    """Set, get, and delete a throwaway key."""
    try:
        from processors.deduplicator import _get_redis  # noqa: SLF001

        client = _get_redis()
        if client is None:
            _record("Redis connection", False, "could not connect")
            return
        key = f"test:{uuid.uuid4().hex[:8]}"
        client.set(key, "1", ex=10)
        value = client.get(key)
        client.delete(key)
        _record("Redis connection", value == "1", "set/get/delete ok")
    except Exception as exc:  # noqa: BLE001
        _record("Redis connection", False, str(exc))


def test_full_pipeline() -> None:
    """Run the whole pipeline with a 7-day lookback and print summary stats."""
    try:
        from pipeline import run_pipeline

        stats = asyncio.run(run_pipeline(lookback_days=7))
        print("    Pipeline stats:", stats)
        _record("Full pipeline run", True, f"{stats.get('companies', 0)} companies")
    except Exception as exc:  # noqa: BLE001
        _record("Full pipeline run", False, str(exc))


def main() -> int:
    """Run all smoke tests and return a process exit code."""
    print("=" * 60)
    print("lead-intel smoke test")
    print("=" * 60)

    test_apify()
    test_apollo()
    test_supabase()
    test_redis()
    test_full_pipeline()

    print("=" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"{passed}/{len(_results)} steps passed")
    print("=" * 60)
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
