"""Quality gates + company-name normalization for lead building.

Two jobs, both about turning raw signals into *sellable* leads:

1. :func:`normalize_company_name` — reduce a company name to a bare matching key
   so the same company arriving from different sources ("TrueFan" from a funding
   headline, "TrueFan Technologies Pvt Ltd" from a job board) groups together.
   This is what lets the high-value hiring+funding overlap fire.

2. :func:`quality_reject_reason` — drop companies that aren't real prospects:
   mega-corps with in-house talent teams, staffing/consulting firms (competitors,
   not clients), and stale signals past their freshness window.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Legal-entity suffixes stripped when building a match key.
_LEGAL_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "llp", "inc", "incorporated", "llc",
    "corp", "corporation", "co", "company", "gmbh", "plc", "sa", "srl", "bv",
    "ag", "oy", "pte", "sas", "spa", "kk", "pty", "ulc",
}
# Generic descriptor suffixes stripped for matching only (not for display).
_DESCRIPTOR_SUFFIXES = {
    "technologies", "technology", "tech", "solutions", "solution", "software",
    "systems", "system", "labs", "lab", "services", "service", "ventures",
    "global", "international", "india", "digital", "group", "networks",
    "network", "online", "industries", "enterprises", "ai", "io",
}
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_company_name(name: Optional[str]) -> str:
    """Reduce a company name to a bare matching key (lowercase core tokens).

    Strips punctuation and trailing legal/descriptor suffixes so variants of the
    same company collapse to one key. Returns "" when nothing meaningful remains.
    """
    if not name:
        return ""
    text = _PUNCT_RE.sub(" ", name.lower())
    tokens = [t for t in text.split() if t]
    while tokens and (tokens[-1] in _LEGAL_SUFFIXES or tokens[-1] in _DESCRIPTOR_SUFFIXES):
        tokens.pop()
    return " ".join(tokens).strip()


# Enterprise / IT-services giants that aren't real SMB leads (they have in-house
# talent acquisition). Matched as whole tokens to avoid false positives.
_MEGA_CORP_TOKENS = {
    "accenture", "infosys", "wipro", "cognizant", "capgemini", "tcs", "hcl",
    "mphasis", "mindtree", "ltimindtree", "genpact", "concentrix", "ibm",
    "oracle", "microsoft", "amazon", "google", "deloitte", "kpmg", "pwc",
    "ey", "sap", "cisco", "dell", "intel", "samsung", "nvidia", "sutherland",
    "wns", "hexaware", "birlasoft", "coforge", "persistent", "zensar",
}
_MEGA_CORP_PHRASES = (
    "tata consultancy", "tech mahindra", "ntt data", "ernst young",
    "larsen toubro", "l t infotech",
)
# Staffing / recruiting / consulting firms — competitors or middlemen, not clients.
_STAFFING_TERMS = (
    "staffing", "manpower", "recruit", "placement", "talent solution",
    "talent acquisition", "hr solution", "hr service", "hr consult",
    "workforce", "outsourcing", "consultancy", "consultant", "headhunt",
    "head hunt", "executive search", "payroll", " rpo ", "hiring solution",
)


def is_excluded_company(name: Optional[str]) -> bool:
    """True when a company should never be a lead (mega-corp or staffing firm)."""
    if not name or not name.strip():
        return True
    low = f" {name.lower()} "
    norm = normalize_company_name(name)
    tokens = set(norm.split())
    if tokens & _MEGA_CORP_TOKENS:
        return True
    if any(phrase in norm for phrase in _MEGA_CORP_PHRASES):
        return True
    if any(term in low for term in _STAFFING_TERMS):
        return True
    return False


def quality_reject_reason(
    company: dict[str, Any],
    *,
    max_hiring_days: int = 30,
    max_funding_days: int = 120,
) -> Optional[str]:
    """Return a rejection reason, or ``None`` when the company is a valid lead.

    Drops nameless records, excluded companies (mega-corp / staffing), and
    companies whose only signals are stale (older than the freshness windows).
    """
    name = company.get("company_name") or ""
    if not name.strip():
        return "no_name"
    if is_excluded_company(name):
        return "excluded"

    has_hiring = company.get("has_hiring", False)
    has_funding = company.get("has_funding", False)
    hiring_days = company.get("hiring_recency_days")
    funding_days = company.get("funding_recency_days")

    fresh_hiring = has_hiring and hiring_days is not None and hiring_days <= max_hiring_days
    fresh_funding = has_funding and funding_days is not None and funding_days <= max_funding_days
    if fresh_hiring or fresh_funding or company.get("has_social"):
        return None
    return "stale"
