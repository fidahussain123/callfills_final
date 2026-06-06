"""Redis-backed deduplication: don't re-process a company within 24 hours.

Keys are namespaced ``seen:{company_key}`` with a 24-hour TTL. The dedup key is
the company domain when available, otherwise a normalized company name, so that
free-text sources without a domain are still deduplicated sensibly.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

import redis

from config import settings
from db.models import NormalizedSignal

logger = logging.getLogger("lead-intel.processors.deduplicator")

_TTL_SECONDS = 86_400  # 24 hours


@lru_cache(maxsize=1)
def _get_redis() -> Optional[redis.Redis]:
    """Return a cached Redis client, or ``None`` if the connection fails."""
    try:
        client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001
        logger.error("Redis unavailable (%s); dedup disabled", exc)
        return None


def _dedup_key(signal: NormalizedSignal) -> str:
    """Compute the per-company dedup key for a signal."""
    if signal.company_domain:
        return signal.company_domain.strip().lower()
    return signal.company_name.strip().lower()


def is_duplicate(company_key: str) -> bool:
    """Return True if this company was already seen within the TTL window."""
    client = _get_redis()
    if client is None:
        return False
    try:
        return bool(client.exists(f"seen:{company_key}"))
    except Exception as exc:  # noqa: BLE001
        logger.error("is_duplicate failed for %s: %s", company_key, exc)
        return False


def mark_seen(company_key: str) -> None:
    """Mark a company as seen with a 24-hour TTL."""
    client = _get_redis()
    if client is None:
        return
    try:
        client.set(f"seen:{company_key}", "1", ex=_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.error("mark_seen failed for %s: %s", company_key, exc)


def filter_duplicates(signals: list[NormalizedSignal]) -> list[NormalizedSignal]:
    """Drop signals for companies already seen in the last 24 hours.

    A company may legitimately appear across several signals in one run; we keep
    all signals for a company that is new *to this run*, marking it seen once.
    """
    kept: list[NormalizedSignal] = []
    seen_this_run: set[str] = set()

    for signal in signals:
        key = _dedup_key(signal)
        if not key:
            continue
        if key in seen_this_run:
            kept.append(signal)  # already accepted this company in this run
            continue
        if is_duplicate(key):
            continue
        seen_this_run.add(key)
        mark_seen(key)
        kept.append(signal)

    logger.info("Kept %d signals after dedup", len(kept))
    return kept
