"""Scraper registry — plug-and-play source discovery.

Any scraper that wants to participate in the pipeline decorates its class with
``@register_scraper``. The pipeline and normalizer then build instances purely
from this registry, so adding a new source is a one-file change: write the
scraper, decorate it, and reference its ``key`` from a vertical's ``sources``.

The registry never touches the network at import time; it just records classes.
``load_all`` imports the bundled scraper modules so their decorators fire, and is
idempotent.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # avoid import cycle at runtime
    from scrapers.base import BaseScraper

logger = logging.getLogger("lead-intel.scrapers.registry")

# key -> scraper class
_REGISTRY: dict[str, type["BaseScraper"]] = {}

# Bundled scraper modules imported by load_all() so their @register_scraper
# decorators run. New scrapers added here become auto-discoverable.
_BUNDLED_MODULES = [
    "scrapers.linkedin_jobs",
    "scrapers.naukri",
    "scrapers.indeed",
    "scrapers.wellfound",
    "scrapers.rss_feeds",
    "scrapers.facebook_ads",
    "scrapers.reddit",
    "scrapers.twitter",
]

_loaded = False


def register_scraper(cls: type["BaseScraper"]) -> type["BaseScraper"]:
    """Class decorator that registers a scraper under its ``key``.

    Falls back to ``source_platform`` when ``key`` is not set, so existing
    scrapers register sensibly even without an explicit key.
    """
    key = getattr(cls, "key", None) or getattr(cls, "source_platform", None)
    if not key:
        raise ValueError(f"{cls.__name__} must define a `key` or `source_platform`")
    if key in _REGISTRY and _REGISTRY[key] is not cls:
        logger.warning("Scraper key %r already registered; overwriting", key)
    _REGISTRY[key] = cls
    logger.debug("Registered scraper %s as %r", cls.__name__, key)
    return cls


def load_all() -> None:
    """Import all bundled scraper modules so their classes self-register."""
    global _loaded
    if _loaded:
        return
    for module in _BUNDLED_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - a broken source shouldn't kill discovery
            logger.error("Failed to import scraper module %s: %s", module, exc)
    _loaded = True


def all_keys() -> list[str]:
    """Return every registered scraper key (sorted)."""
    load_all()
    return sorted(_REGISTRY)


def get_scraper_class(key: str) -> Optional[type["BaseScraper"]]:
    """Return the scraper class registered under ``key``, or ``None``."""
    load_all()
    return _REGISTRY.get(key)


def build_instances(keys: Optional[list[str]] = None) -> list["BaseScraper"]:
    """Instantiate scrapers.

    ``keys`` selects a subset (e.g. a vertical's ``sources``); ``None`` builds
    every registered scraper. Unknown keys are logged and skipped so a typo in a
    vertical config never aborts a run.
    """
    load_all()
    selected = keys if keys is not None else sorted(_REGISTRY)
    instances: list["BaseScraper"] = []
    for key in selected:
        cls = _REGISTRY.get(key)
        if cls is None:
            logger.warning("No scraper registered for key %r; skipping", key)
            continue
        try:
            instances.append(cls())
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to instantiate scraper %r: %s", key, exc)
    return instances
