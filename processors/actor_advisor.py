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
_TIMEOUT = 25.0


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
        out.append({
            "id": f"{a.get('username')}/{a.get('name')}",
            "title": (a.get("title") or a.get("name") or "").strip(),
            "desc": (a.get("description") or "").strip()[:160],
            "users": stats.get("totalUsers") or 0,
            "rating": a.get("actorReviewRating"),
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


def advise(query: str, limit: int = 6) -> dict[str, Any]:
    """Full advisor: search the store, rank with AI, flag the recommended actor.

    Returns {"query", "actors": [...best-first, recommended flagged], "note"}.
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
    # Recommended first, then the rest.
    actors.sort(key=lambda x: not x["recommended"])
    return {"query": query, "actors": actors, "note": note}
