"""Central configuration for the lead-intel pipeline.

All secrets and tunables are loaded from environment variables via python-dotenv.
Importing this module loads the .env file (if present) exactly once.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)

logger = logging.getLogger("lead-intel")


def _get(name: str, default: str | None = None, required: bool = False) -> str | None:
    """Read an environment variable, optionally enforcing that it is set."""
    value = os.getenv(name, default)
    if required and not value:
        logger.warning("Required environment variable %s is not set", name)
    return value


class Settings:
    """Typed accessor for all environment configuration.

    Values are read at instantiation time. Use :func:`get_settings` to obtain a
    cached singleton instance.
    """

    def __init__(self) -> None:
        # --- Third-party API credentials ---
        # Apify is the only required credential (Apify-only mode). Apollo and
        # Supabase are optional: without them the pipeline runs JSON-only and
        # leads carry no enriched contact.
        self.APIFY_API_TOKEN: str | None = _get("APIFY_API_TOKEN", required=True)
        self.APOLLO_API_KEY: str | None = _get("APOLLO_API_KEY")
        # Apollo enrichment knobs. The FREE-plan API allows Organization
        # enrichment (firmographics: industry, size, LinkedIn, company phone) but
        # GATES People Search/Match (decision-maker emails) — those need a paid
        # plan, so the person lookup is OFF by default and auto-disables on a 403.
        # Flip APOLLO_PEOPLE_ENABLED=true after upgrading to turn emails on.
        self.APOLLO_PEOPLE_ENABLED: bool = _get("APOLLO_PEOPLE_ENABLED", "false").lower() in (  # type: ignore[union-attr]
            "1", "true", "yes",
        )
        self.APOLLO_VERIFIED_ONLY: bool = _get("APOLLO_VERIFIED_ONLY", "true").lower() in (  # type: ignore[union-attr]
            "1", "true", "yes",
        )
        self.APOLLO_MAX_ENRICH: int = int(_get("APOLLO_MAX_ENRICH", "50"))  # type: ignore[arg-type]
        # Max decision-makers revealed per "Reveal contacts" click (each email
        # reveal = 1 credit on a paid plan; names/titles are free).
        self.APOLLO_MAX_PEOPLE: int = int(_get("APOLLO_MAX_PEOPLE", "5"))  # type: ignore[arg-type]
        # Auto-enrich the top N leads of every pipeline run with the FREE org
        # endpoint (industry, size, HQ city). People/emails stay on-demand only.
        self.APOLLO_AUTO_ENRICH: bool = _get("APOLLO_AUTO_ENRICH", "true").lower() in (  # type: ignore[union-attr]
            "1", "true", "yes",
        )
        self.APOLLO_AUTO_ENRICH_TOP: int = int(_get("APOLLO_AUTO_ENRICH_TOP", "25"))  # type: ignore[arg-type]
        self.SUPABASE_URL: str | None = _get("SUPABASE_URL")
        self.SUPABASE_SERVICE_KEY: str | None = _get("SUPABASE_SERVICE_KEY")
        # Anon (public) key — used for end-user auth (sign-in/up) via GoTrue.
        # Falls back to the service key if unset so auth works out of the box,
        # but you should set SUPABASE_ANON_KEY in production.
        self.SUPABASE_ANON_KEY: str | None = _get("SUPABASE_ANON_KEY")
        self.SENDGRID_API_KEY: str | None = _get("SENDGRID_API_KEY")
        self.TELEGRAM_BOT_TOKEN: str | None = _get("TELEGRAM_BOT_TOKEN")

        # --- AI verification (Groq) ---
        # Optional LLM gate that filters noisy free-text signals (Reddit, X,
        # Facebook) down to REAL, on-niche buying signals — killing the
        # "Reddit"/"AI"/meme-title junk the structural extractor can't catch.
        # Disabled automatically when GROQ_API_KEY is unset (heuristics-only).
        # Groq is an OpenAI-compatible inference cloud (console.groq.com). Set
        # GROQ_MODEL to a current production id — llama-3.3-70b-versatile (default,
        # best judgment), openai/gpt-oss-20b (fastest), or llama-3.1-8b-instant.
        self.GROQ_API_KEY: str | None = _get("GROQ_API_KEY")
        self.GROQ_BASE_URL: str = _get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")  # type: ignore[assignment]
        self.GROQ_MODEL: str = _get("GROQ_MODEL", "llama-3.3-70b-versatile")  # type: ignore[assignment]
        self.AI_VERIFY_ENABLED: bool = _get("AI_VERIFY_ENABLED", "true").lower() in (  # type: ignore[union-attr]
            "1",
            "true",
            "yes",
        )
        self.AI_VERIFY_MIN_CONFIDENCE: float = float(
            _get("AI_VERIFY_MIN_CONFIDENCE", "0.6")  # type: ignore[arg-type]
        )
        # Platforms the AI gate filters; everything else bypasses it untouched.
        self.AI_VERIFY_PLATFORMS: set[str] = {
            p.strip().lower()
            for p in (_get("AI_VERIFY_PLATFORMS", "reddit,twitter,facebook,hackernews") or "").split(",")
            if p.strip()
        }
        # Company-level AI qualification (post-dedup): one Groq verdict per
        # company — junk filter + one-line "why it's a lead" + HQ-city extraction.
        # Directory sources (Google Maps, IndiaMART) skip it — already verified.
        self.AI_QUALIFY_ENABLED: bool = _get("AI_QUALIFY_ENABLED", "true").lower() in (  # type: ignore[union-attr]
            "1", "true", "yes",
        )

        # --- Real-time radar (Signals page watcher) ---
        # Hacker News needs no key; Reddit joins automatically once its OAuth
        # creds are set. Each cycle: fetch new posts -> AI verify -> store.
        self.RADAR_ENABLED: bool = _get("RADAR_ENABLED", "true").lower() in (  # type: ignore[union-attr]
            "1", "true", "yes",
        )
        self.RADAR_INTERVAL_SECONDS: int = int(_get("RADAR_INTERVAL_SECONDS", "60"))  # type: ignore[arg-type]
        self.RADAR_VERTICAL: str = _get("RADAR_VERTICAL", "recruitment")  # type: ignore[assignment]

        # --- Crunchbase source (startups vertical) ---
        self.CRUNCHBASE_ACTOR_ID: str = _get(  # type: ignore[assignment]
            "CRUNCHBASE_ACTOR_ID", "jungle_synthesizer/crunchbase-pro-companies-scraper"
        )
        self.CRUNCHBASE_MIN_AMOUNT_USD: int = int(_get("CRUNCHBASE_MIN_AMOUNT_USD", "0"))  # type: ignore[arg-type]

        # --- Infrastructure ---
        self.REDIS_URL: str = _get("REDIS_URL", "redis://localhost:6379")  # type: ignore[assignment]

        # --- Pipeline tunables ---
        self.PIPELINE_INTERVAL_HRS: int = int(_get("PIPELINE_INTERVAL_HRS", "4"))  # type: ignore[arg-type]
        # LOOKBACK_DAYS is the overall fallback; the hiring/funding windows below
        # implement the product spec (fresh hiring posts, slightly older funding).
        self.LOOKBACK_DAYS: int = int(_get("LOOKBACK_DAYS", "90"))  # type: ignore[arg-type]
        # Hiring posts should be recent (spec: 10–20 days). Funding can be older
        # (spec: 2–3 months) because freshly-funded companies are about to hire.
        self.HIRING_LOOKBACK_DAYS: int = int(_get("HIRING_LOOKBACK_DAYS", "20"))  # type: ignore[arg-type]
        self.FUNDING_LOOKBACK_DAYS: int = int(_get("FUNDING_LOOKBACK_DAYS", "90"))  # type: ignore[arg-type]

        # --- Geography (India focus, but configurable for scaling) ---
        self.TARGET_LOCATION: str = _get("TARGET_LOCATION", "India")  # type: ignore[assignment]
        self.TARGET_COUNTRY_CODE: str = _get("TARGET_COUNTRY_CODE", "in")  # type: ignore[assignment]
        # Comma-separated city list used by city-scoped scrapers (e.g. Naukri).
        self.TARGET_CITIES: list[str] = [
            c.strip()
            for c in _get(
                "TARGET_CITIES", "bangalore,mumbai,delhi,pune,hyderabad,gurgaon"
            ).split(",")  # type: ignore[union-attr]
            if c.strip()
        ]

        # Employee-count band for LinkedIn hiring signals (spec default 10–500).
        self.LINKEDIN_MIN_EMPLOYEES: int = int(_get("LINKEDIN_MIN_EMPLOYEES", "10"))  # type: ignore[arg-type]
        self.LINKEDIN_MAX_EMPLOYEES: int = int(_get("LINKEDIN_MAX_EMPLOYEES", "500"))  # type: ignore[arg-type]

        # Storage backend: "json" (offline) or "supabase".
        self.STORAGE_BACKEND: str = _get("STORAGE_BACKEND", "json")  # type: ignore[assignment]

        # Per-source result caps keep Apify spend predictable.
        self.MAX_ITEMS_PER_SOURCE: int = int(_get("MAX_ITEMS_PER_SOURCE", "200"))  # type: ignore[arg-type]

        # --- Auth ---
        # Phase 1 uses a single hardcoded-via-env admin token and a JWT secret
        # for client-scoped tokens (legacy JSON-API auth).
        self.ADMIN_TOKEN: str = _get("ADMIN_TOKEN", "change-me-admin-token")  # type: ignore[assignment]
        self.JWT_SECRET: str = _get("JWT_SECRET", "change-me-jwt-secret")  # type: ignore[assignment]
        self.JWT_ALGORITHM: str = _get("JWT_ALGORITHM", "HS256")  # type: ignore[assignment]

        # --- Supabase Auth (dashboard login) ---
        # When True (the default), the server-rendered dashboard is gated behind
        # a Supabase-Auth login. Session tokens live in httpOnly cookies; set
        # COOKIE_SECURE=true behind HTTPS in production.
        self.AUTH_ENABLED: bool = _get("AUTH_ENABLED", "true").lower() in (  # type: ignore[union-attr]
            "1",
            "true",
            "yes",
        )
        self.COOKIE_SECURE: bool = _get("COOKIE_SECURE", "false").lower() in (  # type: ignore[union-attr]
            "1",
            "true",
            "yes",
        )
        # Optional: allow open self-service signup from the login page.
        self.ALLOW_SIGNUP: bool = _get("ALLOW_SIGNUP", "true").lower() in (  # type: ignore[union-attr]
            "1",
            "true",
            "yes",
        )
        # Show the "Continue with Google" button (requires the Google provider to
        # be enabled in the Supabase dashboard — see setup steps).
        self.GOOGLE_AUTH_ENABLED: bool = _get("GOOGLE_AUTH_ENABLED", "true").lower() in (  # type: ignore[union-attr]
            "1",
            "true",
            "yes",
        )
        # Public origin used to build OAuth redirect URLs (e.g.
        # "https://app.callfills.com"). When unset we derive it from the request,
        # which is correct for local dev (http://localhost:8000).
        self.PUBLIC_BASE_URL: str | None = _get("PUBLIC_BASE_URL")
        # Comma-separated emails that get the operator (see-all) view + the
        # client onboarding screen. Everyone else is scoped to their client ICP.
        self.OPERATOR_EMAILS: set[str] = {
            e.strip().lower()
            for e in (_get("OPERATOR_EMAILS", "") or "").split(",")
            if e.strip()
        }

        # --- Delivery defaults ---
        self.SENDGRID_FROM_EMAIL: str = _get(
            "SENDGRID_FROM_EMAIL", "leads@lead-intel.io"
        )  # type: ignore[assignment]
        self.DIGEST_TIMEZONE: str = _get("DIGEST_TIMEZONE", "Asia/Kolkata")  # type: ignore[assignment]

    def validate(self) -> list[str]:
        """Return a list of human-readable problems with the current config.

        An empty list means the configuration is usable for a full pipeline run.
        """
        problems: list[str] = []
        # Apify is the only hard requirement for a live scrape.
        if not self.APIFY_API_TOKEN:
            problems.append("Missing required env var: APIFY_API_TOKEN")
        # Supabase is required only when the Supabase backend is selected.
        if self.STORAGE_BACKEND == "supabase":
            if not self.SUPABASE_URL:
                problems.append("STORAGE_BACKEND=supabase but SUPABASE_URL is unset")
            if not self.SUPABASE_SERVICE_KEY:
                problems.append(
                    "STORAGE_BACKEND=supabase but SUPABASE_SERVICE_KEY is unset"
                )
        return problems

    @property
    def use_supabase(self) -> bool:
        """True when the Supabase backend is selected and credentials exist."""
        return (
            self.STORAGE_BACKEND == "supabase"
            and bool(self.SUPABASE_URL)
            and bool(self.SUPABASE_SERVICE_KEY)
        )

    @property
    def supabase_auth_key(self) -> str | None:
        """Key used for end-user auth — prefer the anon key, fall back to service."""
        return self.SUPABASE_ANON_KEY or self.SUPABASE_SERVICE_KEY

    @property
    def auth_ready(self) -> bool:
        """True when Supabase Auth can be used (URL + a usable key present)."""
        return bool(self.SUPABASE_URL) and bool(self.supabase_auth_key)

    @property
    def ai_verify_ready(self) -> bool:
        """True when the Groq AI verification gate is usable (enabled + key set)."""
        return self.AI_VERIFY_ENABLED and bool(self.GROQ_API_KEY)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()


settings = get_settings()
