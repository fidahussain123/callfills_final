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
    # Tier-2/3 India (state capitals + startup/SME hubs)
    "kanpur": (26.4499, 80.3319),
    "patna": (25.5941, 85.1376),
    "bhopal": (23.2599, 77.4126),
    "ranchi": (23.3441, 85.3096),
    "raipur": (21.2514, 81.6296),
    "guwahati": (26.1445, 91.7362),
    "dehradun": (30.3165, 78.0322),
    "shimla": (31.1048, 77.1734),
    "srinagar": (34.0837, 74.7973),
    "jammu": (32.7266, 74.8570),
    "amritsar": (31.6340, 74.8723),
    "ludhiana": (30.9010, 75.8573),
    "jalandhar": (31.3260, 75.5762),
    "agra": (27.1767, 78.0081),
    "varanasi": (25.3176, 82.9739),
    "prayagraj": (25.4358, 81.8463),
    "meerut": (28.9845, 77.7064),
    "ghaziabad": (28.6692, 77.4538),
    "greater noida": (28.4744, 77.5040),
    "jodhpur": (26.2389, 73.0243),
    "udaipur": (24.5854, 73.7125),
    "kota": (25.2138, 75.8648),
    "rajkot": (22.3039, 70.8022),
    "gandhinagar": (23.2156, 72.6369),
    "bhavnagar": (21.7645, 72.1519),
    "aurangabad": (19.8762, 75.3433),
    "kolhapur": (16.7050, 74.2433),
    "solapur": (17.6599, 75.9064),
    "thane": (19.2183, 72.9781),
    "gwalior": (26.2183, 78.1828),
    "jabalpur": (23.1815, 79.9864),
    "madurai": (9.9252, 78.1198),
    "tiruchirappalli": (10.7905, 78.7047),
    "salem": (11.6643, 78.1460),
    "tirupur": (11.1085, 77.3411),
    "erode": (11.3410, 77.7172),
    "vellore": (12.9165, 79.1325),
    "puducherry": (11.9416, 79.8083),
    "tirupati": (13.6288, 79.4192),
    "vijayawada": (16.5062, 80.6480),
    "guntur": (16.3067, 80.4365),
    "warangal": (17.9689, 79.5941),
    "hubballi": (15.3647, 75.1240),
    "belagavi": (15.8497, 74.4977),
    "mangaluru": (12.9141, 74.8560),
    "udupi": (13.3409, 74.7421),
    "manipal": (13.3525, 74.7928),
    "shivamogga": (13.9299, 75.5681),
    "kozhikode": (11.2588, 75.7804),
    "thrissur": (10.5276, 76.2144),
    "kollam": (8.8932, 76.6141),
    "kannur": (11.8745, 75.3704),
    "kottayam": (9.5916, 76.5222),
    "siliguri": (26.7271, 88.3953),
    "durgapur": (23.5204, 87.3119),
    "asansol": (23.6839, 86.9523),
    "howrah": (22.5958, 88.2636),
    "cuttack": (20.4625, 85.8830),
    "rourkela": (22.2604, 84.8536),
    "jamshedpur": (22.8046, 86.2029),
    "dhanbad": (23.7957, 86.4304),
    "bilaspur": (22.0797, 82.1409),
    "imphal": (24.8170, 93.9368),
    "shillong": (25.5788, 91.8933),
    "agartala": (23.8315, 91.2868),
    "aizawl": (23.7271, 92.7176),
    "itanagar": (27.0844, 93.6053),
    "gangtok": (27.3389, 88.6065),
    "panipat": (29.3909, 76.9635),
    "karnal": (29.6857, 76.9905),
    "hisar": (29.1492, 75.7217),
    "rohtak": (28.8955, 76.6066),
    "sonipat": (28.9931, 77.0151),
    "ambala": (30.3782, 76.7767),
    "mohali": (30.7046, 76.7179),
    "panchkula": (30.6942, 76.8606),
    "zirakpur": (30.6425, 76.8173),
    "haridwar": (29.9457, 78.1642),
    "rishikesh": (30.0869, 78.2676),
    "aligarh": (27.8974, 78.0880),
    "bareilly": (28.3670, 79.4304),
    "moradabad": (28.8386, 78.7733),
    "saharanpur": (29.9680, 77.5460),
    "mathura": (27.4924, 77.6737),
    "jhansi": (25.4484, 78.5685),
    "gorakhpur": (26.7606, 83.3732),
    "ajmer": (26.4499, 74.6399),
    "bikaner": (28.0229, 73.3119),
    "alwar": (27.5530, 76.6346),
    "sikar": (27.6094, 75.1399),
    "anand": (22.5645, 72.9289),
    "vapi": (20.3893, 72.9106),
    "ankleshwar": (21.6264, 73.0152),
    "jamnagar": (22.4707, 70.0577),
    "junagadh": (21.5222, 70.4579),
    "navsari": (20.9467, 72.9520),
    "amravati": (20.9374, 77.7796),
    "akola": (20.7002, 77.0082),
    "latur": (18.4088, 76.5604),
    "sangli": (16.8524, 74.5815),
    "satara": (17.6805, 74.0183),
    "ratnagiri": (16.9902, 73.3120),
    "ujjain": (23.1765, 75.7885),
    "dewas": (22.9676, 76.0534),
    "sagar": (23.8388, 78.7378),
    "rewa": (24.5362, 81.3037),
    "nellore": (14.4426, 79.9865),
    "kurnool": (15.8281, 78.0373),
    "rajahmundry": (17.0005, 81.8040),
    "kakinada": (16.9891, 82.2475),
    "anantapur": (14.6819, 77.6006),
    "karimnagar": (18.4386, 79.1288),
    "nizamabad": (18.6725, 78.0941),
    "khammam": (17.2473, 80.1514),
    "tumakuru": (13.3392, 77.1140),
    "davangere": (14.4644, 75.9218),
    "ballari": (15.1394, 76.9214),
    "kalaburagi": (17.3297, 76.8343),
    "thoothukudi": (8.7642, 78.1348),
    "tirunelveli": (8.7139, 77.7567),
    "thanjavur": (10.7870, 79.1378),
    "hosur": (12.7409, 77.8253),
    "alappuzha": (9.4981, 76.3388),
    "palakkad": (10.7867, 76.6548),
    "muzaffarpur": (26.1209, 85.3647),
    "gaya": (24.7914, 85.0002),
    "bhagalpur": (25.2425, 86.9842),
    "bokaro": (23.6693, 86.1511),
    "korba": (22.3595, 82.7501),
    "bhilai": (21.1938, 81.3509),
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

