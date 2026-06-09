# CALLFILLS — Changes & Backlog

> Captured 2026-06-07 from review notes. Tags: 🐛 bug · 🔧 improve · ✨ new.

## 1. Performance
- 🔧 **Dashboard/leads load is slow** — clients can't wait. Speed up load time.

## 2. Lead quality & scraping
- 🐛 **Junk leads** — must filter to the niche / ICP (Reddit/Facebook noise).
- 🔧 **Scrape leads from the posting source** reliably.
- 🔧 **Build our own AI scoring/verification** (don't rely only on platform signals).
- 🔧 **Funded startups** as the lead focus.
- 🔧 **Produce real Qualified leads** — too few qualify today.

## 3. Lead card / display
- ✅ Keep the **lead description**.
- ✨ Show the **source** each lead was scraped from (Reddit, etc.) on the card.
- ✅ **Source breakdown** — already exists.
- 🔧 **Replace "Signals" → "Job Posts / Leads"** (e.g. staffing: *job filled / position open*).

## 4. Filters
- 🐛 Filters are **delayed / not working**.
- ✅ Search within leads — working.
- 🐛 Filter **by Platform/source** — not working.
- 🐛 **Hiring** filter — not working.
- 🐛 **Score** filter — partly works.
- 🐛 **Qualified-only** filter — not working.

## 5. Pages / navigation
- 🐛 After a pipeline run: **Dashboard shows leads, but the "Leads" page doesn't**.
- 🐛 **Signals** page — works but delayed.
- 🐛 **Companies** page — not working.
- 🐛 **Sidebar Search** (above workspace) — not working.

## 6. Leads-page actions
- 🐛 ✨ **"Select All" not working** → select specific leads and **export only those**.

## 7. Extras / polish
- ✨ **Settings** page — build/update.
- ✨ **Help** page — build.
- ✨ **Click CALLFILLS logo → redirect to Dashboard** (quick win).

---

## Suggested order
1. **Quick wins:** logo→Dashboard, fix broken filters (platform/hiring/qualified), fix Companies + Leads pages, sidebar search.
2. **Core bet:** AI verify + score → fixes junk, niche, and qualifying leads.
3. **New pages:** Settings, Help, export-selected leads.

## To confirm
- "Signals → Job Posts / Leads" = rename/rework the Signals view to show role-level status (filled / open)?
- AI scoring + junk-filter = one AI-verification layer over free-text sources?
