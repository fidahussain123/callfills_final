# Dynamic ICP Targeting — design note

> Captured 2026-06. Idea: let each client define *what kind of leads* they want,
> dynamically, with no code edits and no manual YAML.

## Core principle: filter dynamically, don't fetch dynamically

| Approach | Meaning | Verdict |
|----------|---------|---------|
| Dynamic **fetching** | each client's keywords trigger their own live Apify scrape | ❌ cost explodes, slow, re-scrapes the same companies |
| Dynamic **filtering** | one broad pool scraped on a schedule; each client sees a live query over it | ✅ instant, free, scales |

This is the ZoomInfo / Apollo model: one big pool, every customer sees a dynamic slice.

## Architecture

```
ONE broad scrape (scheduled, batched)          per-client, instant, $0
  hiring + funding across India  ──► pool ──┬─► Client A ICP filter → A's dashboard
                                            ├─► Client B ICP filter → B's dashboard
                                            └─► Client C ICP filter → C's dashboard
```

### 1. ICP record per client (extends `Client.filters` in Supabase)
```json
{ "keywords": ["backend engineer"], "industries": ["fintech"],
  "cities": ["bangalore"], "employee_min": 20, "employee_max": 200,
  "signal_types": ["hiring_post","funding_round"], "min_score": 60,
  "recency_days": 20 }
```
Client edits this in the UI → their dashboard re-filters the stored pool live. No scrape, no wait, no code.

### 2. Natural-language ICP (the "wow")
Client types *"funded SaaS startups in Bangalore hiring backend engineers"* → an LLM parses it into the filter JSON above → instant tailored feed.

### 3. Operator-controlled dynamic fetch (the safe kind)
Move the hardcoded scraper keywords (`naukri._KEYWORDS`, `indeed._TITLES`, …) and geography (`settings.TARGET_*`) into a DB table, editable in the UI. The scheduled scrape reads the **union of all active keywords (deduped)** → adding a keyword expands the pool for everyone in ONE batched run, not one run per client. Retire the currently-dead `signal_keywords` vertical field.

### 4. Feedback loop (the moat, later)
Learn which leads each client marks *won* → auto-tune their ICP/scoring → leads get smarter dynamically.

## Sequencing
A dynamic filter is only worth building once the pool has good leads in it (quality fixes: Steps 3–5). Build order:
1. Lead quality (Steps 3–5).
2. Dynamic ICP system: filter-over-pool + natural-language ICP + UI-editable keywords.
3. Feedback loop.

## Where it touches the code
- `db/models.py` — `Client.filters` already exists; formalize an `ICP` shape.
- `db/store.py` — add client-scoped `get_leads(icp=...)`.
- `api/routes/dashboard.py` — scope reads to the logged-in user's client ICP.
- `scrapers/*` — read keywords/geography from config/DB instead of hardcoding.
- `pipeline._deliver_to_clients` already filters per client — reuse that logic for the dashboard.
