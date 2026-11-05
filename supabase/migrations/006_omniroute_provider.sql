-- DISPATCH — OmniRoute credential for app_settings.
--
-- OmniRoute is the self-hosted router on ks2, reached through the
-- omni.kalistamp.lol proxy. It serves an OpenAI-compatible chat-completions
-- endpoint, so the pipeline reaches it through the same adapter as the six
-- providers in 005 (llm._compat_text); the only per-provider state is the key.
--
-- Use a key created for this site alone and limited to the combos it should
-- run: this column has the same plaintext trade as 004/005, and the proxy is
-- reachable from the internet.
--
-- Idempotent (`if not exists`), so re-running it is harmless.

begin;

alter table news_aggregator.app_settings
  add column if not exists omniroute_api_key text;

-- No new grants or policies: column additions inherit the table privileges
-- `authenticated` already holds from 004 (see 005 for the longer note).

commit;

notify pgrst, 'reload schema';
