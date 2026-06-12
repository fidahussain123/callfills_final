"""Company-level AI qualification — one Groq verdict per deduped company.

Runs AFTER cross-ref (so each company is judged once, not per signal — far
cheaper than gating raw signals) and does three jobs in a single call:

1. **Junk filter** — drops "companies" that aren't a real, sellable B2B
   prospect for the active vertical (news round-ups, investors, products,
   misparsed headline fragments the structural extractor let through).
2. **Why-it's-a-lead** — a one-line plain-English summary shown on the card.
3. **HQ-city extraction** — reads the signal text with judgment and returns
   the company's city when stated or strongly implied ("the Bengaluru-based
   firm", "its Pune headquarters"), so funding/job leads get a location and a
   map pin that regex alone misses.

Directory sources (Google Maps, IndiaMART) skip the gate entirely: those
listings are already real businesses with exact addresses — AI adds nothing.

Fail-open like ai_verify: no key / disabled / API error → companies pass
through unchanged. A company is only removed by an explicit keep=false.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from config import settings

logger = logging.getLogger("lead-intel.processors.ai_qualify")

_BATCH_SIZE = 12          # companies per Groq call
_MAX_CONCURRENCY = 1      # sequential: free-tier TPM limits punish parallelism
_TIMEOUT = 45.0
_MAX_TEXT_CHARS = 350     # per-signal snippet cap (headline carries the signal)
_MAX_SNIPPETS = 2         # signals quoted per company
_MAX_RETRIES = 4
_RETRY_BASE_DELAY = 3.0
_PACE_DELAY = 1.5         # seconds between sequential batches (smooths TPM)

# Sources whose companies are pre-verified directories — never AI-gated.
_DIRECTORY_SOURCES = {"google_maps", "indiamart"}

_SYSTEM_PROMPT = (
    "You are a B2B lead-qualification analyst. You receive deduplicated "
    "company records built from scraped signals (funding news, job posts, "
    "forum posts) and judge each against the given niche / ideal customer "
    "profile.\n\n"
    "Set keep=false ONLY when the record is clearly NOT a sellable company: "
    "a news round-up or listicle, an investor / VC / accelerator, a government "
    "body, a generic word or misparsed headline fragment, a person, or a "
    "product name with no identifiable company. When in doubt, keep it.\n\n"
    "For every record also return:\n"
    "- summary: ONE line (max 18 words) saying what the company does and why "
    "it is a lead right now (e.g. 'Fintech startup, raised $12M Series A — "
    "likely hiring and buying services').\n"
    "- hq_city: the company's headquarters city ONLY if the text states or "
    "strongly implies it ('Bengaluru-based', 'its Pune office'). Use the plain "
    "city name (e.g. 'Delhi', 'Bengaluru', 'Mumbai'). Ignore cities that are "
    "just markets, expansion targets, or universities (e.g. 'IIT Delhi "
    "alumnus' is NOT a Delhi HQ). Return null when unsure.\n\n"
    "Respond with ONLY a JSON object, no prose, no markdown:\n"
    '{"results": [{"id": <int>, "keep": <bool>, "summary": <string>, '
    '"hq_city": <string|null>, "confidence": <number 0-1>}]}\n'
    "Include every id from the input exactly once."
)


def _niche_brief(vertical_config: dict[str, Any]) -> str:
    name = vertical_config.get("display_name") or vertical_config.get("name") or "B2B leads"
    desc = " ".join((vertical_config.get("description") or "").split())
    return f"Vertical: {name}. Ideal customer profile: {desc}"


def _snippet(company: dict[str, Any]) -> str:
    """Quote up to two signal texts (truncated) as the model's evidence."""
    texts: list[str] = []
    for sig in (company.get("signals") or [])[:_MAX_SNIPPETS]:
        raw = getattr(sig, "raw_text", None) or ""
        if raw:
            texts.append(raw[:_MAX_TEXT_CHARS])
    return "\n---\n".join(texts)


def _extract_results(content: str) -> list[dict[str, Any]]:
    if not content:
        return []
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(content[start : end + 1])
    except (ValueError, TypeError):
        return []
    results = parsed.get("results") if isinstance(parsed, dict) else None
    return results if isinstance(results, list) else []


