-- DISPATCH — private dashboard + UI-driven model configuration.
--
-- Two changes, both load-bearing:
--
--  1. app_settings holds WHICH model writes the briefing and the credentials to
--     do it with. The pipeline reads it at startup with the service-role key;
--     the dashboard reads and writes it as a signed-in user. It replaces the
--     model env block that used to live in .github/workflows/daily.yml, so
--     changing models is now a UI action rather than a workflow edit.
--
--  2. briefings / feed_reports move from `anon` to `authenticated`. Until now
--     the anon key could read every briefing, which would have made a login
--     screen decorative — the key ships in docs/script.js, so anyone could
--     query PostgREST directly and skip the UI entirely. Revoking anon is the
--     change that makes the gate real; the login form is only its front door.
--
-- SINGLETON, deliberately. The pipeline runs unattended at 06:17 with nobody
-- awake, and must resolve "which settings?" with no ambiguity. A per-user table
-- would leave that job guessing whose row to honour on a morning when two rows
-- disagree. One briefing a day, one configuration row.
--
-- ON KEYS AT REST: the provider API keys live in this table in plaintext,
-- readable by any signed-in user and by the service role. That is an accepted
-- trade for making the Settings tab the single source of truth (see README).
-- It is safe here only because RLS below denies `anon` outright and this
-- project has exactly one account. If you ever add a second account that should
-- NOT hold the billing keys, split this table before you create the user.

begin;

-- The dashboard now reads as `authenticated` rather than `anon`, so that role
-- needs to reach the schema at all. 001 granted usage only to anon+service_role.
grant usage on schema news_aggregator to authenticated;

create table if not exists news_aggregator.app_settings (
  id smallint primary key default 1 check (id = 1),

  -- Provider-prefixed model ids, exactly as llm.py's split_model() expects:
  -- "openai:gpt-5.6-sol", "anthropic:claude-sonnet-5", "gemini:gemini-3.5-flash".
  -- NULL means "fall back to the environment default" rather than "no model",
  -- so an unset column can never blank out the chain.
  gatekeeper_model            text,
  editor_model                text,

  -- Ordered failover chains. text[] rather than CSV: PostgREST hands these back
  -- as a JSON array, which is one less parsing step to get wrong at 06:17.
  gatekeeper_fallback_models  text[],
  editor_fallback_models      text[],

  -- Provider credentials. Empty string and NULL both mean "not configured";
  -- settings.py treats them identically so a cleared UI field behaves the same
  -- as a field that was never filled.
  openai_api_key              text,
  anthropic_api_key           text,
  gemini_api_key              text,

  -- Reasoning-effort control for the GPT-5.x family. Constrained here rather
  -- than only in the UI, because a typo reaches the API as a 400 on every
  -- single call and takes the whole run down with it.
  openai_reasoning_effort     text default 'low'
    check (openai_reasoning_effort in ('low', 'medium', 'high')),

  updated_at                  timestamptz not null default now(),
  updated_by                  uuid references auth.users (id) on delete set null
);

-- Seed the single row so the dashboard always has something to UPDATE and the
-- pipeline always has something to SELECT. All-NULL means "use env defaults",
-- so seeding cannot change pipeline behaviour on its own.
insert into news_aggregator.app_settings (id)
  values (1)
  on conflict (id) do nothing;

alter table news_aggregator.app_settings enable row level security;

-- ---------------------------------------------------------------- privileges
-- 001 revoked everything from every role and then granted back explicitly.
-- Same shape here so this migration is readable on its own.

revoke all on news_aggregator.app_settings
  from public, anon, authenticated, service_role;

-- The dashboard upserts this row, so it needs INSERT alongside UPDATE: a POST
-- with `Prefer: resolution=merge-duplicates` is rejected on the INSERT check
-- even when the row already exists and only UPDATE ends up running.
grant select, insert, update on news_aggregator.app_settings to authenticated;
grant select, insert, update on news_aggregator.app_settings to service_role;

drop policy if exists "Signed-in users read settings"
  on news_aggregator.app_settings;
create policy "Signed-in users read settings"
  on news_aggregator.app_settings
  for select to authenticated
  using (true);

drop policy if exists "Signed-in users insert settings"
  on news_aggregator.app_settings;
create policy "Signed-in users insert settings"
  on news_aggregator.app_settings
  for insert to authenticated
  with check (id = 1);

drop policy if exists "Signed-in users update settings"
  on news_aggregator.app_settings;
create policy "Signed-in users update settings"
  on news_aggregator.app_settings
  for update to authenticated
  using (true)
  with check (id = 1);

-- --------------------------------------------- close the dashboard to `anon`
-- This is the half that makes the login screen mean something. After this, the
-- publishable key in docs/script.js identifies the project but reads nothing;
-- every SELECT needs a user JWT in the Authorization header.

drop policy if exists "Public can read briefings"
  on news_aggregator.briefings;
drop policy if exists "Public can read feed reports"
  on news_aggregator.feed_reports;

revoke select on news_aggregator.briefings,
                 news_aggregator.feed_reports
  from anon;

grant select on news_aggregator.briefings,
                news_aggregator.feed_reports
  to authenticated;

create policy "Signed-in users read briefings"
  on news_aggregator.briefings
  for select to authenticated
  using (true);

create policy "Signed-in users read feed reports"
  on news_aggregator.feed_reports
  for select to authenticated
  using (true);

commit;

notify pgrst, 'reload schema';
