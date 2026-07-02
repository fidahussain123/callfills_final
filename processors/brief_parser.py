"""Parse a plain-English pipeline brief into structured scrape filters (Groq).

The operator describes what they want in the Description — e.g. "founders and
CTOs of startups in Bangalore with under 1000 LinkedIn followers, so we can
pitch follower-growth services" — and this turns it into the structured knobs
the scraper + filters actually use: job titles, location, industries, company
size, and numeric caps like ``max_followers``. That's what makes the free-text
description "drive the fetch according to the tool."

Fail-open: returns {} when Groq is unavailable, so the pipeline still runs off
the explicit form fields.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from config import settings

logger = logging.getLogger("lead-intel.processors.brief_parser")

_SYSTEM = (
    "You convert a lead-generation brief into a strict JSON filter object. "
    "Extract ONLY what the brief states; use null/empty when unstated. Shape:\n"
    '{"source": <one of: linkedin_people, google_maps, funding_news, jobs, indiamart, null>,\n'
    ' "job_titles": [<e.g. "Founder","CTO","CEO">],\n'
    ' "industries": [<e.g. "fintech","saas">],\n'
    ' "locations": [<cities/countries, e.g. "Bangalore">],\n'
    ' "company_min": <int|null>, "company_max": <int|null>,   // employee headcount\n'
    ' "max_followers": <int|null>, "min_followers": <int|null>,  // LinkedIn follower caps\n'
    ' "recently_posted": <bool|null>,\n'
    ' "summary": "<one line: who this targets>"}\n'
    "Rules: 'under/less than 1000 followers' -> max_followers:1000. "
    "'startups' with no size -> company_max:200. Map CTO/CEO/Founder to job_titles. "
    "If the brief is about finding PEOPLE on LinkedIn (founders/CTOs/profiles), source='linkedin_people'. "
    "Respond with ONLY the JSON object, no prose."
)


def _extract_json(content: str) -> dict[str, Any]:
    if not content:
        return {}
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        obj = json.loads(content[start : end + 1])
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def parse_brief(text: str) -> dict[str, Any]:
    """Return structured filters extracted from a free-text brief, or {}.

    Also runs a cheap deterministic backstop for the follower cap so
    "under 1000 followers" still works even if the model misses it.
    """
    text = (text or "").strip()
    if not text:
        return {}

    parsed: dict[str, Any] = {}
    if settings.ai_verify_ready:
        try:
            url = f"{settings.GROQ_BASE_URL.rstrip('/')}/chat/completions"
            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                json={
                    "model": settings.AI_QUALIFY_MODEL,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": f"BRIEF:\n{text}"},
                    ],
                },
                timeout=30,
            )
            if resp.status_code == 200:
                parsed = _extract_json(resp.json()["choices"][0]["message"]["content"])
        except Exception as exc:  # noqa: BLE001 - fail-open to the regex backstop
            logger.warning("brief parse via Groq failed (%s); using regex backstop", exc)

    # Deterministic backstop for the numeric follower cap (the critical filter).
    if not parsed.get("max_followers"):
        m = re.search(r"(?:under|below|less than|<|max(?:imum)?)\s*([\d,]+)\s*(?:k\b)?\s*followers?", text, re.I)
        if not m:
            m = re.search(r"followers?\s*(?:under|below|less than|<)\s*([\d,]+)", text, re.I)
        if m:
            n = int(m.group(1).replace(",", ""))
            if re.search(r"[\d,]+\s*k\b", m.group(0), re.I):
                n *= 1000
            parsed["max_followers"] = n

    return parsed
