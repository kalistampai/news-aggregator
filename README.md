# DISPATCH — Serverless Agentic News Aggregator

A four-stage agentic pipeline that fetches your RSS feeds, scores them against a strict
offensive-security / OSINT / homelab profile, synthesizes the winners into a telegraphic
briefing, and pushes the result to a GitHub Gist. A static GitHub Pages dashboard reads
that Gist. **No server, no SMTP, no database.**

```
feeds.txt ─► ingest ─► gatekeeper (Gemini Flash-Lite) ─► editor (Gemini Flash) ─► dispatch ─► Gist
                                                                                    │
                                                             GitHub Pages dashboard ┘ (reads raw JSON)
```

## Layout
```
news-aggregator/
├── .github/workflows/daily.yml   # cron scheduler (GitHub Actions)
├── pipeline/
│   ├── feeds.txt                 # your source URLs (auto-feed-discovered)
│   ├── ingest.py                 # stage 1: fetch + discover + dedupe
│   ├── gatekeeper.py             # stage 2: batched relevance scoring + tiering
│   ├── editor.py                 # stage 3: batched synthesis + URL re-attach
│   ├── dispatch.py               # stage 4: PATCH the Gist
│   ├── llm.py                    # shared Google Gemini client (retries + model failover)
│   ├── run.py                    # orchestrator (runs 1→4)
│   ├── prompts/{gatekeeper,editor}.txt
│   └── requirements.txt
└── docs/                         # GitHub Pages root
    ├── index.html · style.css · script.js
```

## Models
The pipeline runs on **Google Gemini** (largest free tier). Defaults, overridable via env:

- `GATEKEEPER_MODEL` — `gemini-3.1-flash-lite` (GA; cheap, high-volume scoring)
- `EDITOR_MODEL` — `gemini-3.5-flash` (GA; synthesis)
- `EDITOR_FALLBACK_MODELS` — comma-separated list; the editor fails over to these, in
  order, if the primary model is transiently unavailable (503 / overload). Defaults to
  the gatekeeper model, which is already exercised each run and therefore known-reachable.

Newer GA alternatives exist if you want to swap: `gemini-3.6-flash` (cheaper and more
token-efficient than 3.5 Flash) or `gemini-3.5-flash-lite` (newest low-cost lite tier).
Just change the env vars.

> Note: Gemini 3.x deprecates the `temperature` / `top_p` / `top_k` sampling params
> (silently ignored on the newest models). `llm.py` no longer sends them — forced-JSON
> output plus the schema in each prompt keep responses well-formed.

## One-time setup
1. **Create the Gist.** New secret Gist with a file `briefing.json` containing `{}`.
   Copy its ID (the hash in the URL).
2. **Create a PAT** scoped to `gist` (classic) or fine-grained with Gist read/write.
3. **Get a Gemini API key** from Google AI Studio (the free tier is sufficient).
4. **Repo secrets** (Settings → Secrets → Actions): `GEMINI_API_KEY`,
   `GH_GIST_TOKEN`, `GIST_ID`.
5. **Dashboard config:** in `docs/script.js` set `CONFIG.GIST_ID` to your Gist ID.
6. **Enable Pages:** Settings → Pages → deploy from branch → `main` / `/docs`.

## Run it
- Manual: Actions tab → **daily-briefing** → *Run workflow*.
- Local: `cd pipeline && pip install -r requirements.txt`, export the three env vars
  (`GEMINI_API_KEY`, `GH_GIST_TOKEN`, `GIST_ID`), then `python run.py`. Intermediate
  artifacts (`raw_articles.json`, `scored_articles.json`, `briefing.json`) are written
  in place for inspection.

## Scheduling
GitHub Actions cron is **UTC only and does not observe daylight saving.** The default
`0 12 * * *` is 05:00 PDT (summer) / 04:00 PST (winter); no single fixed cron can be
5 AM Pacific year-round. Scheduled runs are also best-effort and can be delayed at the
top of the hour under load — shift a few minutes past `:00` if punctuality matters. See
the comments in `daily.yml`.

## Tuning
- **Volume:** raise/lower the score→tier thresholds in `prompts/gatekeeper.txt`.
- **Cost / capacity:** swap `GATEKEEPER_MODEL` / `EDITOR_MODEL` and set
  `EDITOR_FALLBACK_MODELS` via env.
- **Resilience:** the editor synthesizes in batches and, if a batch can't be produced
  even after retries + fallback, emits minimal "degraded" cards so the briefing still
  ships. Set `EDITOR_STRICT=1` to instead abort the whole run on any unrecoverable
  editor failure (leaving the previous day's briefing in the Gist).
- **Feeds:** edit `feeds.txt`. Sources with no discoverable RSS are logged and skipped.
