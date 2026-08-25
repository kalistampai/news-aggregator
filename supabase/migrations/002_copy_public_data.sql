-- Copy the current live rows into the new schema without changing public.
-- Safe to rerun: rows are upserted by their primary keys.

begin;

do $migration$
begin
  if to_regclass('public.briefings') is not null then
    execute $sql$
      insert into news_aggregator.briefings (date, payload, updated_at)
      select date, payload, coalesce(updated_at, now()) from public.briefings
      on conflict (date) do update
      set payload = excluded.payload,
          updated_at = excluded.updated_at
    $sql$;
  end if;

  if to_regclass('public.feed_reports') is not null then
    execute $sql$
      insert into news_aggregator.feed_reports (date, payload, updated_at)
      select date, payload, coalesce(updated_at, now()) from public.feed_reports
      on conflict (date) do update
      set payload = excluded.payload,
          updated_at = excluded.updated_at
    $sql$;
  end if;

  if to_regclass('public.seen_articles') is not null then
    execute $sql$
      insert into news_aggregator.seen_articles (id, seen_on)
      select id, coalesce(seen_on, current_date) from public.seen_articles
      on conflict (id) do update
      set seen_on = excluded.seen_on
    $sql$;
  end if;
end
$migration$;

commit;

select 'briefings' as table_name, count(*) as copied_rows
from news_aggregator.briefings
union all
select 'feed_reports', count(*) from news_aggregator.feed_reports
union all
select 'seen_articles', count(*) from news_aggregator.seen_articles;

notify pgrst, 'reload schema';
