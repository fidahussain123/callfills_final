"""Text intelligence for unstructured signals (Reddit, Twitter, RSS).

Provides keyword-based signal classification and spaCy-NER company extraction.
The spaCy model is loaded lazily and cached so importing this module is cheap
and the pipeline degrades gracefully if the model is not installed.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse

from signal_types import (
    AGENCY_SWITCH,
    FUNDING_ROUND,
    GENERAL_SIGNAL,
    GROWTH_PAIN,
    HIRING_POST,
    OUTSOURCE_INTENT,
)

logger = logging.getLogger("lead-intel.processors.extractor")

# ──────────────────────────────────────────────────────────────────────────────
# Signal-type keyword tables (checked in priority order)
# ──────────────────────────────────────────────────────────────────────────────
_SIGNAL_KEYWORDS: list[tuple[str, list[str]]] = [
    (
        FUNDING_ROUND,
        [
            "raised",
            "series a",
            "series b",
            "seed round",
            "just closed",
            "secured funding",
            "new investment",
            "backed by",
            "$m",
            "million",
        ],
    ),
    (
        AGENCY_SWITCH,
        [
            "switching from",
            "leaving",
            "fired our agency",
            "bad experience with",
            "looking for new agency",
            "frustrated with our",
            "replacing our",
        ],
    ),
    (
        OUTSOURCE_INTENT,
        [
            "outsource",
            "looking for agency",
            "need a development team",
            "contractor",
            "freelancer",
            "recommendation for",
            "who do you use for",
        ],
    ),
    (
        HIRING_POST,
        [
            "hiring",
            "we're hiring",
            "we’re hiring",
            "join our team",
            "open role",
            "job opening",
            "looking for engineer",
            "we need a",
            "open position",
        ],
    ),
    (
        GROWTH_PAIN,
        [
            "scaling issues",
            "can't keep up",
            "can’t keep up",
            "overwhelmed",
            "growing fast",
            "need help",
            "too much work",
        ],
    ),
]


def detect_signal_type(text: str) -> str:
    """Classify free text into one of the known signal types.

    Categories are checked in priority order; the first matching category wins.
    Returns ``"general_signal"`` when nothing matches.
    """
    if not text:
        return GENERAL_SIGNAL
    lower = text.lower()
    for signal_type, keywords in _SIGNAL_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return signal_type
    return GENERAL_SIGNAL


# ──────────────────────────────────────────────────────────────────────────────
# spaCy NER for company extraction
# ──────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _load_nlp():
    """Lazily load the spaCy ``en_core_web_sm`` model, or ``None`` if missing."""
    try:
        import spacy  # imported lazily to keep module import cheap

        return spacy.load("en_core_web_sm")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "spaCy en_core_web_sm not available (%s); company extraction degraded", exc
        )
        return None


# Tokens that NER often mislabels as ORG but are platforms, not companies.
_ORG_STOPWORDS = {
    "reddit",
    "twitter",
    "x",
    "linkedin",
    "indeed",
    "naukri",
    "wellfound",
    "techcrunch",
    "venturebeat",
    "yourstory",
    "inc42",
    "entrackr",
    "economic times",
    "et",
    "the economic times",
    "moneycontrol",
    "livemint",
    "mint",
    "sifted",
    "google",
    "google news",
    "ycombinator",
    "y combinator",
    "sebi",
    "rbi",
}


def extract_company_name(text: str, source_url: str = "") -> Optional[str]:
    """Extract the most prominent company (ORG) name from ``text``.

    Strategy:

    1. Run spaCy NER and collect ORG spans.
    2. Drop known platform/publisher names.
    3. Prefer the first-mentioned ORG (usually the subject of a headline/post).
    4. Fallback: derive a company token from the source URL's host or path.
    """
    nlp = _load_nlp()
    if nlp is not None and text:
        try:
            doc = nlp(text[:5000])
            orgs = [
                ent.text.strip()
                for ent in doc.ents
                if ent.label_ == "ORG" and ent.text.strip()
            ]
            for org in orgs:
                if org.lower() not in _ORG_STOPWORDS and len(org) > 1:
                    return org
        except Exception as exc:  # noqa: BLE001
            logger.debug("NER failed: %s", exc)

    # Fallback: try to recover something usable from the URL.
    domain = extract_domain_from_url(source_url)
    if domain:
        return domain.split(".")[0].replace("-", " ").title()
    return None


# ──────────────────────────────────────────────────────────────────────────────
# URL → domain / company-slug extraction
# ──────────────────────────────────────────────────────────────────────────────
_COMPANY_PATH_RE = re.compile(r"/company/([^/?#]+)", re.IGNORECASE)


def extract_domain_from_url(url: str) -> Optional[str]:
    """Extract a company domain or slug from a job/profile URL.

    For LinkedIn/Wellfound ``/company/<slug>`` URLs the slug is returned (Apollo
    later resolves the real domain). For ordinary URLs the registrable host is
    returned with a leading ``www.`` stripped.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url if "//" in url else f"https://{url}")
    except ValueError:
        return None

    host = (parsed.netloc or "").lower().removeprefix("www.")

    # Job-board company-slug URLs: linkedin.com/company/novatech -> "novatech"
    if host in {"linkedin.com", "www.linkedin.com", "wellfound.com", "angel.co"}:
        match = _COMPANY_PATH_RE.search(parsed.path)
        if match:
            return match.group(1).lower()

    if host and "." in host:
        return host
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Funding round / amount extraction (for RSS funding headlines)
# ──────────────────────────────────────────────────────────────────────────────
_ROUND_RE = re.compile(
    r"\b(pre-seed|seed|series\s+[a-h]|angel|bridge|debt|growth|pre-series\s+[a-h])\b",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(
    r"(?:\$|₹|€|£|usd|inr|rs\.?)\s?\d[\d.,]*\s?"
    r"(?:million|billion|thousand|mn|bn|m|b|k|crore|cr|lakh|lakhs)?",
    re.IGNORECASE,
)


def extract_funding_round(text: str) -> Optional[str]:
    """Extract a normalized funding-round label from text (e.g. ``Series A``)."""
    if not text:
        return None
    match = _ROUND_RE.search(text)
    if not match:
        return None
    return " ".join(part.capitalize() for part in match.group(0).split())


def extract_funding_amount(text: str) -> Optional[str]:
    """Extract a raw funding-amount string from text (e.g. ``$10 million``)."""
    if not text:
        return None
    match = _AMOUNT_RE.search(text)
    return match.group(0).strip() if match else None


# ──────────────────────────────────────────────────────────────────────────────
# Funding-headline company extraction (structural, not NER)
# ──────────────────────────────────────────────────────────────────────────────
# Funding headlines follow a regular shape:  [descriptor] <Company> <verb>
# <amount> [from <investor>] …  Taking the subject *before* the verb yields the
# fundee and excludes investors (which follow "from"). This is far more reliable
# than first-ORG NER, which grabs descriptors, investors, or publisher names.

# Verbs that mark a company raising money. The company is the subject before it.
_FUNDING_VERB_RE = re.compile(
    r"\b(?:raises?|raised|secures?|secured|bags?|bagged|gets?|nets?|netted|"
    r"closes?|closed|lands?|landed|garners?|garnered|raking|rakes?|"
    r"mops?\s+up|picks?\s+up|picked\s+up|pockets?|to\s+raise|"
    r"set\s+to\s+raise|in\s+talks\s+to\s+raise)\b",
    re.IGNORECASE,
)

# Leading editorial tags to strip: "Exclusive:", "Breaking:", "Watch -" …
_LEAD_TAG_RE = re.compile(
    r"^\s*(?:exclusive|breaking|update|watch|report|news|just\s+in|opinion)\s*[:\-–]\s*",
    re.IGNORECASE,
)

# Sector / descriptor words that are never a company name on their own and mark
# the boundary of a leading descriptor phrase ("AI Startup <Company> raises …").
_DESCRIPTOR_WORDS = {
    "ai", "ml", "genai", "saas", "d2c", "b2b", "b2c", "fintech", "edtech",
    "healthtech", "medtech", "agritech", "insurtech", "proptech", "deeptech",
    "cleantech", "foodtech", "spacetech", "biotech", "ev", "fmcg", "consumer",
    "startup", "startups", "company", "firm", "brand", "platform", "app",
    "maker", "player", "unicorn", "giant", "major", "venture", "ventures",
    "group", "arm", "label", "chain", "network", "studio", "lab", "labs",
    "indian", "india", "india's", "asia's", "world's", "country's", "video",
    "generation", "perfume", "beauty", "skincare", "logistics", "mobility",
    "gaming", "crypto", "web3", "online", "digital", "tech", "software",
    "funding", "round",  # title-phrase words ("X Funding Round: …") — never a name part
}

# Trailing title words to strip off an extracted name ("Sarvam AI Funding Round" → "Sarvam AI").
_TRAILING_JUNK = {"funding", "round"}

# If the WHOLE extracted name is one of these, it's not a real company.
_NON_COMPANY = {
    "vc", "vcs", "news", "funding", "report", "investors", "investor",
    "startup", "startups", "india", "indian", "market", "markets", "round",
    "the", "this", "these", "more", "new",
}

_AFFIX_RE = re.compile(r".+-(based|backed|led|owned|focused|founded|run)$", re.IGNORECASE)

# Aggregate / market-roundup headlines that are NOT about a single company.
_AGGREGATE_RE = re.compile(
    r"(^\s*between\b|\bas many as\b|\bthis week\b|funding and acquisitions|"
    r"\b\d+\s+(?:indian\s+)?start-?ups?\b|"
    r"\bstart-?ups?\s+(?:raised|raise|rake|raked|mopped|garnered|secured)\b|"
    r"weekly\s+funding|funding\s+round-?up|funding\s+wrap|funding\s+tracker|\bdeal\s+book\b)",
    re.IGNORECASE,
)

_QUOTE_CHARS = " ,.&'’\""


def _is_descriptor(low: str) -> bool:
    """True when a (lowercased) token is a sector/descriptor or an affix word."""
    return low in _DESCRIPTOR_WORDS or bool(_AFFIX_RE.match(low))


def _name_from_subject(subject: str) -> Optional[str]:
    """Collect the company name from the subject phrase before the funding verb.

    A descriptor word (e.g. "Startup", "AI") only ends the run *after* a real
    proper noun has been seen, so names ending in a sector word ("TrueFan AI")
    survive while leading descriptors ("AI Startup TrueFan") are stripped.
    """
    subject = subject.strip(" ,–-:")
    if not subject:
        return None
    tokens = subject.split()
    name_tokens: list[str] = []  # collected right-to-left
    seen_strong = False
    for tok in reversed(tokens):
        bare = tok.strip(_QUOTE_CHARS)
        if not bare:
            continue
        low = bare.lower().rstrip(".")
        strong = (not _is_descriptor(low)) and any(c.isupper() for c in bare)
        if _is_descriptor(low):
            if seen_strong:
                break
            name_tokens.append(bare)
            if len(name_tokens) >= 4:
                break
            continue
        if name_tokens and not strong and bare[:1].islower():
            break
        if strong:
            seen_strong = True
        name_tokens.append(bare)
        if len(name_tokens) >= 4:
            break

    if not seen_strong:
        if len(tokens) == 1:
            only = tokens[0].strip(_QUOTE_CHARS)
            if only.lower() not in _DESCRIPTOR_WORDS and only.lower() not in _NON_COMPANY and len(only) > 1:
                return only
        return None

    parts = " ".join(reversed(name_tokens)).strip(_QUOTE_CHARS).split()
    while parts and parts[0].lower() in _DESCRIPTOR_WORDS:        # trim leading descriptors
        parts.pop(0)
    while parts and parts[-1].lower().rstrip(".:,") in _TRAILING_JUNK:  # trim trailing "Funding Round"
        parts.pop()
    name = " ".join(parts).strip(_QUOTE_CHARS)
    if not name or name.lower() in _NON_COMPANY or len(name) < 2:
        return None
    return name


def extract_funding_company(headline: str) -> Optional[str]:
    """Extract the funded company from a funding-news headline.

    Strips editorial tags, rejects market round-ups (incl. "Funding Wrap"),
    splits on the funding verb to isolate the subject (excluding investors named
    after "from"), and pulls the company name. Title-style headlines where the
    company precedes a colon ("TIST Media: India-Based Startup Raises…",
    "Sarvam AI Funding Round: $350M Raise…") are handled by trying the pre-colon
    segment first. Returns ``None`` when it's not a single company raising.
    """
    if not headline:
        return None
    text = _LEAD_TAG_RE.sub("", headline).strip()
    if _AGGREGATE_RE.search(text):
        return None

    verb = _FUNDING_VERB_RE.search(text)
    if not verb:
        return None  # not a single-company raise event
    subject = text[: verb.start()].strip(" ,–-:")
    if not subject:
        return None

    # Title-style colon: the company usually sits before it — try that first,
    # but only accept it if it parses to a real name (so "India: … Immuneel
    # Therapeutics raises" still falls through to the full subject).
    if ":" in subject:
        pre = _name_from_subject(subject.split(":", 1)[0])
        if pre:
            return pre
    return _name_from_subject(subject)


# HQ city sits next to "based" / "headquartered" in funding articles
# ("Bengaluru-based X", "Hyderabad headquartered Y", "headquartered in Pune").
_HQ_RE = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)[\s-]{0,3}(?:based|headquartered|head-quartered)\b"
    r"|(?:based|headquartered)\s+(?:in|out\s+of)\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)"
)


def extract_hq_city(text: str) -> Optional[str]:
    """Return a company's HQ city from funding-article text, or ``None``.

    Only accepts cities the offline geocoder recognizes, and only when tied to an
    HQ phrase ("Bengaluru-based", "headquartered in Pune") — so incidental
    mentions ("IIT Delhi alumnus", "India-based", an investor's city) are
    ignored rather than dropping a false map pin.
    """
    if not text:
        return None
    from processors.geocoder import geocode_city

    clean = re.sub(r"<[^>]+>", " ", text)
    for match in _HQ_RE.finditer(clean):
        cand = (match.group(1) or match.group(2) or "").strip()
        cand = re.sub(r"^(?:The|A|An|This|Its|Their)\s+", "", cand, flags=re.IGNORECASE).strip()
        if cand and geocode_city(cand):
            return cand
    return None
