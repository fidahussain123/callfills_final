-- lead-intel database schema (Supabase / PostgreSQL)
-- Run this against your Supabase project SQL editor before first pipeline run.

-- ──────────────────────────────────────────────────────────────────────────
-- clients: tenant agencies consuming leads
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clients (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text NOT NULL,
    vertical         text NOT NULL,
    slack_webhook    text,
    telegram_chat_id text,
    email_to         text,
    filters          jsonb DEFAULT '{}'::jsonb,
    min_score        int DEFAULT 60,
    plan             text DEFAULT 'trial',
    active           boolean DEFAULT true,
    created_at       timestamptz DEFAULT now()
);

-- ──────────────────────────────────────────────────────────────────────────
-- companies: deduplicated by domain
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS companies (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name           text NOT NULL,
    domain         text UNIQUE,
    employee_count int,
    industry       text,
    hq_country     text,
    description    text,
    tech_stack     text[],
    created_at     timestamptz DEFAULT now(),
    updated_at     timestamptz DEFAULT now()
);

-- ──────────────────────────────────────────────────────────────────────────
-- signals: raw intent signals tied to a company
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid REFERENCES companies(id) ON DELETE CASCADE,
    signal_type     text,
    source_platform text,
    raw_text        text,
    source_url      text,
    detected_at     timestamptz DEFAULT now(),
    metadata        jsonb DEFAULT '{}'::jsonb
);

-- ──────────────────────────────────────────────────────────────────────────
-- leads: scored, client-scoped leads
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leads (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid REFERENCES companies(id) ON DELETE CASCADE,
    client_id       uuid REFERENCES clients(id) ON DELETE CASCADE,
    vertical        text,
    contact_name    text,
    contact_role    text,
    email           text,
    linkedin_url    text,
    score           int,
    score_breakdown jsonb DEFAULT '{}'::jsonb,
    status          text DEFAULT 'new',
    created_at      timestamptz DEFAULT now()
);

-- ──────────────────────────────────────────────────────────────────────────
-- Indexes
-- ──────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_companies_domain
    ON companies (domain);

CREATE INDEX IF NOT EXISTS idx_signals_company_platform_detected
    ON signals (company_id, source_platform, detected_at);

CREATE INDEX IF NOT EXISTS idx_leads_client_score_status_created
    ON leads (client_id, score, status, created_at);

-- Prevent duplicate signals from the same URL.
CREATE UNIQUE INDEX IF NOT EXISTS uq_signals_source_url
    ON signals (source_url)
    WHERE source_url IS NOT NULL;

-- ──────────────────────────────────────────────────────────────────────────
-- Rich lead-card columns (idempotent; safe to re-run).
-- The lead-builder produces a full card; these columns persist the headline
-- fields while ``signals`` keeps every piece of evidence (date + url + snippet).
-- ──────────────────────────────────────────────────────────────────────────
ALTER TABLE companies ADD COLUMN IF NOT EXISTS website   text;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS city      text;

ALTER TABLE leads ADD COLUMN IF NOT EXISTS company_name       text;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS website            text;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS location           text;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS summary            text;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS has_hiring         boolean DEFAULT false;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS has_funding        boolean DEFAULT false;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS has_social         boolean DEFAULT false;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS hiring_role_count  int DEFAULT 0;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS hiring_recency_days  int;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_recency_days int;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_round      text;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_amount     text;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS signal_sources     jsonb DEFAULT '[]'::jsonb;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS signal_types       jsonb DEFAULT '[]'::jsonb;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS signals            jsonb DEFAULT '[]'::jsonb;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS meets_threshold    boolean DEFAULT false;

-- ──────────────────────────────────────────────────────────────────────────
-- lead_pool: the shared, client-agnostic generated lead pool the dashboard
-- renders (then filters per-tenant in Python via apply_icp). Persisting it here
-- means leads survive restarts and show on ephemeral-disk deploys (e.g. Render),
-- instead of living only in local data/leads.json. The full LeadCard is stored
-- in `card` (jsonb) for lossless reads; the promoted columns enable indexing.
-- ``leads`` (above) stays the per-client DELIVERED-lead ledger; this is the POOL.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_pool (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name    text NOT NULL,
    vertical        text NOT NULL,
    company_domain  text,
    location        text,
    score           int DEFAULT 0,
    has_hiring      boolean DEFAULT false,
    has_funding     boolean DEFAULT false,
    meets_threshold boolean DEFAULT false,
    card            jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at      timestamptz DEFAULT now()
);

-- One row per (company, vertical); upsert refreshes it each run.
CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_pool_name_vertical
    ON lead_pool (company_name, vertical);

CREATE INDEX IF NOT EXISTS idx_lead_pool_vertical_score
    ON lead_pool (vertical, score DESC);

-- ──────────────────────────────────────────────────────────────────────────
-- profiles: links a Supabase Auth user (auth.users) to a tenant + role.
-- A profile row is created automatically on signup via the trigger below, so
-- every authenticated user maps to exactly one app identity. ``client_id`` is
-- nullable until an admin assigns the user to a tenant agency.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS profiles (
    id          uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email       text,
    full_name   text,
    client_id   uuid REFERENCES clients(id) ON DELETE SET NULL,
    role        text NOT NULL DEFAULT 'member',  -- 'admin' | 'member'
    created_at  timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profiles_client ON profiles (client_id);

-- Auto-create a profile whenever a new auth user signs up.
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
    INSERT INTO public.profiles (id, email, full_name)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data ->> 'full_name', NEW.raw_user_meta_data ->> 'name')
    )
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- Backfill profiles for users who signed up BEFORE this trigger existed
-- (the trigger only fires on new inserts). Safe to re-run.
INSERT INTO public.profiles (id, email, full_name)
SELECT id, email, COALESCE(raw_user_meta_data ->> 'full_name', raw_user_meta_data ->> 'name')
FROM auth.users
ON CONFLICT (id) DO NOTHING;

-- Row Level Security: a user may read/update only their own profile. The
-- pipeline uses the service-role key, which bypasses RLS entirely.
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "profiles_select_own" ON profiles;
CREATE POLICY "profiles_select_own" ON profiles
    FOR SELECT USING (auth.uid() = id);

DROP POLICY IF EXISTS "profiles_update_own" ON profiles;
CREATE POLICY "profiles_update_own" ON profiles
    FOR UPDATE USING (auth.uid() = id);
