"""Base class for vertical configurations.

A vertical encapsulates everything that differs between client industries:
the keywords that signal intent, the decision-maker roles to enrich, the scoring
weights, which sources to draw from, and the default lead threshold. Adding a new
vertical means subclassing this and registering it — no pipeline changes.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("lead-intel.verticals")

# Directory holding YAML vertical configs (verticals/configs/<name>.yaml).
_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")


class BaseVertical:
    """Configuration container shared by all verticals.

    Subclasses override the class attributes. :meth:`to_config` produces the
    plain dict the scorer and pipeline consume.
    """

    name: str = "base"
    signal_keywords: list[str] = []
    target_roles: list[str] = []
    scoring: dict[str, int] = {}
    sources: list[str] = []
    min_score: int = 60
    filters: dict[str, Any] = {}

    @classmethod
    def to_config(cls) -> dict[str, Any]:
        """Return the vertical as a plain dict for use by processors."""
        return {
            "name": cls.name,
            "signal_keywords": list(cls.signal_keywords),
            "target_roles": list(cls.target_roles),
            "scoring": dict(cls.scoring),
            "sources": list(cls.sources),
            "min_score": cls.min_score,
            "filters": dict(cls.filters),
        }


# Registry populated by importing the concrete vertical modules below.
_REGISTRY: dict[str, type[BaseVertical]] = {}


def register(vertical: type[BaseVertical]) -> type[BaseVertical]:
    """Class decorator that registers a vertical by its ``name``."""
    _REGISTRY[vertical.name] = vertical
    return vertical


def _load_yaml_config(name: str) -> Optional[dict[str, Any]]:
    """Load ``configs/<name>.yaml`` if PyYAML and the file are available.

    Returns ``None`` (so the caller falls back to the class registry) when PyYAML
    is not installed or the file is missing/invalid. The vertical name is
    sanitized to prevent path traversal.
    """
    safe = os.path.basename(name).strip()
    if not safe:
        return None
    path = os.path.join(_CONFIG_DIR, f"{safe}.yaml")
    if not os.path.isfile(path):
        return None
    try:
        import yaml  # imported lazily; optional dependency
    except ImportError:
        logger.debug("PyYAML not installed; using class-based vertical configs")
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load vertical config %s: %s", path, exc)
        return None
    # Ensure the canonical keys exist with sane defaults.
    data.setdefault("name", safe)
    data.setdefault("scoring", {})
    data.setdefault("sources", [])
    data.setdefault("target_roles", [])
    data.setdefault("signal_keywords", [])
    data.setdefault("min_score", 60)
    data.setdefault("filters", {})
    return data


def list_verticals() -> list[str]:
    """Return all known vertical names (YAML configs + registered classes)."""
    from verticals import dev_agency, recruitment  # noqa: F401

    names = set(_REGISTRY)
    if os.path.isdir(_CONFIG_DIR):
        for fname in os.listdir(_CONFIG_DIR):
            if fname.endswith((".yaml", ".yml")):
                names.add(os.path.splitext(fname)[0])
    return sorted(names)


def get_vertical_config(name: str) -> dict[str, Any]:
    """Return the config dict for a vertical name.

    Resolution order: (1) YAML config file, (2) registered vertical class, (3)
    the recruitment fallback. This lets verticals be defined as data (YAML) while
    keeping the class-based ones working unchanged.
    """
    # Import here to ensure concrete verticals have registered themselves.
    from verticals import dev_agency, recruitment  # noqa: F401

    yaml_config = _load_yaml_config(name)
    if yaml_config is not None:
        return yaml_config

    vertical = _REGISTRY.get(name) or _REGISTRY.get("recruitment", BaseVertical)
    return vertical.to_config()
