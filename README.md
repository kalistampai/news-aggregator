# DISPATCH — Serverless Agentic News Aggregator

A four-stage agentic pipeline that fetches your RSS feeds, scores them against a strict
offensive-security / OSINT / homelab profile, synthesizes the winners into a telegraphic
briefing, and stores the result in Supabase Postgres. A static GitHub Pages dashboard
reads it back. **No server, no SMTP.**

```
                          ┌─ app_settings (which model, whose keys) ─┐
                          ▼                                          │
feeds.txt ─► ingest ─► gatekeeper ─► editor ─► dispatch ─► Supabase   │
                                                              │      │
                            GitHub Pages dashboard ────────────┴──────┘
                            (sign-in required; settings written here)
```

### Storage
Four tables in the custom `news_aggregator` schema:

| Table | Key | Holds |
| --- | --- | --- |
| `briefings` | `date` | the day's briefing, exactly the JSON the editor produced |
| `feed_reports` | `date` | the slim feed-health snapshot the dashboard renders |
| `seen_articles` | `id` | article ids already published, for cross-run dedupe |
| `app_settings` | `id=1` | model choice + provider API keys, written by the dashboard |

**Two keys, two jobs.** The pipeline writes with the **service_role** key, which
bypasses Row Level Security — a full-database credential that lives only in Actions
secrets. The dashboard uses the **publishable** key, which is committed to `docs/` on
purpose: it identifies the project and nothing more.

**The dashboard is private.** Migration `004` moved `SELECT` on `briefings` and
`feed_reports` from `anon` to `authenticated`, so the publishable key on its own reads
*nothing*. Every table request carries the signed-in user's JWT. The sign-in screen is
the front door; the grant is the lock. A login gate with `anon` still able to `SELECT`
would be decoration — anyone could read the key out of `docs/script.js` and query
PostgREST directly.

**RLS is load-bearing, not hygiene.** Database grants and RLS together decide what a
browser may do. `seen_articles` carries RLS with no policy at all, which denies every
role except `service_role`.

**Keys at rest.** `app_settings` holds the provider API keys in plaintext, readable by
any signed-in user and by the service role. That is the accepted trade for making the
Settings tab the single source of truth, and it is safe here only because `anon` is
denied outright and this project has exactly one account. Split the table before you
create a second user who should not hold the billing keys.

## Layout
```
news-aggregator/
├── .github/workflows/daily.yml   # cron scheduler (GitHub Actions)
├── pipeline/
│   ├── feeds.txt                 # your source URLs (auto-feed-discovered)
│   ├── ingest.py                 # stage 1: fetch + discover + dedupe
│   ├── gatekeeper.py             # stage 2: batched relevance scoring + tiering
│   ├── editor.py                 # stage 3: batched synthesis + URL re-attach
│   ├── store.py                  # Supabase (PostgREST) client
│   ├── settings.py               # loads model config from app_settings
│   ├── dispatch.py               # stage 4: write the day's rows
│   ├── llm.py                    # shared model client (OpenAI + Anthropic + Gemini)
│   ├── test_llm_fallback.py      # offline simulation of the failover chain
│   ├── test_gatekeeper_parsing.py # verdict-shape + empty-briefing guard tests
│   ├── run.py                    # orchestrator (runs 1→4)
│   ├── prompts/{gatekeeper,editor}.txt
│   └── requirements.txt
└── docs/                         # GitHub Pages root
    ├── index.html · style.css · script.js
```

## Models

**Choose the model in the dashboard, not in this repo.** Sign in → ⚙ Settings → Models.
That writes `app_settings`, and `pipeline/settings.py` loads it before the first stage
runs. The workflow file no longer carries model configuration at all.

> Changes apply to the **next** scheduled run. Nothing in the dashboard regenerates
> today's briefing — the pipeline is a cron job, not a live service.