async def _call_groq(
    client: httpx.AsyncClient, niche: str, batch: list[dict[str, Any]]
) -> dict[int, dict[str, Any]]:
    """Qualify one batch; {id: verdict}. Retries 429/5xx, fails open to {}."""
    payload = {
        "model": settings.GROQ_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"NICHE / ICP:\n{niche}\n\n"
                    f"Companies to qualify (JSON array):\n"
                    f"{json.dumps(batch, ensure_ascii=False)}"
                ),
            },
        ],
    }
    url = f"{settings.GROQ_BASE_URL.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.GROQ_API_KEY}"}
    delay = _RETRY_BASE_DELAY
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 429 or resp.status_code >= 500:
                hdr = resp.headers.get("retry-after", "")
                wait = float(hdr) if hdr.replace(".", "", 1).isdigit() else delay
                logger.warning(
                    "Groq %s (attempt %d/%d); retrying in %.1fs",
                    resp.status_code, attempt, _MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                delay *= 2
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return {
                int(r["id"]): r
                for r in _extract_results(content)
                if isinstance(r, dict) and "id" in r
            }
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning(
                "Groq network error %s (attempt %d/%d); retrying in %.1fs",
                exc, attempt, _MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)
            delay *= 2
        except Exception as exc:  # noqa: BLE001 - unexpected: fail open
            logger.error("Groq qualify batch failed (%s); passing batch through", exc)
            return {}
    logger.error("Groq qualify exhausted %d retries; passing batch through", _MAX_RETRIES)
    return {}


async def qualify_companies(
    companies: list[dict[str, Any]], vertical_config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Qualify every non-directory company via Groq; fail-open on any error.

    Mutates kept companies in place: sets ``ai_summary`` and, when the model
    found an HQ city for a company without one, fills ``location``/``lat``/
    ``lng`` so the lead pins on the map and passes city filters.
    """
    if not (settings.ai_verify_ready and settings.AI_QUALIFY_ENABLED):
        return companies

    passthrough = [
        c for c in companies
        if set(c.get("signal_sources") or []) & _DIRECTORY_SOURCES
    ]
    gated = [
        c for c in companies
        if not (set(c.get("signal_sources") or []) & _DIRECTORY_SOURCES)
    ]
    if not gated:
        return companies

    niche = _niche_brief(vertical_config)
    items = [
        {
            "id": i,
            "company_name": c.get("company_name"),
            "known_location": c.get("location"),
            "signal_types": c.get("signal_types"),
            "evidence": _snippet(c),
        }
        for i, c in enumerate(gated)
    ]
    batches = [items[i : i + _BATCH_SIZE] for i in range(0, len(items), _BATCH_SIZE)]
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:

        async def _run(batch: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
            async with semaphore:
                result = await _call_groq(client, niche, batch)
                await asyncio.sleep(_PACE_DELAY)  # smooth token usage across batches
                return result

        batch_results = await asyncio.gather(*(_run(b) for b in batches))

    verdicts: dict[int, dict[str, Any]] = {}
    for result in batch_results:
        verdicts.update(result)

    from processors.geocoder import geocode_city

    min_conf = settings.AI_VERIFY_MIN_CONFIDENCE
    kept: list[dict[str, Any]] = []
    dropped = 0
    located = 0
    for i, company in enumerate(gated):
        verdict = verdicts.get(i)
        if verdict is None:
            kept.append(company)  # fail-open: no verdict → keep
            continue
        confidence = verdict.get("confidence")
        confident = not isinstance(confidence, (int, float)) or confidence >= min_conf
        if not verdict.get("keep", True) and confident:
            dropped += 1
            continue
        summary = (verdict.get("summary") or "").strip()
        if summary:
            company["ai_summary"] = summary
        hq_city = (verdict.get("hq_city") or "").strip() if verdict.get("hq_city") else ""
        if hq_city and not company.get("location"):
            coords = geocode_city(hq_city)
            if coords:  # only trust city names we can place
                company["location"] = hq_city
                if company.get("lat") is None or company.get("lng") is None:
                    company["lat"], company["lng"] = coords
                located += 1
        kept.append(company)

    logger.info(
        "AI qualify (%s): %d gated → %d kept, %d dropped, %d newly located · "
        "%d directory passthrough",
        settings.GROQ_MODEL, len(gated), len(kept), dropped, located, len(passthrough),
    )
    return passthrough + kept
