-- Canonical database definition for DISPATCH in the shared Supabase project.

begin;

create schema if not exists news_aggregator;
revoke all on schema news_aggregator from public;
grant usage on schema news_aggregator to anon, service_role;

create table if not exists news_aggregator.briefings (
  date date primary key,
  payload jsonb not null,
  updated_at timestamptz not null default now()
);

create table if not exists news_aggregator.feed_reports (
  date date primary key,
  payload jsonb not null,
  updated_at timestamptz not null default now()
);

create table if not exists news_aggregator.seen_articles (
  id text primary key,
  seen_on date not null
);

create index if not exists seen_articles_seen_on_idx
  on news_aggregator.seen_articles (seen_on);

alter table news_aggregator.briefings enable row level security;
alter table news_aggregator.feed_reports enable row level security;
alter table news_aggregator.seen_articles enable row level security;

revoke all on all tables in schema news_aggregator
  from public, anon, authenticated, service_role;

grant select on news_aggregator.briefings,
                news_aggregator.feed_reports
  to anon;

grant select, insert, update, delete
  on news_aggregator.briefings,
     news_aggregator.feed_reports,
     news_aggregator.seen_articles
  to service_role;

drop policy if exists "Public can read briefings"
  on news_aggregator.briefings;
create policy "Public can read briefings"
  on news_aggregator.briefings
  for select to anon
  using (true);

drop policy if exists "Public can read feed reports"
  on news_aggregator.feed_reports;
create policy "Public can read feed reports"
  on news_aggregator.feed_reports
  for select to anon
  using (true);

-- Future objects are private until a later migration grants them explicitly.
alter default privileges for role postgres in schema news_aggregator
  revoke all on tables from public, anon, authenticated, service_role;
alter default privileges for role postgres in schema news_aggregator
  revoke all on sequences from public, anon, authenticated, service_role;
alter default privileges for role postgres in schema news_aggregator
  revoke execute on functions from public, anon, authenticated, service_role;

commit;

notify pgrst, 'reload schema';
