-- DISPATCH — six more provider credentials for app_settings.
--
-- Groq, Cerebras, OpenRouter, Mistral, Cohere and Hugging Face all serve an
-- OpenAI-compatible chat-completions endpoint, so the pipeline reaches them
-- through ONE adapter (llm._compat_text) rather than six. The only per-provider
-- state is the key, which is why this migration is just columns.
--
-- Same trade as migration 004: keys sit here in plaintext, readable by any
-- signed-in user and by the service role, because the Settings tab is the single
-- source of truth. Safe only while `anon` is denied and this project has one
-- account. Adding a second user who should not hold these keys means splitting
-- the table first.
--
-- Idempotent (`if not exists`), so re-running it is harmless.

begin;

alter table news_aggregator.app_settings
  add column if not exists groq_api_key        text,
  add column if not exists cerebras_api_key    text,
  add column if not exists openrouter_api_key  text,
  add column if not exists mistral_api_key     text,
  add column if not exists cohere_api_key      text,
  add column if not exists huggingface_api_key text;

-- No new grants or policies: `authenticated` already holds SELECT/INSERT/UPDATE
-- on this table from 004, and column additions inherit it. Spelled out because
-- "why does the dashboard 403 on the new fields" is the obvious next question
-- when a migration adds columns and forgets that table privileges are per-table.

commit;

notify pgrst, 'reload schema';