Three providers are supported — **OpenAI**, **Anthropic** and **Google Gemini**. Every
model id is provider-prefixed (`openai:` / `anthropic:` / `gemini:`), so one ordered list
can cross providers and the whole chain stays a single code path. An unprefixed id is
inferred from its name (`gpt-*` → OpenAI, `claude-*` → Anthropic, `gemini-*` → Gemini).

| Setting | Column | Meaning |
| --- | --- | --- |
| Gatekeeper / Editor model | `gatekeeper_model`, `editor_model` | first choice per stage |
| Failover chains | `*_fallback_models` | tried in order when the primary is busy or dry |
| Provider keys | `openai_api_key`, … | credentials the run uses |
| Reasoning effort | `openai_reasoning_effort` | `low` / `medium` / `high`, GPT-5.x only |

**Keep a second provider in the chain.** This job runs at 06:17 Pacific with nobody
awake: if the primary key is missing, the credit runs dry, or a model id turns out to be
wrong, the chain walks past it and something else still writes the briefing.

**Every field falls back to the environment.** A `NULL` column, an unreadable
`app_settings`, an unreachable Supabase, or `SETTINGS_FROM_DB=0` all degrade to the
defaults in `daily.yml` — logged loudly, never fatal. A configuration read must not be
able to cost you a morning's briefing. The provider secrets stay in Actions as an
optional backstop for exactly that case; `llm.api_key()` prefers the stored value.

Model ids are verified against `/v1/models` once per run (`LLM_RESOLVE_MODELS=1`), so a
family name that needs a dated snapshot id is corrected and logged rather than 404-ing
every call all night. Set it to `0` to use the ids exactly as written.

Model ids are verified against `/v1/models` once per run (`LLM_RESOLVE_MODELS=1`), so a
family name that needs a dated snapshot id is corrected and logged rather than 404-ing
every call all night. Set it to `0` to use the ids exactly as written.

### Cost
List price per 1M tokens: **Sol $5 in / $30 out**, **Terra $2.50 / $15**, **Luna $1 / $6**.
A run makes roughly one gatekeeper call per 30 articles (~50) plus one editor call per
20 feature-tier articles (~6), so ~60–70 calls before retries — on the order of **$3–4 a
morning with Sol on both stages**, i.e. about two weeks on $50.

To stretch it, point the *gatekeeper* at `openai:gpt-5.6-luna`: it is ~90% of the calls,
and the writing you actually read comes from the editor. Both stages currently lead with
Sol, as configured.

GPT-5.6 is a reasoning family, which has two consequences the pipeline handles
explicitly: reasoning tokens are billed as output tokens, and they come out of the same
budget as the answer. `OPENAI_REASONING_EFFORT` (default `low`) keeps deliberation
proportional to the task, and `OPENAI_TOKEN_HEADROOM` (default `4000`) is added on top of
each stage's `max_tokens` so a long think cannot truncate the JSON.

### Which model actually ran
The configured model and the model that produced the briefing are **not** the same thing
whenever a failover happens — which is exactly when you want to know. After each
successful response `llm.py` records the model that answered; `editor.py` writes it into
`briefing.json` under `models`, and the dashboard shows it in the masthead as
`via <model-id>` (amber, marked `(fallback)`, when it differs from the configured one).
The tooltip carries the configured model, the gatekeeper's model, and the failover log.

```json
"models": {
  "editor":     {"configured": "openai:gpt-5.6-sol", "primary": "openai:gpt-5.6-sol",
                 "effective": "openai:gpt-5.6-terra",
                 "counts": {"openai:gpt-5.6-terra": 6}, "fell_back": true,
                 "usage": {"prompt": 120000, "completion": 40000,
                           "reasoning": 9000, "total": 160000, "calls": 11}},
  "gatekeeper": {"configured": "openai:gpt-5.6-sol", "primary": "openai:gpt-5.6-sol",
                 "effective": "openai:gpt-5.6-sol",
                 "counts": {"openai:gpt-5.6-sol": 5}, "fell_back": false,
                 "usage": {"prompt": 600000, "completion": 90000,
                           "reasoning": 20000, "total": 690000, "calls": 34}},
  "events": ["openai:gpt-5.6-sol is overloaded (503) — trying the next: openai:gpt-5.6-terra"],
  "alerts": []
}
```

