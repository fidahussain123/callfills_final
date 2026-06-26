"""Supabase client initialization and high-level persistence helpers.

The service-role key is used so the pipeline bypasses Row Level Security; never
expose this client to untrusted callers.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Optional

from supabase import Client as SupabaseClient
from supabase import create_client

from config import settings

logger = logging.getLogger("lead-intel.db")


@lru_cache(maxsize=1)
def get_client() -> SupabaseClient:
    """Return a cached Supabase client built from the service-role key."""
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)


# ──────────────────────────────────────────────────────────────────────────────
# Companies
# ──────────────────────────────────────────────────────────────────────────────
def upsert_company(company: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Insert or update a company by ``domain`` and return the stored row.

    On conflict with an existing domain the row is updated (this also refreshes
    ``updated_at`` through the DEFAULT/trigger). Companies without a domain are
    inserted as new rows since they cannot be deduplicated.
    """
    client = get_client()
    try:
        payload = {k: v for k, v in company.items() if v is not None}
        if payload.get("domain"):
            res = (
                client.table("companies")
                .upsert(payload, on_conflict="domain")
                .execute()
            )
        else:
            res = client.table("companies").insert(payload).execute()
        return res.data[0] if res.data else None
    except Exception as exc:  # noqa: BLE001 - log and continue pipeline
        logger.error("upsert_company failed for %s: %s", company.get("name"), exc)
        return None