# Common aliases / alternate spellings / well-known suburbs -> a canonical key.
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
    # Renamed cities (old name still common in listings/news)
    "allahabad": "prayagraj",
    "calcutta": "kolkata",
    "madras": "chennai",
    "poona": "pune",
    "hubli": "hubballi",
    "belgaum": "belagavi",
    "mangalore": "mangaluru",
    "shimoga": "shivamogga",
    "tumkur": "tumakuru",
    "bellary": "ballari",
    "gulbarga": "kalaburagi",
    "calicut": "kozhikode",
    "trichy": "tiruchirappalli",
    "tuticorin": "thoothukudi",
    "pondicherry": "puducherry",
    "benares": "varanasi",
    "vijaywada": "vijayawada",
    # Bengaluru tech suburbs (Wellfound/job posts often name the area)
    "whitefield": "bengaluru",
    "koramangala": "bengaluru",
    "hsr layout": "bengaluru",
    "indiranagar": "bengaluru",
    "electronic city": "bengaluru",
    "marathahalli": "bengaluru",
    "jayanagar": "bengaluru",
    "yelahanka": "bengaluru",
    "hebbal": "bengaluru",
    "bellandur": "bengaluru",
    # Delhi-NCR localities
    "connaught place": "delhi",
    "saket": "delhi",
    "okhla": "delhi",
    "dwarka": "delhi",
    "rohini": "delhi",
    "nehru place": "delhi",
    "cyber city": "gurugram",
    "udyog vihar": "gurugram",
    "dlf": "gurugram",
    # Mumbai localities
    "andheri": "mumbai",
    "bandra": "mumbai",
    "powai": "mumbai",
    "lower parel": "mumbai",
    "goregaon": "mumbai",
    "malad": "mumbai",
    "vashi": "mumbai",
    "borivali": "mumbai",
    # Hyderabad localities
    "hitech city": "hyderabad",
    "hitec city": "hyderabad",
    "gachibowli": "hyderabad",
    "madhapur": "hyderabad",
    "banjara hills": "hyderabad",
    "jubilee hills": "hyderabad",
    "secunderabad": "hyderabad",
    "kondapur": "hyderabad",
    # Pune localities
    "hinjewadi": "pune",
    "baner": "pune",
    "kharadi": "pune",
    "viman nagar": "pune",
    "pimpri": "pune",
    "pimpri-chinchwad": "pune",
    "wakad": "pune",
    # Chennai localities
    "omr": "chennai",
    "guindy": "chennai",
    "t nagar": "chennai",
    "velachery": "chennai",
    "tambaram": "chennai",
    "sholinganallur": "chennai",
    # Kolkata localities
    "salt lake": "kolkata",
    "new town": "kolkata",
    "rajarhat": "kolkata",
    # Ahmedabad / Gujarat
    "sg highway": "ahmedabad",
    "prahlad nagar": "ahmedabad",
    # Goa towns
    "margao": "goa",
    "mapusa": "goa",
    "vasco da gama": "goa",
}

# Every searchable name (canonical + alias), longest first so multi-word names
# ("new delhi", "san francisco") win before their single-word substrings.
_NAMES = sorted(set(_COORDS) | set(_ALIASES), key=len, reverse=True)
_PATTERNS = [(n, re.compile(r"\b" + re.escape(n) + r"\b")) for n in _NAMES]


def canonical_city(location: Optional[str]) -> Optional[str]:
    """Return the canonical city key found in ``location`` (alias-aware), else None.

    "Bangalore"/"Bengaluru" -> "bengaluru"; "New Delhi"/"Delhi" -> "delhi";
    "Gurgaon"/"Gurugram" -> "gurugram". Used to compare a lead's location to a
    pipeline's target cities without tripping over spelling variants.
    """
    if not location:
        return None
    text = str(location).strip().lower()
    if not text:
        return None
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return _ALIASES.get(name, name)
    return None


def geocode_city(location: Optional[str]) -> Optional[tuple[float, float]]:
    """Return (lat, lng) for the first known city found in ``location``, else None.

    Matches whole words so "HSR Layout, Bangalore" -> Bengaluru and
    "Bengaluru, Karnataka, India" -> Bengaluru, while "Remote"/unknown -> None.
    """
    canon = canonical_city(location)
    return _COORDS[canon] if canon else None


def known_cities() -> list[str]:
    """Sorted, display-cased city names (canonical + common aliases) for the
    pipeline city-search autocomplete. Every name geocodes + city-filters."""
    return sorted({n.title() for n in (set(_COORDS) | set(_ALIASES))})
