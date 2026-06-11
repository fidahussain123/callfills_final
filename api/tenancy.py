"""Multi-tenant scoping — resolve a logged-in user to their client + ICP.

This is the "Shared-by-ICP" model (see ``docs/dynamic-icp-targeting.md``): there
is one scraped lead pool, and each client sees a live *slice* of it defined by
their vertical + filters + min_score. A user maps to a client via the Supabase
``profiles`` table; operators (the platform owner) see the whole pool.

Everything degrades gracefully: if Supabase isn't configured or migrations
haven't been run yet, the dashboard falls back to the operator (see-all) view so
nothing breaks during setup.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from config import settings

logger = logging.getLogger("lead-intel.api.tenancy")

# Cache resolved tenants briefly so HTMX keystrokes don't hit Supabase each time.
_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_TTL = 60  # seconds


def _operator(role: str = "operator") -> dict[str, Any]:
    """The see-all view used by the platform owner / admins."""
    return {
        "is_operator": True,
        "client": None,
        "client_name": "All leads",
        "role": role,
        "needs_workspace": False,
    }


def _member(profile: dict[str, Any], client: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        "is_operator": False,
        "client": client,
        "client_name": (client or {}).get("name"),
        "role": profile.get("role") or "member",
        "needs_workspace": client is None,
    }


def _resolve(user: dict[str, Any], email: str) -> dict[str, Any]:
    """Compute the tenant context for a user (uncached)."""
    # No Supabase → single-operator JSON mode.
    if not settings.auth_ready:
        return _operator()
    # Explicitly-configured operators always see everything.
    if email and email in settings.OPERATOR_EMAILS:
        return _operator()
    uid = user.get("id")
    if not uid or uid == "anonymous":
        return _operator()

    try:
        from db import supabase_client as db

        profile = db.get_profile(uid)
    except Exception as exc:  # noqa: BLE001 - never break the dashboard
        logger.debug("Profile lookup failed (%s); defaulting to operator", exc)
        profile = None

    if profile is None:
        # Table missing / no row. If tenancy isn't configured yet (no operator
        # emails set), we're bootstrapping → operator. Once tenancy is live,
        # an unknown user gets an empty workspace rather than the whole pool.
        return _operator() if not settings.OPERATOR_EMAILS else _member({}, None)

    if (profile.get("role") or "").lower() == "admin":
        return _operator(role="admin")

    client_id = profile.get("client_id")
    if not client_id:
        return _member(profile, None)
    try:
        from db import supabase_client as db

        client = db.get_client_by_id(str(client_id))
    except Exception:  # noqa: BLE001
        client = None
    return _member(profile, client)


def resolve_tenant(user: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return the tenant context for ``user`` (cached for ``_TTL`` seconds)."""
    user = user or {}
    uid = user.get("id")
    now = time.time()
    if uid:
        cached = _CACHE.get(uid)
        if cached and cached[1] > now:
            return cached[0]
    tenant = _resolve(user, (user.get("email") or "").lower())
    if uid:
        _CACHE[uid] = (tenant, now + _TTL)
    return tenant


def invalidate(user_id: Optional[str]) -> None:
    """Drop a cached tenant (call after reassigning a user's client)."""
    if user_id:
        _CACHE.pop(user_id, None)


def apply_icp(leads: list[dict[str, Any]], client: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter the shared lead pool down to a client's ICP.

    Filters present on the client narrow the pool; absent ones don't. When a
    lead lacks a field the ICP filters on (e.g. industry, before enrichment is
    enabled), the lead is kept rather than dropped, so the dashboard isn't empty
    while that data is sparse.
    """
    if not client:
        return leads
    vertical = client.get("vertical")
    min_score = client.get("min_score")
    f = client.get("filters") or {}
    from processors.geocoder import canonical_city

    industries = {s.lower() for s in (f.get("industries") or [])}
    cities = {s.lower() for s in ((f.get("cities") or []) + (f.get("locations") or []))}
    # Canonical keys of the target cities, so "Delhi" also matches "New Delhi" and
    # "Bangalore" matches "Bengaluru" without spelling-variant misses.
    city_canons = {c for c in (canonical_city(s) for s in cities) if c}
    keywords = [s.lower() for s in (f.get("keywords") or [])]
    signal_types = set(f.get("signal_types") or [])
    emp_min, emp_max = f.get("employee_min"), f.get("employee_max")
    want_funding, want_hiring = f.get("has_funding"), f.get("has_hiring")

    out: list[dict[str, Any]] = []
    for lead in leads:
        if vertical and lead.get("vertical") != vertical:
            continue
        if min_score is not None and (lead.get("score") or 0) < min_score:
            continue
        if want_funding is not None and bool(lead.get("has_funding")) != bool(want_funding):
            continue
        if want_hiring is not None and bool(lead.get("has_hiring")) != bool(want_hiring):
            continue
        if signal_types and not (set(lead.get("signal_types") or []) & signal_types):
            continue
        if industries:
            ind = (lead.get("industry") or "").lower()
            if ind and ind not in industries:
                continue
        if cities:
            loc = (lead.get("location") or "").lower()
            # Only filter leads that actually carry a location; an alias-aware
            # canonical match ("New Delhi" -> delhi) with a raw-substring fallback.
            if loc:
                loc_canon = canonical_city(loc)
                matched = (loc_canon and loc_canon in city_canons) or any(
                    c in loc for c in cities
                )
                if not matched:
                    continue
        if keywords:
            hay = " ".join(
                str(lead.get(k) or "") for k in ("company_name", "website", "location", "summary")
            ).lower()
            if not any(k in hay for k in keywords):
                continue
        emp = lead.get("employee_count")
        if emp is not None:
            if emp_min is not None and emp < emp_min:
                continue
            if emp_max is not None and emp > emp_max:
                continue
        out.append(lead)
    return out


def scope_leads(tenant: dict[str, Any], leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scope a lead list to the tenant: all for operators, ICP slice for clients."""
    if tenant.get("is_operator"):
        return leads
    if tenant.get("needs_workspace"):
        return []
    return apply_icp(leads, tenant.get("client"))


def assign_user_to_client(email: str, client_id: Optional[str]) -> tuple[bool, Optional[str]]:
    """Assign a user (by email) to a client. Returns (ok, error_message).

    Handles users who signed up before the auto-profile trigger existed: if no
    profile row is found but the Supabase Auth account exists, the profile is
    created on the fly, then assigned.
    """
    from db import supabase_client as db

    email_n = email.strip().lower()
    profile = db.get_profile_by_email(email_n)
    if not profile:
        auth_user = db.get_auth_user_by_email(email_n)
        if not auth_user or not auth_user.get("id"):
            return False, "No account with that email — they must sign up first."
        db.ensure_profile(auth_user["id"], email_n, auth_user.get("name"))
        profile = {"id": auth_user["id"]}
    ok = db.update_profile_client(profile["id"], client_id)
    invalidate(profile["id"])
    return (ok, None) if ok else (False, "Could not update the user's workspace.")
