#!/usr/bin/env python3
"""Export fetched Lead Cards to CSV.

Reads the stored lead cards (data/leads.json by default), optionally filters to
India-based leads, and writes a flat CSV — one row per lead — that keeps the rich
signal evidence (latest post link + date, plus every signal URL pipe-joined).

Usage:
    python3 scripts/export_leads_csv.py                      # all leads -> data/leads_india.csv
    python3 scripts/export_leads_csv.py --all-locations      # do not filter by India
    python3 scripts/export_leads_csv.py --in data/leads.json --out data/my_leads.csv
    python3 scripts/export_leads_csv.py --min-score 30        # only score >= 30
    python3 scripts/export_leads_csv.py --qualified-only      # only meets_threshold
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

# Tokens that mark a lead as India-based (matched case-insensitively in location).
_INDIA_TOKENS = (
    "india", "bangalore", "bengaluru", "mumbai", "delhi", "pune", "hyderabad",
    "gurgaon", "gurugram", "chennai", "kolkata", "noida", "ahmedabad", "jaipur",
    "kochi", "nagpur", "thane", "nashik", "aurangabad", "karnataka", "maharashtra",
    "telangana", "haryana", "kerala",
)

# Output columns (order matters — this is what the recruiter sees in Excel).
_COLUMNS = [
    "company_name", "website", "location", "industry", "employee_count", "vertical",
    "score", "qualified", "summary",
    "has_hiring", "has_funding", "has_social",
    "hiring_role_count", "hiring_recency_days",
    "funding_round", "funding_amount", "funding_recency_days",
    "signal_count", "signal_sources", "signal_types",
    "contact_name", "contact_role", "email", "linkedin_url",
    "latest_signal_date", "latest_signal_source", "latest_signal_url", "latest_signal_snippet",
    "all_signal_urls",
    "created_at",
]


def _is_india(lead: dict) -> bool:
    loc = (lead.get("location") or "").lower()
    return any(tok in loc for tok in _INDIA_TOKENS)


def _yn(value) -> str:
    return "Yes" if value else "No"


def _join(value) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value if v not in (None, ""))
    return "" if value in (None, "") else str(value)


def _latest_signal(signals: list[dict]) -> dict:
    """Signals are stored newest-first; return the first usable one."""
    return signals[0] if signals else {}


def _row(lead: dict) -> dict:
    signals = lead.get("signals") or []
    latest = _latest_signal(signals)
    urls = [s.get("source_url") for s in signals if s.get("source_url")]
    detected = (latest.get("detected_at") or "")
    return {
        "company_name": lead.get("company_name", ""),
        "website": lead.get("website") or lead.get("company_domain") or "",
        "location": lead.get("location") or "",
        "industry": lead.get("industry") or "",
        "employee_count": lead.get("employee_count") or "",
        "vertical": lead.get("vertical") or "",
        "score": lead.get("score", 0),
        "qualified": _yn(lead.get("meets_threshold")),
        "summary": lead.get("summary") or "",
        "has_hiring": _yn(lead.get("has_hiring")),
        "has_funding": _yn(lead.get("has_funding")),
        "has_social": _yn(lead.get("has_social")),
        "hiring_role_count": lead.get("hiring_role_count") or "",
        "hiring_recency_days": lead.get("hiring_recency_days")
        if lead.get("hiring_recency_days") is not None else "",
        "funding_round": lead.get("funding_round") or "",
        "funding_amount": lead.get("funding_amount") or "",
        "funding_recency_days": lead.get("funding_recency_days")
        if lead.get("funding_recency_days") is not None else "",
        "signal_count": len(signals),
        "signal_sources": _join(lead.get("signal_sources")),
        "signal_types": _join(lead.get("signal_types")),
        "contact_name": lead.get("contact_name") or "",
        "contact_role": lead.get("contact_role") or "",
        "email": lead.get("email") or "",
        "linkedin_url": lead.get("linkedin_url") or "",
        "latest_signal_date": detected[:10] if detected else "",
        "latest_signal_source": latest.get("source_platform") or "",
        "latest_signal_url": latest.get("source_url") or "",
        "latest_signal_snippet": latest.get("snippet") or "",
        "all_signal_urls": " | ".join(urls),
        "created_at": lead.get("created_at") or "",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Export Lead Cards to CSV.")
    ap.add_argument("--in", dest="infile", default="data/leads.json",
                    help="Input lead-cards JSON (default: data/leads.json)")
    ap.add_argument("--out", dest="outfile", default="data/leads_india.csv",
                    help="Output CSV path (default: data/leads_india.csv)")
    ap.add_argument("--all-locations", action="store_true",
                    help="Do not filter to India-based leads.")
    ap.add_argument("--min-score", type=int, default=0,
                    help="Only include leads with score >= this value.")
    ap.add_argument("--qualified-only", action="store_true",
                    help="Only include leads that meet the score threshold.")
    args = ap.parse_args()

    if not os.path.exists(args.infile):
        print(f"ERROR: input file not found: {args.infile}", file=sys.stderr)
        return 1

    with open(args.infile, encoding="utf-8") as fh:
        data = json.load(fh)
    leads = data if isinstance(data, list) else data.get("leads", [])

    rows = []
    for lead in leads:
        if not args.all_locations and not _is_india(lead):
            continue
        if (lead.get("score") or 0) < args.min_score:
            continue
        if args.qualified_only and not lead.get("meets_threshold"):
            continue
        rows.append(_row(lead))

    # Highest score first — recruiters work the top of the list.
    rows.sort(key=lambda r: r["score"], reverse=True)

    os.makedirs(os.path.dirname(os.path.abspath(args.outfile)), exist_ok=True)
    with open(args.outfile, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} lead(s) -> {args.outfile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