def get_company_by_domain(domain: str) -> Optional[dict[str, Any]]:
    """Fetch a single company row by domain, or ``None`` if not found."""
    client = get_client()
    try:
        res = (
            client.table("companies")
            .select("*")
            .eq("domain", domain)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:  # noqa: BLE001
        logger.error("get_company_by_domain failed for %s: %s", domain, exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Signals
# ──────────────────────────────────────────────────────────────────────────────
def insert_signal(signal: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Insert a signal, silently skipping if its ``source_url`` already exists."""
    client = get_client()
    try:
        payload = {k: v for k, v in signal.items() if v is not None}
        res = (
            client.table("signals")
            .upsert(payload, on_conflict="source_url", ignore_duplicates=True)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:  # noqa: BLE001
        logger.error("insert_signal failed (%s): %s", signal.get("source_url"), exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Leads
# ──────────────────────────────────────────────────────────────────────────────
def insert_lead(lead: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Insert a lead row and return it."""
    client = get_client()
    try:
        payload = {k: v for k, v in lead.items() if v is not None}
        res = client.table("leads").insert(payload).execute()
        return res.data[0] if res.data else None
    except Exception as exc:  # noqa: BLE001
        logger.error("insert_lead failed: %s", exc)
        return None


def lead_exists_today(company_id: str, client_id: str) -> bool:
    """Return True if a lead for this company+client was already created today."""
    client = get_client()
    try:
        from datetime import date

        today = date.today().isoformat()
        res = (
            client.table("leads")
            .select("id")
            .eq("company_id", company_id)
            .eq("client_id", client_id)
            .gte("created_at", f"{today}T00:00:00")
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception as exc:  # noqa: BLE001
        logger.error("lead_exists_today failed: %s", exc)
        return False


def get_leads(
    client_id: str,
    status: Optional[str] = None,
    min_score: Optional[int] = None,
    vertical: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return leads for a client, filtered and sorted by score descending."""
    client = get_client()
    try:
        query = client.table("leads").select("*").eq("client_id", client_id)
        if status:
            query = query.eq("status", status)
        if min_score is not None:
            query = query.gte("score", min_score)
        if vertical:
            query = query.eq("vertical", vertical)
        res = (
            query.order("score", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("get_leads failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Lead pool (shared, client-agnostic generated pool the dashboard renders)
# ──────────────────────────────────────────────────────────────────────────────
def _pool_row(lead: dict[str, Any]) -> dict[str, Any]:
    """Map a LeadCard dict to a ``lead_pool`` row (promoted cols + full card)."""
    return {
        "company_name": lead.get("company_name") or "",
        "vertical": lead.get("vertical") or "",
        "company_domain": lead.get("company_domain"),
        "location": lead.get("location"),
        "score": int(lead.get("score") or 0),
        "has_hiring": bool(lead.get("has_hiring")),
        "has_funding": bool(lead.get("has_funding")),
        "meets_threshold": bool(lead.get("meets_threshold")),
        "card": lead,
    }


def replace_pool_leads(verticals: Any, leads: list[dict[str, Any]]) -> int:
    """Upsert ``leads`` into ``lead_pool`` and drop now-absent rows per vertical.

    Mirrors the JSON store's per-vertical merge: rows for OTHER verticals are
    left untouched, and for each vertical in this run the pool ends up exactly
    matching the new set (upsert refreshes, delete-stale removes companies that
    no longer appear). Returns the number of rows written. Fail-open: returns 0
    and logs on any error so the run never aborts on a persistence hiccup.
    """
    client = get_client()
    # Dedupe by (company_name, vertical), keeping the highest-scored copy.
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for lead in leads:
        row = _pool_row(lead)
        if not row["company_name"] or not row["vertical"]:
            continue
        key = (row["company_name"], row["vertical"])
        cur = best.get(key)
        if cur is None or row["score"] > cur["score"]:
            best[key] = row
    rows = list(best.values())
    try:
        if rows:
            client.table("lead_pool").upsert(
                rows, on_conflict="company_name,vertical"
            ).execute()
        for vertical in verticals:
            names = [r["company_name"] for r in rows if r["vertical"] == vertical]
            q = client.table("lead_pool").delete().eq("vertical", vertical)
            if names:  # keep the refreshed rows, drop the vanished ones
                q = q.not_.in_("company_name", names)
            q.execute()
        logger.info("lead_pool: wrote %d rows (verticals=%s)", len(rows), sorted(set(verticals)))
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        logger.error("replace_pool_leads failed: %s", exc)
        return 0


def get_pool_leads(limit: int = 2000) -> list[dict[str, Any]]:
    """Return full LeadCards from the shared pool, best score first."""
    client = get_client()
    try:
        res = (
            client.table("lead_pool")
            .select("card")
            .order("score", desc=True)
            .limit(limit)
            .execute()
        )
        return [r["card"] for r in (res.data or []) if isinstance(r.get("card"), dict)]
    except Exception as exc:  # noqa: BLE001
        logger.error("get_pool_leads failed: %s", exc)
        return []


def get_recent_leads_for_client(client_id: str, hours: int = 24) -> list[dict[str, Any]]:
    """Return leads created for a client within the last ``hours`` hours."""
    client = get_client()
    try:
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = (
            client.table("leads")
            .select("*")
            .eq("client_id", client_id)
            .gte("created_at", since)
            .order("score", desc=True)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("get_recent_leads_for_client failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Clients
# ──────────────────────────────────────────────────────────────────────────────
def get_active_clients() -> list[dict[str, Any]]:
    """Return all active client rows."""
    client = get_client()
    try:
        res = client.table("clients").select("*").eq("active", True).execute()
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("get_active_clients failed: %s", exc)
        return []


def create_client_row(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Insert a new client row and return it."""
    client = get_client()
    try:
        data = {k: v for k, v in payload.items() if v is not None}
        res = client.table("clients").insert(data).execute()
        return res.data[0] if res.data else None
    except Exception as exc:  # noqa: BLE001
        logger.error("create_client_row failed: %s", exc)
        return None


def get_client_by_id(client_id: str) -> Optional[dict[str, Any]]:
    """Fetch a single client row by id."""
    client = get_client()
    try:
        res = (
            client.table("clients").select("*").eq("id", client_id).limit(1).execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:  # noqa: BLE001
        logger.error("get_client_by_id failed: %s", exc)
        return None


def delete_client_row(client_id: str) -> bool:
    """Delete a client (pipeline) row. Leads cascade; profiles unassign (SET NULL)."""
    client = get_client()
    try:
        client.table("clients").delete().eq("id", client_id).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("delete_client_row failed: %s", exc)
        return False


def list_clients() -> list[dict[str, Any]]:
    """Return every client row (active and inactive), newest first."""
    client = get_client()
    try:
        res = client.table("clients").select("*").order("created_at", desc=True).execute()
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("list_clients failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Profiles (tenancy: maps a Supabase Auth user to a client + role)
# ──────────────────────────────────────────────────────────────────────────────
def get_profile(user_id: str) -> Optional[dict[str, Any]]:
    """Fetch a profile row by auth user id, or ``None``."""
    client = get_client()
    try:
        res = (
            client.table("profiles").select("*").eq("id", user_id).limit(1).execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:  # noqa: BLE001
        logger.error("get_profile failed for %s: %s", user_id, exc)
        return None


def get_profile_by_email(email: str) -> Optional[dict[str, Any]]:
    """Fetch a profile row by email, or ``None``."""
    client = get_client()
    try:
        res = (
            client.table("profiles").select("*").eq("email", email).limit(1).execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:  # noqa: BLE001
        logger.error("get_profile_by_email failed for %s: %s", email, exc)
        return None


def update_profile_client(user_id: str, client_id: Optional[str]) -> bool:
    """Assign (or clear) a profile's client_id. Returns success."""
    client = get_client()
    try:
        client.table("profiles").update({"client_id": client_id}).eq(
            "id", user_id
        ).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("update_profile_client failed for %s: %s", user_id, exc)
        return False


def get_profiles_for_client(client_id: str) -> list[dict[str, Any]]:
    """Return the profiles (users) assigned to a client/pipeline."""
    client = get_client()
    try:
        res = (
            client.table("profiles").select("*").eq("client_id", client_id).execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("get_profiles_for_client failed for %s: %s", client_id, exc)
        return []


def get_auth_user_by_email(email: str) -> Optional[dict[str, Any]]:
    """Find a Supabase Auth user by email — used to assign pre-trigger signups."""
    client = get_client()
    target = (email or "").strip().lower()
    try:
        res = client.auth.admin.list_users()
        users = res if isinstance(res, list) else getattr(res, "users", res)
        for u in users or []:
            if (getattr(u, "email", None) or "").lower() == target:
                meta = getattr(u, "user_metadata", None) or {}
                name = (meta.get("full_name") or meta.get("name")) if isinstance(meta, dict) else None
                return {"id": getattr(u, "id", None), "email": target, "name": name}
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("get_auth_user_by_email failed for %s: %s", email, exc)
        return None


def ensure_profile(user_id: str, email: str, full_name: Optional[str] = None) -> bool:
    """Create a profile row if one doesn't exist yet (no-op when it does)."""
    client = get_client()
    try:
        client.table("profiles").upsert(
            {"id": user_id, "email": email, "full_name": full_name},
            on_conflict="id",
            ignore_duplicates=True,
        ).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("ensure_profile failed for %s: %s", user_id, exc)
        return False


def get_companies(
    has_funding: Optional[bool] = None,
    has_hiring: Optional[bool] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return companies with their signals attached (best-effort join)."""
    client = get_client()
    try:
        res = (
            client.table("companies")
            .select("*, signals(*)")
            .limit(limit)
            .execute()
        )
        rows = res.data or []
        # has_funding / has_hiring are applied in Python against joined signals.
        def _matches(row: dict[str, Any]) -> bool:
            from signal_types import canonical, is_funding, is_hiring

            sigs = row.get("signals") or []
            types = {canonical(s.get("signal_type")) for s in sigs}
            if has_funding and not any(is_funding(t) for t in types):
                return False
            if has_hiring and not any(is_hiring(t) for t in types):
                return False
            return True

        return [r for r in rows if _matches(r)]
    except Exception as exc:  # noqa: BLE001
        logger.error("get_companies failed: %s", exc)
        return []
