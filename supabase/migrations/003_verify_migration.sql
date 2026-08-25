-- Read-only migration checks. Run after 001 and 002, and again after cutover.

select 'briefings' as table_name,
       (select count(*) from public.briefings) as public_rows,
       (select count(*) from news_aggregator.briefings) as target_rows,
       (select count(*) from news_aggregator.briefings) >=
         (select count(*) from public.briefings) as row_check_passed
union all
select 'feed_reports',
       (select count(*) from public.feed_reports),
       (select count(*) from news_aggregator.feed_reports),
       (select count(*) from news_aggregator.feed_reports) >=
         (select count(*) from public.feed_reports)
union all
select 'seen_articles',
       (select count(*) from public.seen_articles),
       (select count(*) from news_aggregator.seen_articles),
       (select count(*) from news_aggregator.seen_articles) >=
         (select count(*) from public.seen_articles);

select has_schema_privilege('anon', 'news_aggregator', 'usage')
         as anon_has_schema_usage,
       has_table_privilege('anon', 'news_aggregator.briefings', 'select')
         as anon_can_read_briefings,
       has_table_privilege('anon', 'news_aggregator.feed_reports', 'select')
         as anon_can_read_feed_reports,
       not has_table_privilege('anon', 'news_aggregator.seen_articles', 'select')
         as anon_cannot_read_seen_articles,
       not has_table_privilege(
         'anon', 'news_aggregator.briefings', 'insert, update, delete')
         as anon_cannot_write_briefings;

select n.nspname as schema_name,
       c.relname as table_name,
       c.relrowsecurity as rls_enabled
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'news_aggregator'
  and c.relname in ('briefings', 'feed_reports', 'seen_articles')
order by c.relname;
