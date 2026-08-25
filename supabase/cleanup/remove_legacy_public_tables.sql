-- OPTIONAL AND DESTRUCTIVE. Run only after the new schema has passed a manual
-- workflow run and the dashboard has been verified. The frozen backup schema
-- is intentionally retained.

begin;

do $cleanup$
begin
  if to_regclass('news_aggregator.briefings') is null
     or to_regclass('news_aggregator.feed_reports') is null
     or to_regclass('news_aggregator.seen_articles') is null then
    raise exception 'news_aggregator is incomplete; refusing cleanup';
  end if;

  if exists (
    select 1 from public.briefings old
    where not exists (
      select 1 from news_aggregator.briefings new where new.date = old.date)
  ) or exists (
    select 1 from public.feed_reports old
    where not exists (
      select 1 from news_aggregator.feed_reports new where new.date = old.date)
  ) or exists (
    select 1 from public.seen_articles old
    where not exists (
      select 1 from news_aggregator.seen_articles new where new.id = old.id)
  ) then
    raise exception 'target schema is missing legacy rows; refusing cleanup';
  end if;
end
$cleanup$;

drop table public.briefings;
drop table public.feed_reports;
drop table public.seen_articles;

commit;

notify pgrst, 'reload schema';
