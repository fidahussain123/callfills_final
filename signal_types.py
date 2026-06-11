"""Canonical signal taxonomy — the single source of truth for signal types.

Every scraper emits one of these canonical types and every processor reasons
about them through the helper sets/functions here. Centralizing the taxonomy is
what lets new sources and new buyer verticals plug in without touching core
logic: a scraper just declares which canonical type it produces, and the scorer
weights types it recognizes.

Legacy aliases (e.g. the old ``funding_news``) are mapped to their canonical
form by :func:`canonical` so historical data keeps working.
"""

from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
# Canonical signal types
# ──────────────────────────────────────────────────────────────────────────────
# Hiring intent
HIRING_POST = "hiring_post"          # job-board listing (LinkedIn, Naukri, Indeed, Wellfound)
HIRING_AD = "hiring_ad"              # paid recruitment ad (Meta Ad Library)

# Funding / growth intent
FUNDING_ROUND = "funding_round"      # funding announcement (RSS, news)
PRODUCT_LAUNCH = "product_launch"    # new product / GA launch
OFFICE_EXPANSION = "office_expansion"  # new office / market expansion
LEADERSHIP_CHANGE = "leadership_change"  # new exec hire (VP Eng, CTO…)
TECH_ADOPTION = "tech_adoption"      # adopting a new tool/stack

# Social / unstructured intent
SOCIAL_MENTION = "social_mention"    # generic social post (Reddit, Twitter)
OUTSOURCE_INTENT = "outsource_intent"  # looking to outsource dev/recruiting
AGENCY_SWITCH = "agency_switch"      # dissatisfied with current agency
GROWTH_PAIN = "growth_pain"          # scaling pain expressed publicly

# Local business (Google Maps listing — a real, contactable business)
LOCAL_BUSINESS = "local_business"

# B2B supplier listing (IndiaMART/TradeIndia — a contactable supplier)
SUPPLIER_LISTING = "supplier_listing"

# Fallback
GENERAL_SIGNAL = "general_signal"

# ──────────────────────────────────────────────────────────────────────────────
# Groupings used by cross-reference + scoring
# ──────────────────────────────────────────────────────────────────────────────
#: Types that indicate a company is actively hiring.
HIRING_TYPES: frozenset[str] = frozenset({HIRING_POST, HIRING_AD})

#: Types that indicate fresh capital / growth (a precursor to hiring).
FUNDING_TYPES: frozenset[str] = frozenset({FUNDING_ROUND})

#: Types that indicate buyer intent specific to agency/outsourcing verticals.
INTENT_TYPES: frozenset[str] = frozenset(
    {OUTSOURCE_INTENT, AGENCY_SWITCH, GROWTH_PAIN}
)

#: Types that come from unstructured social text (need NER + classification).
SOCIAL_TYPES: frozenset[str] = frozenset(
    {SOCIAL_MENTION, OUTSOURCE_INTENT, AGENCY_SWITCH, GROWTH_PAIN}
)

#: Types from business directories (Google Maps, IndiaMART) — real, contactable
#: businesses with no "posted at" freshness, so quality gates treat them as live.
LOCAL_TYPES: frozenset[str] = frozenset({LOCAL_BUSINESS, SUPPLIER_LISTING})

#: All recognized canonical types.
ALL_TYPES: frozenset[str] = frozenset(
    {
        HIRING_POST,
        HIRING_AD,
        FUNDING_ROUND,
        PRODUCT_LAUNCH,
        OFFICE_EXPANSION,
        LEADERSHIP_CHANGE,
        TECH_ADOPTION,
        SOCIAL_MENTION,
        OUTSOURCE_INTENT,
        AGENCY_SWITCH,
        GROWTH_PAIN,
        LOCAL_BUSINESS,
        SUPPLIER_LISTING,
        GENERAL_SIGNAL,
    }
)

# ──────────────────────────────────────────────────────────────────────────────
# Legacy aliases → canonical
# ──────────────────────────────────────────────────────────────────────────────
_ALIASES: dict[str, str] = {
    "funding_news": FUNDING_ROUND,
    "hiring": HIRING_POST,
    "social": SOCIAL_MENTION,
}


def canonical(signal_type: str | None) -> str:
    """Return the canonical form of ``signal_type``.

    Maps legacy aliases (``funding_news`` → ``funding_round``) forward and leaves
    unknown values untouched (so we never silently drop data).
    """
    if not signal_type:
        return GENERAL_SIGNAL
    value = signal_type.strip()
    return _ALIASES.get(value, value)


def is_hiring(signal_type: str | None) -> bool:
    """True if the (canonicalized) type counts as a hiring signal."""
    return canonical(signal_type) in HIRING_TYPES


def is_funding(signal_type: str | None) -> bool:
    """True if the (canonicalized) type counts as a funding/growth signal."""
    return canonical(signal_type) in FUNDING_TYPES


def is_local(signal_type: str | None) -> bool:
    """True if the (canonicalized) type is a local-business listing."""
    return canonical(signal_type) in LOCAL_TYPES