### Token usage
`usage` is the token cost of the day's run, accumulated per stage across every batch
(one request per gatekeeper/editor batch, hence `calls`). The dashboard sums both
stages and shows the total next to the model tag as `850K tokens`; the tooltip breaks
it down by stage and by input/output, and calls out how much of the output was
reasoning tokens — those are billed as output and are already inside the output count,
not additional to it.

Two deliberate choices:

* **`usage` is `null`, never zeros, when nothing was reported.** A provider that omits
  token counts and a run that genuinely cost nothing are different claims. The
  dashboard hides the counter entirely for the first, rather than publishing a `0`
  that looks like a measurement. Briefings archived before this landed have no `usage`
  key at all and hide it the same way.
* **It is not a billing figure, and the tooltip says so.** Only responses that came
  back are counted; a request that errored or timed out has no usage object to read
  yet may still have been charged. Expect it to read slightly low on a night with
  retries.

The record is keyed by **stage**, not by model id. Keying it by model id merged the
two stages whenever both were configured to the same model, and on 2026-08-02 that
made an empty briefing claim `via openai:gpt-5.6-sol` — the gatekeeper's five calls,
credited to an editor that never ran. A stage with no successful response now
reports `effective: null`, and the dashboard says "model not recorded".

`primary` is the first choice *after* id resolution, so a family name that resolved
to a dated snapshot is not mistaken for a failover.

This value is **display only**. Nothing reads it back to decide what to call: routing
always restarts from the configured model, so a model that was merely busy today is
still tried first tomorrow. The dashboard mirrors it to `localStorage`
(`dispatch.model.v1`) so the label still describes the output after a reload — including
a reload that can't reach Supabase, where it is shown dimmed as the last known value.

### Failure handling
`llm.py` sorts every error into one of three buckets, by HTTP status first and message
text only as a fallback:

| Bucket | Examples | Behaviour |
| --- | --- | --- |
| **busy** | 5xx, `overloaded`, `try again later`, timeouts, dropped connections | retry with backoff, then fail over to the next model — printed, never silent |
| **soft** | empty / truncated / non-JSON / safety-blocked output | retry the same model, then fail over; never reported as an outage |
| **unusable** | 401/403 bad key, 404 unknown model, exhausted credit | advance to the next candidate **and** raise an ACTION NEEDED alert. A key problem rules out that provider's other models immediately; an unknown id rules out only itself |
| **fatal** | 400 malformed request | raised immediately — it fails identically on every model |

Unattended runs favour shipping over stopping: an unusable provider is walked past, not
died on, because at 06:17 nobody can fix a key. It is never *swallowed* — the reason is
printed as `ACTION NEEDED`, stored in `briefing.json` under `models.alerts`, and turns
the dashboard's model label red with a ⚠ until the next clean run.

- Every failover prints `<model> is overloaded (503) — trying the next: <model>`.
- When **every** candidate is busy, the run aborts with a distinct message saying the
  condition is temporary and nothing was written (exit code 75). `dispatch.py` never
  runs, so the stored briefing is untouched. No model-discovery call is made on that
  path — it would be a wasted round trip against an API that is already failing.
- `LLM_BUSY_COOLDOWN` (default `120`s) keeps a model that just 503'd out of the rotation
  for the rest of the run, so 20 batches don't each re-discover the same outage. It is
  in-memory only — the next process starts clean.
- `LLM_RETRY_429` (**default on**) treats a rate limit as *busy*: back off, then fail
  over. An unattended run should ride out a quota spike rather than die on it. Set it to
  `0` when debugging by hand to have 429s stop the run immediately. An OpenAI 429 that is
  really `insufficient_quota` is detected by message and treated as *unusable* either
  way — a dry account does not recover by waiting.
