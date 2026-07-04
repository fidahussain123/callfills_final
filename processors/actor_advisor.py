"""AI Scraper Advisor — pick a source/need, get the best Apify actors + costs.

Flow: a query (e.g. "google maps businesses in bangalore" or "linkedin
employees") → live Apify Store search → normalize each actor's real pricing and
stats → Groq ranks them for the need and picks the best. Powers the pipeline
form's "which scraper should I use?" step so operators choose grounded in real
actors, real costs, and an AI recommendation — not guesswork.

Fail-open everywhere: if the Store API or Groq is unavailable, we degrade to a
popularity heuristic so the panel still returns something useful.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from config import settings

logger = logging.getLogger("lead-intel.processors.actor_advisor")

_STORE_URL = "https://api.apify.com/v2/store"
_ACTS_URL = "https://api.apify.com/v2/acts"
_TIMEOUT = 25.0


def _is_search_field(name: str, title: str) -> bool:
    """Heuristic: is this the actor's keyword/search input?"""
    t = f"{name} {title}".lower()
    return any(k in t for k in (
        "search", "query", "queries", "keyword", "term", "searchstring", "job title",
    ))


def fetch_actor_schema(actor_id: str) -> dict[str, Any]:
    """Fetch an actor's live INPUT schema so the operator can see exactly what it
    searches on (and which field their keywords feed) before scraping.

    Two Apify REST calls: the actor (for title/desc + latest build id) then that
    build (for the input schema). Fail-open: returns ``{"error": ...}``.
    """
    if not (settings.APIFY_API_TOKEN and (actor_id or "").strip()):
        return {"id": actor_id, "error": "Apify token or actor id missing."}
    aid = actor_id.strip().replace("/", "~")
    # Token in the Authorization header (not the query string) so it never lands
    # in a URL — and therefore never in an error message / log line.
    headers = {"Authorization": f"Bearer {settings.APIFY_API_TOKEN}"}
    try:
        act_resp = httpx.get(f"{_ACTS_URL}/{aid}", headers=headers, timeout=_TIMEOUT)
        act_resp.raise_for_status()
        act = act_resp.json().get("data") or {}
        build_id = ((act.get("taggedBuilds") or {}).get("latest") or {}).get("buildId")
        props: dict[str, Any] = {}
        required: list[str] = []
        if build_id:
            build_resp = httpx.get(f"{_ACTS_URL}/{aid}/builds/{build_id}",
                                   headers=headers, timeout=_TIMEOUT)
            build_resp.raise_for_status()
            build = build_resp.json().get("data") or {}
            schema = build.get("inputSchema")
            if isinstance(schema, str):
                schema = json.loads(schema)
            if isinstance(schema, dict):
                props = schema.get("properties") or {}
                required = schema.get("required") or []
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.error("actor schema fetch failed for %s: %s", actor_id, exc)
        return {"id": actor_id, "error": "Couldn't load this actor's fields right now."}

    fields: list[dict[str, Any]] = []
    search_fields: list[str] = []
    for name, p in props.items():
        if not isinstance(p, dict):
            continue
        title = p.get("title") or name
        is_search = _is_search_field(name, title)
        if is_search:
            search_fields.append(title)
        enum = p.get("enum")
        fields.append({
            "name": name,
            "title": title,
            "type": p.get("type"),
            "desc": " ".join((p.get("description") or "").split())[:150],
            "is_search": is_search,
            "required": name in required,
            "options": [str(e) for e in enum[:6]] if isinstance(enum, list) else None,
        })
    # Search fields first, then required, then the rest.
    fields.sort(key=lambda f: (not f["is_search"], not f["required"]))
    return {
        "id": actor_id,
        "title": act.get("title") or act.get("name") or actor_id,
        "description": " ".join((act.get("description") or "").split())[:240],
        "url": f"https://apify.com/{actor_id}",
        "fields": fields,
        "search_fields": search_fields,
        "field_count": len(fields),
    }


def _price_summary(actor: dict[str, Any]) -> tuple[str, Optional[float]]:
    """Human price + best-effort per-result USD. Handles Apify's pricing models."""
    pi = actor.get("currentPricingInfo") or {}
    model = pi.get("pricingModel")
    if model == "FREE":
        return ("Free", 0.0)
    if model in ("PRICE_PER_DATASET_ITEM", "PAY_PER_DATASET_ITEM"):
        unit = pi.get("pricePerUnitUsd") or pi.get("unitPriceUsd")
        if unit is not None:
            return (f"${unit:.4f}/result", float(unit))
        return ("per result", None)
    if model == "PAY_PER_EVENT":
        events = (pi.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}
        prices: list[float] = []
        for ev in events.values():
            tier = (ev.get("eventTieredPricingUsd") or {}).get("FREE") or {}
            p = tier.get("tieredEventPriceUsd")
            if p is None:
                p = ev.get("eventPriceUsd")
            if isinstance(p, (int, float)):
                prices.append(float(p))
        if prices:
            lo = min(prices)
            return (f"~${lo:.4f}/result", lo)
        return ("pay per use", None)
    if model == "FLAT_PRICE_PER_MONTH":
        m = pi.get("pricePerUnitUsd") or pi.get("flatPricePerMonthUsd")
        return (f"${m}/mo rental" if m else "monthly rental", None)
    return (model or "—", None)


