"""Offline city geocoder — map a location string to (lat, lng) with no network.

Structured non-map sources (Wellfound, Crunchbase, funding RSS) give a company's
city as free text ("Bengaluru, India", "HSR Layout, Bangalore", "Remote") but no
coordinates. This tiny lookup turns a recognised city into centroid coords so
those leads render on the same Leaflet map as Google Maps businesses — for free,
instantly, with zero dependencies. Unknown cities / "Remote" return None (no pin).

City-centroid granularity is intentional: an HQ-city pin, not a street address.
"""

from __future__ import annotations

import re
from typing import Optional

# Canonical city -> (lat, lng). India hubs first, then the global startup hubs
# Wellfound surfaces. Centroid coordinates (good enough for an HQ-city pin).
_COORDS: dict[str, tuple[float, float]] = {
    "bengaluru": (12.9716, 77.5946),
    "mumbai": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
    "gurugram": (28.4595, 77.0266),
    "noida": (28.5355, 77.3910),
    "pune": (18.5204, 73.8567),
    "hyderabad": (17.3850, 78.4867),
    "chennai": (13.0827, 80.2707),
    "kolkata": (22.5726, 88.3639),
    "ahmedabad": (23.0225, 72.5714),
    "jaipur": (26.9124, 75.7873),
    "chandigarh": (30.7333, 76.7794),
    "indore": (22.7196, 75.8577),
    "kochi": (9.9312, 76.2673),
    "coimbatore": (11.0168, 76.9558),
    "thiruvananthapuram": (8.5241, 76.9366),
    "surat": (21.1702, 72.8311),
    "nagpur": (21.1458, 79.0882),
    "lucknow": (26.8467, 80.9462),
    "bhubaneswar": (20.2961, 85.8245),
    "vadodara": (22.3072, 73.1812),
    "visakhapatnam": (17.6868, 83.2185),
    "mysuru": (12.2958, 76.6394),
    "nashik": (19.9975, 73.7898),
    "faridabad": (28.4089, 77.3178),
    "goa": (15.4909, 73.8278),
    # Global startup hubs (Wellfound is worldwide)
    "san francisco": (37.7749, -122.4194),
    "new york": (40.7128, -74.0060),
    "london": (51.5074, -0.1278),
    "singapore": (1.3521, 103.8198),
    "dubai": (25.2048, 55.2708),
    "berlin": (52.5200, 13.4050),
    "austin": (30.2672, -97.7431),
    "toronto": (43.6532, -79.3832),
}

# Common aliases / alternate spellings -> a canonical key above.
_ALIASES: dict[str, str] = {
    "bangalore": "bengaluru",
    "bombay": "mumbai",
    "navi mumbai": "mumbai",
    "new delhi": "delhi",
    "gurgaon": "gurugram",
    "cochin": "kochi",
    "trivandrum": "thiruvananthapuram",
    "mysore": "mysuru",
    "vizag": "visakhapatnam",
    "baroda": "vadodara",
    "panaji": "goa",
    "panjim": "goa",
    "san francisco bay area": "san francisco",
    "bay area": "san francisco",
    "new york city": "new york",
}

# Every searchable name (canonical + alias), longest first so multi-word names
# ("new delhi", "san francisco") win before their single-word substrings.
_NAMES = sorted(set(_COORDS) | set(_ALIASES), key=len, reverse=True)
_PATTERNS = [(n, re.compile(r"\b" + re.escape(n) + r"\b")) for n in _NAMES]


def geocode_city(location: Optional[str]) -> Optional[tuple[float, float]]:
    """Return (lat, lng) for the first known city found in ``location``, else None.

    Matches whole words so "HSR Layout, Bangalore" -> Bengaluru and
    "Bengaluru, Karnataka, India" -> Bengaluru, while "Remote"/unknown -> None.
    """
    if not location:
        return None
    text = str(location).strip().lower()
    if not text:
        return None
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return _COORDS[_ALIASES.get(name, name)]
    return None