- Pacing is per provider: `OPENAI_MIN_INTERVAL` (default `1`s) and `GEMINI_MIN_INTERVAL`
  (default `13`s, which is 4.6 RPM — under the free tier's 5 RPM ceiling). `LLM_MIN_INTERVAL`
  sets both at once.

#### A model's cap is not the provider's cap
**Quotas are metered per model**, so the likeliest overnight failure — one model
exhausting its own bucket — is answered by the next model on the *same API key*. Put
two or more models from one provider in the chain and a single-vendor setup still
survives a cap.

This needs saying because it is a trap: a Gemini per-model quota error repeats OpenAI's
out-of-credit message word for word — *"You exceeded your current quota, please check
your plan and billing details."* Matching `billing` classified a throttled Gemini model
as a dead **account**, marked the whole provider unusable, and skipped every remaining
Gemini model for the run. `llm.py` now discriminates on the machine-readable parts
instead (`QuotaFailure` / `quotaMetric` / `rate_limit_exceeded` vs. `insufficient_quota`
/ `credit balance`), so:

- a **per-model cap** → *busy*, model-scoped: siblings on the same key are still tried
- a **dead account or bad key** → *unusable*, provider-wide: its other models are skipped,
  because retrying a key that cannot pay only spends the run's time budget
- a **per-day cap** → advanced past after **one** request rather than six, since the
  retry ladder would burn ~2 minutes of a 60-minute budget to earn six more refusals

`test_llm_fallback.py` pins all four behaviours, including a guard against
over-correcting the other way.

### Never publish over a good briefing
`briefings` holds the only copy of a day's briefing and `dispatch.py` upserts by date,
so an empty result must never be allowed to overwrite it. The pipeline aborts (exit 75, `dispatch` never runs) when:

- ingest kept no articles — usually everything was already published earlier today;
- articles were scored but **none** came back usable — a broken response contract,
  not a quiet news day;
- every scored article was rejected (`EMPTY_OK=1` publishes anyway);
- features went into the editor and zero cards came out.

**JSON-object mode matters here.** OpenAI's `json_object` format cannot return a
bare top-level array, so the gatekeeper prompt asks for `{"verdicts": [...]}`. On
2026-08-02 the prompt still said "JSON array only": all five batches returned valid
JSON, the wrapper key was unrecognised, 130 articles scored zero, and an empty
briefing replaced a good one with no error in the log. `_unwrap` now accepts any
wrapper (including an object keyed by article id), ids are compared as strings, and
an unrecognised shape is printed rather than silently dropped.

Cross-run dedupe marks an article published as soon as it survives ingest, so a bad
run "spends" its articles. To rebuild that day, run the workflow manually with the
**"Recovery: re-ingest articles already marked published"** checkbox ticked — it
sets `CROSS_RUN_DEDUPE=0` for that run only. Confirm it applied by looking for
`[seen] cross-run dedupe DISABLED` in the log, or `cross_run_suppressed: 0` in the
feed report — a recovery run that silently kept dedupe on scores the handful of
leftovers and looks like a scoring failure.

### Sections safety net
A briefing with notable items but no feature-tier cards renders as a bare link strip
with no sections — correct scoring, but it reads as broken. When nothing clears the
feature bar, the gatekeeper promotes the highest-scoring notable items
(`PROMOTE_MIN_SCORE`, default 6; at most `PROMOTE_MAX`, default 6) and says so in the
log. `PROMOTE_ON_EMPTY=0` restores strict tiering and accepts a section-less day.

This exists because tier promotion is a *ratio* judgement — the prompt asks the model
to calibrate so ~20% of a batch reaches 7+ — and a small or weak article pool can
legitimately produce zero. On 2026-08-02 a 17-article pool (the rest suppressed by
dedupe) topped out at a single 7, which is a real outcome, not a bug.

Simulate the whole chain without touching the API — no key or network needed:

```
cd pipeline && python test_llm_fallback.py        # provider chain + failover
cd pipeline && python test_gatekeeper_parsing.py  # verdict shapes + the guard
cd pipeline && python test_supabase_store.py      # storage, paging, prune, RLS-less stub
cd pipeline && python test_settings.py            # dashboard settings -> model chain
```

> Note: Gemini 3.x deprecates the `temperature` / `top_p` / `top_k` sampling params
> (silently ignored on the newest models). `llm.py` no longer sends them — forced-JSON
> output plus the schema in each prompt keep responses well-formed.

## One-time setup
1. **Create the Supabase project** (or reuse one), then run the migrations in
   `supabase/migrations/` in order — `001` provisions the tables, `004` adds
   `app_settings` and closes the dashboard to `anon`. Add the `news_aggregator`
   schema under Project Settings → Data API → Exposed schemas.
2. **Copy both keys** from Project Settings → API: the **publishable** key for the
   dashboard and the `service_role` key for the pipeline.
3. **Create the dashboard user.** Authentication → Users → *Add user*. This is the
   only account that can read the briefing after migration `004`. There is no sign-up
   flow in `docs/` on purpose — the dashboard is for one person.
4. **Repo secrets** (Settings → Secrets → Actions): `SUPABASE_URL` and
   `SUPABASE_SERVICE_KEY` are the only *required* ones now.
   `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` are optional backstops
   for the morning Supabase is unreachable — normally the keys come from Settings.
5. **Dashboard config:** in `docs/script.js` set `CONFIG.SUPABASE_URL`,
   `CONFIG.SUPABASE_ANON_KEY`, and `CONFIG.SUPABASE_SCHEMA`. The **publishable** key,
   never `service_role`. The shared production project uses `news_aggregator`.
6. **Sign in and pick your models.** Open the dashboard, sign in as the user from
   step 3, then ⚙ Settings → paste a provider key → *Refresh* to list the models that
   key can actually see → choose the gatekeeper and editor models → *Save*. *Test
   provider keys* verifies each one before it has to work unattended.
7. **Publish the dashboard.** The live site is served from the **separate
   `kalistampai/news` repo**, which holds byte-identical copies of `docs/`
   (`index.html`, `script.js`, `style.css`, `.nojekyll`) — not from this repo's
   `/docs`. A front-end change is not live until it is pushed to **both**.

## Run it
- Manual: Actions tab → **daily-briefing** → *Run workflow*.
- Local: `cd pipeline && pip install -r requirements.txt`, export `SUPABASE_URL` and
  `SUPABASE_SERVICE_KEY`, then `python run.py` — model choice and provider keys are
  read from `app_settings`. To run entirely on env vars instead, set
  `SETTINGS_FROM_DB=0` and export `GATEKEEPER_MODEL` / `EDITOR_MODEL` / the provider
  key yourself. Intermediate artifacts (`raw_articles.json`, `scored_articles.json`,
  `briefing.json`) are written in place for inspection.

## Scheduling
GitHub Actions cron is **UTC only and does not observe daylight saving.** The default
`0 12 * * *` is 05:00 PDT (summer) / 04:00 PST (winter); no single fixed cron can be
5 AM Pacific year-round. Scheduled runs are also best-effort and can be delayed at the
top of the hour under load — shift a few minutes past `:00` if punctuality matters. See
the comments in `daily.yml`.

## Tuning
- **Volume:** raise/lower the score→tier thresholds in `prompts/gatekeeper.txt`.
- **Cost / capacity:** change the gatekeeper and editor models, and their failover
  chains, in the dashboard's ⚙ Settings tab. No commit, no workflow edit.
- **Resilience:** the editor synthesizes in batches and, if a batch can't be produced
  even after retries + fallback, emits minimal "degraded" cards so the briefing still
  ships. Set `EDITOR_STRICT=1` to instead abort the whole run on any unrecoverable
  editor failure (leaving the previous day's briefing stored). Degrading applies
  to *output* failures only: an all-models-busy outage or a fatal error aborts either
  way, since degrading those would publish a gutted briefing over a good one.
- **Feeds:** edit `feeds.txt`. Sources with no discoverable RSS are logged and skipped.
