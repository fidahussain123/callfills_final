"""Abstract base class for all scrapers.

A scraper is responsible for two things:

1. ``fetch`` — pull raw records from a source and wrap each in a ``RawSignal``.
2. ``normalize`` — map one ``RawSignal`` to the canonical ``NormalizedSignal``.

Concrete scrapers that talk to Apify share the :class:`ApifyScraper` helper which
handles client construction and actor invocation with error handling.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from apify_client import ApifyClient

from config import settings
from db.models import NormalizedSignal, RawSignal

logger = logging.getLogger("lead-intel.scrapers")


class BaseScraper(ABC):
    """Contract every scraper must implement."""

    #: Short platform identifier stored on each signal, e.g. ``"linkedin"``.
    source_platform: str = "base"

    @abstractmethod
    def fetch(self, lookback_days: int) -> list[RawSignal]:
        """Pull raw records from the source for the given lookback window."""
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        """Map a single raw record to a :class:`NormalizedSignal`.

        Return ``None`` if the record cannot be normalized (e.g. missing a
        company name), so the caller can drop it.
        """
        raise NotImplementedError

    # --- shared helpers -----------------------------------------------------
    @staticmethod
    def cutoff(lookback_days: int) -> datetime:
        """Return the UTC datetime that marks the oldest acceptable record."""
        return datetime.now(timezone.utc) - timedelta(days=lookback_days)

    @staticmethod
    def parse_epoch(value: Any) -> Optional[datetime]:
        """Parse a unix epoch (seconds) into a UTC datetime, if possible."""
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def parse_iso(value: Any) -> Optional[datetime]:
        """Parse an ISO-8601 string into a UTC datetime, if possible."""
        if not value:
            return None
        try:
            text = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None


class ApifyScraper(BaseScraper):
    """Base for scrapers that invoke an Apify actor.

    Subclasses set :attr:`actor_id` and implement :meth:`build_input` and
    :meth:`normalize`. The :meth:`run_actor` helper executes the actor and
    streams dataset items.
    """

    #: Apify actor identifier, e.g. ``"apify/linkedin-jobs-scraper"``.
    actor_id: str = ""

    def __init__(self) -> None:
        if not settings.APIFY_API_TOKEN:
            logger.warning("APIFY_API_TOKEN not set; %s will return no data", type(self).__name__)
            self._client: Optional[ApifyClient] = None
        else:
            self._client = ApifyClient(settings.APIFY_API_TOKEN)

    def build_input(self, lookback_days: int) -> dict[str, Any]:
        """Return the Apify actor input payload. Override in subclasses."""
        raise NotImplementedError

    @staticmethod
    def _default_dataset_id(run: Any) -> Optional[str]:
        """Extract the default dataset id from an Apify run result.

        The apify-client return type varies by version: older clients return a
        plain ``dict`` while newer ones return a typed ``Run`` object. Support
        both ``run["defaultDatasetId"]`` and ``run.default_dataset_id`` shapes.
        """
        if run is None:
            return None
        if isinstance(run, dict):
            return run.get("defaultDatasetId") or run.get("default_dataset_id")
        for attr in ("default_dataset_id", "defaultDatasetId"):
            value = getattr(run, attr, None)
            if value:
                return value
        return None

    def run_actor(
        self, run_input: dict[str, Any], max_items: Optional[int] = None
    ) -> list[dict[str, Any]]:
        """Run the configured actor and return its dataset items.

        Errors are logged and result in an empty list so one failing source
        never aborts the whole pipeline.
        """
        if not self._client:
            return []
        try:
            logger.info("Running Apify actor %s", self.actor_id)
            run = self._client.actor(self.actor_id).call(run_input=run_input)
            dataset_id = self._default_dataset_id(run)
            if not dataset_id:
                logger.error("Actor %s returned no dataset", self.actor_id)
                return []
            items: list[dict[str, Any]] = []
            for item in self._client.dataset(dataset_id).iterate_items():
                items.append(item)
                if max_items and len(items) >= max_items:
                    break
            logger.info("Actor %s returned %d items", self.actor_id, len(items))
            return items
        except Exception as exc:  # noqa: BLE001 - isolate source failures
            logger.error("Apify actor %s failed: %s", self.actor_id, exc)
            return []

    def fetch(self, lookback_days: int) -> list[RawSignal]:
        """Default fetch: build input, run actor, wrap each item in a RawSignal."""
        run_input = self.build_input(lookback_days)
        items = self.run_actor(run_input)
        return [RawSignal(source_platform=self.source_platform, data=item) for item in items]
