-- Run this ONCE before migrating the existing DISPATCH tables.
-- It creates a private, frozen rollback copy inside the same database.
-- For an off-site backup, also export these tables as CSV from Table Editor.

begin;

create schema migration_backup_news_aggregator_20260824;
revoke all on schema migration_backup_news_aggregator_20260824 from public;

create table migration_backup_news_aggregator_20260824.briefings
  (like public.briefings including all);
insert into migration_backup_news_aggregator_20260824.briefings
select * from public.briefings;

create table migration_backup_news_aggregator_20260824.feed_reports
  (like public.feed_reports including all);
insert into migration_backup_news_aggregator_20260824.feed_reports
select * from public.feed_reports;

create table migration_backup_news_aggregator_20260824.seen_articles
  (like public.seen_articles including all);
insert into migration_backup_news_aggregator_20260824.seen_articles
select * from public.seen_articles;

revoke all on all tables in schema migration_backup_news_aggregator_20260824
  from public, anon, authenticated, service_role;

commit;

select 'briefings' as table_name, count(*) as backed_up_rows
from migration_backup_news_aggregator_20260824.briefings
union all
select 'feed_reports', count(*)
from migration_backup_news_aggregator_20260824.feed_reports
union all
select 'seen_articles', count(*)
from migration_backup_news_aggregator_20260824.seen_articles;
