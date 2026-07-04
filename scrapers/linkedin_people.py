"""LinkedIn People source — find PEOPLE (founders/CTOs) directly, not companies.

Person-first path: given job titles + location (+ optional company size /
industry), it searches LinkedIn profiles and returns each person — name, title,
company, LinkedIn URL, email and **follower count** — then drops anyone above
``max_followers`` (the "low followers" filter). Powers pipelines like "startup
founders in Bangalore with under 1000 followers".

Two search actors are supported (auto-detected from ``actor_id``):

* ``apimaestro/linkedin-profile-search-scraper`` — **default**, pay-per-result
  (uses Apify credits, no run cap). Searches one title × one location per run,
  so we fan out across the pipeline's titles/cities.
* ``harvestapi/linkedin-profile-search`` — array input, single run, richer
  filters (industry, company headcount). Its FREE tier caps at 10 runs, so it's
  only used when the operator explicitly picks it (e.g. after renting it).

An incompatible "Use this" actor (a profile-DETAIL scraper that needs URLs)
falls back to the default search actor.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from db.models import NormalizedSignal, RawSignal
from scrapers.base import ApifyScraper
from scrapers.registry import register_scraper

logger = logging.getLogger("lead-intel.scrapers.linkedin_people")

_APIMAESTRO = "apimaestro/linkedin-profile-search-scraper"
_HARVEST = "harvestapi/linkedin-profile-search"

# Region words apimaestro (single-location search) can't resolve to a geo.
_NON_GEO = {"europe", "asia", "worldwide", "global", "emea", "apac", "americas"}

# LinkedIn headcount codes → we pick the small-startup bands from a max size.
_HEADCOUNT = [("A", 0), ("B", 10), ("C", 50), ("D", 200), ("E", 500),
              ("F", 1000), ("G", 5000), ("H", 10000), ("I", 10**9)]

# LinkedIn v2 industry codes (harvestapi ``industryIds``). A pipeline's free-text
# Industries field is matched — by substring — against these keywords so the
# search is HARD-filtered to the right sector (else "manufacturing" leads would
# be silently ignored). Broad category codes keep the net wide; a few sub-codes
# are added where the parent alone misses common cases (freight, warehousing).
# Full list: github.com/HarvestAPI/linkedin-industry-codes-v2
_INDUSTRY_CODES: dict[str, list[str]] = {
    "manufacturing": ["25"],
    "pharma": ["15"], "pharmaceutical": ["15"], "pharmaceuticals": ["15"],
    "logistics": ["116", "87", "93"], "supply chain": ["116", "87", "93"],
    "transportation": ["116", "92"], "transport": ["116", "92"],
    "freight": ["87"], "warehousing": ["93"], "shipping": ["116", "95"],
    "software": ["4"], "saas": ["4"], "it services": ["4"], "tech": ["4"],
    "fintech": ["43", "4"], "financial": ["43"], "finance": ["43"],
    "banking": ["41"], "insurance": ["42"], "accounting": ["47"],
    "healthcare": ["14"], "health care": ["14"], "medical": ["14"],
    "construction": ["48"], "real estate": ["44"], "retail": ["27"],
    "wholesale": ["133"], "ecommerce": ["27"], "e-commerce": ["27"],
    "marketing": ["1862"], "advertising": ["1862"], "design": ["99"],
    "consulting": ["1810"], "professional services": ["1810"],
    "telecom": ["8"], "telecommunications": ["8"],
    "utilities": ["59"], "energy": ["59"],
    "chemical": ["54"], "chemicals": ["54"],
    "food": ["23"], "beverage": ["23"],
}


def industry_ids(names: list[str]) -> list[str]:
    """Resolve free-text industry names → unique LinkedIn industryIds (ordered)."""
    codes: list[str] = []
    for raw in names:
        req = str(raw).strip().lower()
        if not req:
            continue
        for key, ids in _INDUSTRY_CODES.items():
            if key in req or req in key:
                for cid in ids:
                    if cid not in codes:
                        codes.append(cid)
    return codes


@register_scraper
class LinkedInPeopleScraper(ApifyScraper):
    """Search LinkedIn profiles by title/location and filter by follower count."""

    key = "linkedin_people"
    source_platform = "linkedin_profile"
    actor_id = _APIMAESTRO          # default: pay-per-result, no run cap
    _DEFAULT_ACTOR = _APIMAESTRO

    def __init__(self) -> None:
        super().__init__()
        self._titles: list[str] = ["Founder", "Co-Founder", "CEO", "CTO"]
        self._locations: list[str] = []
        self._industries: list[str] = []
        self._max_followers: Optional[int] = None
        self._company_max: Optional[int] = None
        self._max: int = 25
        self._email: bool = True

    def configure(self, cfg: dict[str, Any]) -> None:
        """Inject filters from the pipeline ICP + AI-parsed description."""
        titles = cfg.get("job_titles")
        if not titles and "google_maps" not in (cfg.get("sources") or []):
            # Keywords double as job titles ONLY when Maps isn't also enabled —
            # in a combined pipeline the keywords are the Maps search category
            # ("dental clinic"), not people titles; default titles apply instead.
            titles = cfg.get("keywords")
        if titles:
            self._titles = [str(t).strip() for t in titles if str(t).strip()]
        locs = cfg.get("locations") or cfg.get("cities")
        if locs:
            self._locations = [str(c).strip() for c in locs if str(c).strip()]
        if cfg.get("industries"):
            self._industries = [str(i).strip() for i in cfg["industries"] if str(i).strip()]
        if cfg.get("max_followers") is not None:
            try:
                self._max_followers = int(cfg["max_followers"])
            except (TypeError, ValueError):
                pass
        if cfg.get("employee_max") is not None:
            try:
                self._company_max = int(cfg["employee_max"])
            except (TypeError, ValueError):
                pass
        if cfg.get("max_results"):
            try:
                self._max = max(1, int(cfg["max_results"]))
            except (TypeError, ValueError):
                pass
        # "Use this" — run the operator's chosen actor (must be a people-search actor).
        if (cfg.get("actor_id") or "").strip():
            self.actor_id = cfg["actor_id"].strip()

    # --- actor dispatch -----------------------------------------------------
    def _ensure_actor(self) -> None:
        """Force a SUPPORTED people-search actor (harvestapi or apimaestro).

        Anything else — a profile-detail scraper that needs URLs — can't do a
        filtered search and would return 0, so fall back to the default.
        """
        aid = (self.actor_id or "").strip()
        if aid == _HARVEST or "apimaestro/linkedin-profile-search" in aid:
            return
        logger.warning(
            "LinkedIn People: %r isn't a supported people-search actor — using %s",
            aid or "(none)", self._DEFAULT_ACTOR,
        )
        self.actor_id = self._DEFAULT_ACTOR

    def fetch(self, lookback_days: int) -> list[RawSignal]:
        self._ensure_actor()
        if "apimaestro" in self.actor_id:
            return self._fetch_apimaestro()
        return self._fetch_harvest()

    def normalize(self, raw: RawSignal) -> Optional[NormalizedSignal]:
        it = raw.data
        if not isinstance(it, dict):
            return None
        # apimaestro nests everything under basic_info; harvestapi is flat.
        if "basic_info" in it:
            return self._normalize_apimaestro(it)
        return self._normalize_harvest(it)

    # --- apimaestro (default) ----------------------------------------------
    def _fetch_apimaestro(self) -> list[RawSignal]:
        """Fan out one title × one location per run (the actor's search unit)."""
        titles = self._titles[:3] or ["Founder"]
        locs = [l for l in self._locations if l.lower() not in _NON_GEO][:2] or [None]
        combos = [(t, l) for t in titles for l in locs]
        per = max(3, -(-self._max // len(combos)))  # ceil-divide, ≥3 per run
        kept: list[RawSignal] = []
        fetched = dropped = 0
        for title, loc in combos:
            run_input: dict[str, Any] = {
                "current_job_title": title,
                "max_profiles": per,
                "include_email": self._email,
            }
            if loc:
                run_input["location"] = loc
            for it in self.run_actor(run_input, max_items=per):
                fetched += 1
                bi = it.get("basic_info") if isinstance(it, dict) else None
                fc = (bi or {}).get("follower_count")
                if self._max_followers is not None and isinstance(fc, (int, float)) and fc > self._max_followers:
                    dropped += 1
                    continue
                kept.append(RawSignal(source_platform=self.source_platform, data=it))
            if len(kept) >= self._max:
                kept = kept[: self._max]
                break
        logger.info(
            "LinkedIn People (apimaestro): %d combos, %d fetched, kept %d "
            "(max_followers=%s, dropped %d over cap)",
            len(combos), fetched, len(kept), self._max_followers, dropped,
        )
        return kept

    def _normalize_apimaestro(self, it: dict[str, Any]) -> Optional[NormalizedSignal]:
        bi = it.get("basic_info") or {}
        if not isinstance(bi, dict):
            return None
        name = (bi.get("fullname")
                or ((bi.get("first_name") or "") + " " + (bi.get("last_name") or "")).strip())
        if not name:
            return None
        loc = bi.get("location") if isinstance(bi.get("location"), dict) else {}
        city = loc.get("city") or loc.get("full") or loc.get("country")
        pid = bi.get("public_identifier")
        url = bi.get("profile_url") or (f"https://linkedin.com/in/{pid}" if pid else "")
        email = bi.get("email")
        return NormalizedSignal(
            company_name=name,
            company_domain=None,
            signal_type="linkedin_profile",
            source_platform=self.source_platform,
            raw_text=(bi.get("headline") or name),
            source_url=url,
            detected_at=self.cutoff(0),
            metadata={
                "headline": bi.get("headline"),
                "followers": bi.get("follower_count"),
                "connections": bi.get("connection_count"),
                "linkedin_url": url,
                "email": email if isinstance(email, str) else None,
                "company": bi.get("current_company"),
                "location": city,
                "is_influencer": bi.get("is_influencer"),
            },
        )

    # --- harvestapi (opt-in, rented) ---------------------------------------
    def build_input(self, lookback_days: int = 0) -> dict[str, Any]:
        run_input: dict[str, Any] = {
            "profileScraperMode": "Full + email search" if self._email else "Full",
            "currentJobTitles": self._titles,
            "maxItems": self._max,
        }
        if self._locations:
            run_input["locations"] = self._locations
        if self._industries:
            ind = industry_ids(self._industries)
            if ind:
                run_input["industryIds"] = ind
        if self._company_max:
            codes = [code for code, hi in _HEADCOUNT if hi and self._company_max >= 0 and lo_ok(code, self._company_max)]
            if codes:
                run_input["companyHeadcount"] = codes
        return run_input

    def _fetch_harvest(self) -> list[RawSignal]:
        items = self.run_actor(self.build_input())
        kept: list[RawSignal] = []
        dropped = 0
        for it in items:
            fc = it.get("followerCount") if isinstance(it, dict) else None
            if self._max_followers is not None and isinstance(fc, (int, float)) and fc > self._max_followers:
                dropped += 1
                continue
            kept.append(RawSignal(source_platform=self.source_platform, data=it))
        logger.info(
            "LinkedIn People (harvestapi): %d profiles, kept %d (max_followers=%s, dropped %d over cap)",
            len(items), len(kept), self._max_followers, dropped,
        )
        return kept

    def _normalize_harvest(self, it: dict[str, Any]) -> Optional[NormalizedSignal]:
        name = ((it.get("firstName") or "") + " " + (it.get("lastName") or "")).strip()
        if not name:
            return None
        loc = it.get("location")
        parsed = (loc.get("parsed") if isinstance(loc, dict) else None) or {}
        if not isinstance(parsed, dict):
            parsed = {}
        pos = it.get("currentPosition")
        if isinstance(pos, list):
            pos = pos[0] if pos else {}
        if not isinstance(pos, dict):
            pos = {}

        def _email_str(v: Any) -> Optional[str]:
            if isinstance(v, dict):
                return v.get("email")
            return v if isinstance(v, str) else None
        email = _email_str(it.get("email"))
        if not email:
            elist = it.get("emails")
            if isinstance(elist, list) and elist:
                email = _email_str(elist[0])
        return NormalizedSignal(
            company_name=name,
            company_domain=None,
            signal_type="linkedin_profile",
            source_platform=self.source_platform,
            raw_text=(it.get("headline") or name),
            source_url=it.get("linkedinUrl") or "",
            detected_at=self.cutoff(0),
            metadata={
                "headline": it.get("headline"),
                "followers": it.get("followerCount"),
                "connections": it.get("connectionsCount"),
                "linkedin_url": it.get("linkedinUrl"),
                "email": email,
                "company": pos.get("companyName"),
                "location": parsed.get("city") or parsed.get("text"),
                "is_influencer": it.get("influencer"),
            },
        )


def lo_ok(code: str, company_max: int) -> bool:
    """True if a headcount band's lower bound is within the requested max size."""
    prev = 0
    for c, hi in _HEADCOUNT:
        if c == code:
            return prev <= company_max
        prev = hi
    return False
