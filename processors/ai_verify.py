"""AI verification gate — filters noisy free-text signals down to genuine,
on-niche buying signals using Groq (OpenAI-compatible inference API).

Scraped posts from unstructured sources (Reddit, Twitter/X, Facebook Ads) are
full of junk the structural extractor can't reliably reject: subreddit names,
generic words ("AI", "Reddit"), meme/show titles, person/investor names, news
outlets, and off-topic chatter. This module sends those signals to Groq in small
batches and keeps only the ones that are (a) a real, identifiable company and
(b) a genuine buying signal relevant to the active vertical's niche / ICP.

Design principles:

* **Scoped** — only the platforms in ``settings.AI_VERIFY_PLATFORMS`` are gated.
  Structured sources (LinkedIn, Naukri, Indeed, Wellfound, RSS) bypass untouched.
* **Fail-open** — if ``GROQ_API_KEY`` is unset, the feature is disabled, or the
  API errors/times out, signals pass through unchanged. The gate can only remove
  a lead via an explicit ``keep=false`` verdict, never by failing.
* **Cheap & fast** — small batches, bounded concurrency, truncated post text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from config import settings
from db.models import NormalizedSignal
from signal_types import canonical

logger = logging.getLogger("lead-intel.processors.ai_verify")

_BATCH_SIZE = 15          # posts per Groq call
_MAX_CONCURRENCY = 2      # simultaneous in-flight calls (gentle on free-tier limits)
_TIMEOUT = 45.0           # seconds per call
_MAX_TEXT_CHARS = 600     # truncate long posts to control token cost
_MAX_RETRIES = 4          # retries on 429 / 5xx before failing open
_RETRY_BASE_DELAY = 2.0   # seconds; doubles each retry (honors Retry-After)

# Obvious non-companies dropped by a cheap local heuristic BEFORE any API call —
# slashes the Reddit/Facebook firehose (usernames, HTML-entity scraps, generic
# words) so the LLM only judges plausible candidates: lower cost, and the batch
# count stays under free-tier rate limits. Conservative on purpose — anything
# plausible still goes to the model.
_JUNK_EXACT = {
    "reddit", "wikipedia", "ai", "none", "null", "n/a", "na", "remote",
    "startup", "startups", "job", "jobs", "hiring", "career", "careers",
    "anonymous", "deleted", "unknown", "the", "op", "edit", "comment",
}
_USERNAME_RE = re.compile(r"^/?u/", re.IGNORECASE)   # reddit /u/handle
_HANDLE_RE = re.compile(r"^[@/]")                     # @handle, /r/sub, etc.

_SYSTEM_PROMPT = (
    "You are a strict B2B lead-qualification filter for a recruitment-agency "
    "lead-generation tool. You receive scraped social/forum/ad posts and decide, "
    "for each, whether it represents a REAL, identifiable company showing a "
    "GENUINE buying signal relevant to the given niche.\n\n"
    "Set keep=false when the 'company' is NOT a real hiring company: a subreddit, "
    "a generic word (e.g. 'AI', 'Reddit', 'Startup', 'Remote'), a meme / show / "
    "movie / product title, a person's name, an investor / VC / accelerator, a "
    "news outlet or job board itself, or anything you cannot tie to one specific "
    "company. Also set keep=false for off-niche posts and posts clearly outside "
    "the target geography when that is determinable; when only the geography is "
    "uncertain, keep it.\n\n"
    "For kept items return a cleaned company_name (the proper company name only, "
    "no extra words/punctuation) and the best signal_type from exactly: "
    "hiring_post, funding_news, outsource_intent, agency_switch, growth_pain, "
    "general_signal.\n\n"
    "Respond with ONLY a JSON object of this exact shape, no prose, no markdown:\n"
    '{"results": [{"id": <int>, "keep": <bool>, "company_name": <string>, '
    '"signal_type": <string>, "confidence": <number 0-1>, "reason": <short string>}]}\n'
    "Include every id from the input exactly once."
)


def _niche_brief(vertical_config: dict[str, Any]) -> str:
    """Build a one-paragraph niche/ICP brief from the vertical config."""
    name = (
        vertical_config.get("display_name")
        or vertical_config.get("name")
        or "B2B leads"
    )
    desc = " ".join((vertical_config.get("description") or "").split())
    location = settings.TARGET_LOCATION
    return (
        f"Vertical: {name}. "
        f"Ideal customer profile: {desc} "
        f"Geographic focus: {location}-based companies (a recruitment agency's buyers)."
    )


def _is_obvious_junk(name: str) -> bool:
    """Cheap local reject for clear non-companies (no API call needed)."""
    n = (name or "").strip()
    if not n:
        return True
    if _USERNAME_RE.match(n) or _HANDLE_RE.match(n):
        return True
    # HTML-entity scraps that survived decoding ("&quot;business", "&#39;...").
    if n.startswith("&") or "&quot;" in n or "&#" in n or "&amp;" in n:
        return True
    if n.lower() in _JUNK_EXACT:
        return True
    # Needs at least two actual letters of signal.
    if len(re.sub(r"[^A-Za-z]", "", n)) < 2:
        return True
    return False


def _extract_results(content: str) -> list[dict[str, Any]]:
    """Pull the ``results`` array out of a model response, tolerating fences/prose."""
    if not content:
        return []
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1 or end <= start:
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
    """Verify one batch; return ``{id: verdict}``.

    Retries on rate-limit (429) and server (5xx) errors with exponential backoff,
    honoring any ``Retry-After`` header. Returns an empty dict (fail-open: keep
    the batch) only after retries are exhausted or on an unexpected error.
    """
    payload = {
        "model": settings.GROQ_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"NICHE / ICP:\n{niche}\n\n"
                    f"Posts to evaluate (JSON array):\n"
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
            results = _extract_results(content)
            return {int(r["id"]): r for r in results if isinstance(r, dict) and "id" in r}
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning(
                "Groq network error %s (attempt %d/%d); retrying in %.1fs",
                exc, attempt, _MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)
            delay *= 2
        except Exception as exc:  # noqa: BLE001 - unexpected: fail open
            logger.error("Groq verify batch failed (%s); passing batch through", exc)
            return {}
    logger.error(
        "Groq verify batch exhausted %d retries; passing batch through", _MAX_RETRIES
    )
    return {}


async def verify_signals(
    signals: list[NormalizedSignal], vertical_config: dict[str, Any]
) -> list[NormalizedSignal]:
    """Filter free-text signals to real, on-niche buying signals via Groq.

    Returns the structured-source signals untouched plus the AI-kept free-text
    signals (with cleaned company name + signal type applied). Fail-open: any
    signal without a usable verdict is kept.
    """
    if not settings.ai_verify_ready:
        logger.info(
            "AI verify disabled (set GROQ_API_KEY and AI_VERIFY_ENABLED=true to "
            "enable); passing all signals through"
        )
        return signals

    platforms = settings.AI_VERIFY_PLATFORMS
    gated = [s for s in signals if s.source_platform in platforms]
    passthrough = [s for s in signals if s.source_platform not in platforms]
    if not gated:
        return signals

    # Cheap local pre-filter first: drop obvious junk (usernames, HTML scraps,
    # generic words) with no API call, so the LLM only judges real candidates.
    candidates = [s for s in gated if not _is_obvious_junk(s.company_name)]
    pre_dropped = len(gated) - len(candidates)
    if not candidates:
        logger.info(
            "AI verify: %d gated all pre-filtered as junk · %d passthrough",
            len(gated), len(passthrough),
        )
        return passthrough

    niche = _niche_brief(vertical_config)
    items = [
        {
            "id": i,
            "source": s.source_platform,
            "company_name": s.company_name,
            "text": (s.raw_text or "")[:_MAX_TEXT_CHARS],
        }
        for i, s in enumerate(candidates)
    ]
    batches = [items[i : i + _BATCH_SIZE] for i in range(0, len(items), _BATCH_SIZE)]
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:

        async def _run(batch: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
            async with semaphore:
                return await _call_groq(client, niche, batch)

        batch_results = await asyncio.gather(*(_run(b) for b in batches))

    verdicts: dict[int, dict[str, Any]] = {}
    for result in batch_results:
        verdicts.update(result)

    min_conf = settings.AI_VERIFY_MIN_CONFIDENCE
    kept: list[NormalizedSignal] = []
    dropped = 0
    for i, signal in enumerate(candidates):
        verdict = verdicts.get(i)
        if verdict is None:
            kept.append(signal)  # fail-open: no verdict → keep
            continue
        if not verdict.get("keep", True):
            dropped += 1
            continue
        confidence = verdict.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < min_conf:
            dropped += 1
            continue
        name = (verdict.get("company_name") or "").strip()
        if name:
            signal.company_name = name
        stype = (verdict.get("signal_type") or "").strip()
        if stype:
            signal.signal_type = canonical(stype)
        kept.append(signal)

    logger.info(
        "AI verify (%s): %d gated → %d pre-dropped + %d sent → %d kept, "
        "%d AI-dropped · %d passthrough",
        settings.GROQ_MODEL,
        len(gated),
        pre_dropped,
        len(candidates),
        len(kept),
        dropped,
        len(passthrough),
    )
    return passthrough + kept