def search_store(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Search the Apify Store for a need; return normalized actor dicts."""
    if not (settings.APIFY_API_TOKEN and (query or "").strip()):
        return []
    try:
        resp = httpx.get(
            _STORE_URL,
            params={"search": query.strip(), "limit": limit},
            headers={"Authorization": f"Bearer {settings.APIFY_API_TOKEN}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("data", {}).get("items", [])
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.error("Apify store search failed for %r: %s", query, exc)
        return []

    out: list[dict[str, Any]] = []
    for a in items:
        stats = a.get("stats") or {}
        display, per_result = _price_summary(a)
        cost_1k = round(per_result * 1000, 2) if per_result is not None else None
        raw_rating = a.get("actorReviewRating")
        rating = round(float(raw_rating), 1) if isinstance(raw_rating, (int, float)) else None
        out.append({
            "id": f"{a.get('username')}/{a.get('name')}",
            "title": (a.get("title") or a.get("name") or "").strip(),
            "desc": (a.get("description") or "").strip()[:160],
            "users": stats.get("totalUsers") or 0,
            "rating": rating,
            "reviews": a.get("actorReviewCount") or 0,
            "price_display": display,
            "per_result": per_result,
            "cost_per_1k": cost_1k,
            "url": a.get("url") or f"https://apify.com/{a.get('username')}/{a.get('name')}",
        })
    return out


def _heuristic_rank(actors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Popularity × rating fallback ranking."""
    return sorted(
        actors,
        key=lambda x: (x.get("users") or 0) * (float(x.get("rating") or 3.5)),
        reverse=True,
    )


def _ai_rank(query: str, actors: list[dict[str, Any]]) -> tuple[Optional[str], str]:
    """Ask Groq to pick the best actor for the need. Returns (best_id, note)."""
    if not settings.ai_verify_ready or not actors:
        return (None, "")
    brief = [
        {"id": a["id"], "title": a["title"], "users": a["users"],
         "rating": a["rating"], "price": a["price_display"], "desc": a["desc"]}
        for a in actors
    ]
    sys = (
        "You are a scraping-cost advisor. Given a user's data need and a list of "
        "Apify actors (with usage, rating, price), pick the ONE best actor "
        "balancing reliability (high users + rating), fit to the need, and cost. "
        "Respond ONLY as JSON: {\"best_id\": <exact id from the list>, "
        "\"why\": <one short sentence>}."
    )
    payload = {
        "model": settings.AI_QUALIFY_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"NEED: {query}\n\nACTORS:\n{json.dumps(brief, ensure_ascii=False)}"},
        ],
    }
    try:
        r = httpx.post(
            f"{settings.GROQ_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
            json=payload, timeout=_TIMEOUT,
        )
        r.raise_for_status()
        c = r.json()["choices"][0]["message"]["content"]
        s, e = c.find("{"), c.rfind("}")
        obj = json.loads(c[s:e + 1]) if s != -1 else {}
        best = obj.get("best_id")
        ids = {a["id"] for a in actors}
        return (best if best in ids else None, (obj.get("why") or "").strip())
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.error("Actor AI-rank failed: %s", exc)
        return (None, "")


def _source_key(title: str, actor_id: str) -> str:
    """Best-guess which internal scraper an Apify actor plugs into.

    The form's "Use this" needs to know which Tools source to select + which
    scraper's normalizer will parse the actor's output, so we map the actor's
    title/id to one of our registry keys. Empty string = no confident match.
    """
    t = f"{title} {actor_id}".lower()
    if "google" in t and ("map" in t or "place" in t):
        return "google_maps"
    if "linkedin" in t and ("job" in t or "vacan" in t or "hiring" in t):
        return "linkedin_jobs"
    if "linkedin" in t and ("profile" in t or "people" in t or "employee"
                            in t or "person" in t or "lead" in t or "search" in t):
        return "linkedin_people"
    if "indeed" in t:
        return "indeed"
    if "wellfound" in t or "angellist" in t or "angel.co" in t:
        return "wellfound"
    if "naukri" in t:
        return "naukri"
    if "crunchbase" in t:
        return "crunchbase"
    if "indiamart" in t:
        return "indiamart"
    if "facebook" in t and "ad" in t:
        return "facebook_ads"
    if "twitter" in t or "x.com" in t or t.rstrip().endswith("/x"):
        return "twitter"
    if "linkedin" in t:
        return "linkedin_people"  # generic LinkedIn → decision-maker search
    return ""


def advise(query: str, limit: int = 6) -> dict[str, Any]:
    """Full advisor: search the store, rank with AI, flag the recommended actor.

    Returns {"query", "actors": [...best-first, recommended flagged], "note"}.
    Each actor carries ``source_key`` — the internal scraper it maps to — so the
    form can select the right tool and apply the actor as a per-source override.
    """
    actors = search_store(query, limit)
    if not actors:
        return {"query": query, "actors": [], "note": ""}
    best_id, note = _ai_rank(query, actors)
    if not best_id:
        actors = _heuristic_rank(actors)
        best_id = actors[0]["id"]
        note = note or "Top pick by usage + rating (AI unavailable)."
    for a in actors:
        a["recommended"] = a["id"] == best_id
        a["source_key"] = _source_key(a.get("title") or "", a.get("id") or "")
    # Recommended first, then the rest.
    actors.sort(key=lambda x: not x["recommended"])
    return {"query": query, "actors": actors, "note": note}
