"""Pydantic v2 data models for the lead-intel pipeline.

These models define the contracts that flow between scrapers, processors,
delivery channels, and the API layer. ``RawSignal`` is the loose container that
every scraper emits; ``NormalizedSignal`` is the single canonical shape that all
downstream processing operates on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
# Signal models
# ──────────────────────────────────────────────────────────────────────────────
class RawSignal(BaseModel):
    """Raw, source-specific payload emitted by a scraper before normalization.

    Each scraper stuffs its native record into ``data`` and tags it with the
    ``source_platform`` so the normalizer can dispatch to the right mapper.
    """

    model_config = ConfigDict(extra="allow")

    source_platform: str = Field(..., description="linkedin, wellfound, indeed, reddit, twitter, rss")
    data: dict[str, Any] = Field(default_factory=dict, description="Native scraper record")


class NormalizedSignal(BaseModel):
    """Canonical signal shape consumed by all processors and the database.

    Every scraper's ``normalize`` method must produce this exact schema.
    """

    company_name: str
    company_domain: Optional[str] = None
    signal_type: str = Field(..., description="hiring_post, funding_news, outsource_intent, agency_switch, growth_pain, general_signal")
    source_platform: str
    raw_text: str
    source_url: str
    detected_at: datetime = Field(default_factory=_utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Persistence models (mirror the Supabase tables)
# ──────────────────────────────────────────────────────────────────────────────
class Company(BaseModel):
    """A company record. ``domain`` is the deduplication key."""

    id: Optional[UUID] = None
    name: str
    domain: Optional[str] = None
    employee_count: Optional[int] = None
    industry: Optional[str] = None
    hq_country: Optional[str] = None
    description: Optional[str] = None
    tech_stack: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Signal(BaseModel):
    """A persisted signal tied to a company."""

    id: Optional[UUID] = None
    company_id: Optional[UUID] = None
    signal_type: str
    source_platform: str
    raw_text: Optional[str] = None
    source_url: Optional[str] = None
    detected_at: datetime = Field(default_factory=_utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Lead(BaseModel):
    """A scored, client-scoped lead ready for delivery."""

    id: Optional[UUID] = None
    company_id: Optional[UUID] = None
    client_id: Optional[UUID] = None
    vertical: str
    contact_name: Optional[str] = None
    contact_role: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    score: int = 0
    score_breakdown: dict[str, Any] = Field(default_factory=dict)
    status: str = "new"
    created_at: Optional[datetime] = None


class Client(BaseModel):
    """A tenant agency consuming leads from the pipeline."""

    id: Optional[UUID] = None
    name: str
    vertical: str
    slack_webhook: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    email_to: Optional[str] = None
    filters: dict[str, Any] = Field(default_factory=dict)
    min_score: int = 60
    plan: str = "trial"
    active: bool = True
    created_at: Optional[datetime] = None


# ──────────────────────────────────────────────────────────────────────────────
# Rich lead card (the user-facing lead format)
# ──────────────────────────────────────────────────────────────────────────────
class SignalCard(BaseModel):
    """One signal as shown on a lead card: what, when, where, and a snippet.

    This is the "date + type of fetch + link of the post + snippet" view the
    product surfaces for every piece of evidence behind a lead.
    """

    signal_type: str
    source_platform: str
    detected_at: Optional[str] = None  # ISO date string for display
    source_url: Optional[str] = None
    snippet: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LeadCard(BaseModel):
    """A fully-formed, display-ready lead aggregating all evidence for a company.

    This is the canonical artifact the dashboard renders and the delivery layer
    sends. It is storage-agnostic: the JSON store writes it as-is and the Supabase
    store maps it onto the ``leads`` table.
    """

    # Identity
    company_name: str
    company_domain: Optional[str] = None
    website: Optional[str] = None
    location: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None

    # Scoring
    vertical: str
    score: int = 0
    score_breakdown: dict[str, Any] = Field(default_factory=dict)
    meets_threshold: bool = False
    summary: str = ""
    # One-line AI verdict from the qualify gate ("why this is a lead").
    ai_summary: Optional[str] = None

    # Signal profile
    has_hiring: bool = False
    has_funding: bool = False
    has_social: bool = False
    hiring_role_count: int = 0
    hiring_recency_days: Optional[int] = None
    funding_recency_days: Optional[int] = None
    funding_round: Optional[str] = None
    funding_amount: Optional[str] = None
    signal_sources: list[str] = Field(default_factory=list)
    signal_types: list[str] = Field(default_factory=list)
    signals: list[SignalCard] = Field(default_factory=list)

    # Contact (optional Apollo enrichment)
    contact_name: Optional[str] = None
    contact_role: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None

    # Local business (Google Maps leads) — None for intent verticals.
    phone: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    category: Optional[str] = None
    address: Optional[str] = None
    maps_url: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None

    # LinkedIn People (person leads) — None for company leads.
    followers: Optional[int] = None

    # Lifecycle
    status: str = "new"
    created_at: str = Field(default_factory=lambda: _utcnow().isoformat())


# ──────────────────────────────────────────────────────────────────────────────
# API request/response schemas
# ──────────────────────────────────────────────────────────────────────────────
class ClientCreate(BaseModel):
    """Payload for ``POST /clients``."""

    name: str
    vertical: str
    slack_webhook: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    email_to: Optional[str] = None
    filters: dict[str, Any] = Field(default_factory=dict)
    min_score: int = 60
    plan: str = "trial"
