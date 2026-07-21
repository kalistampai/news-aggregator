# DISPATCH — Serverless Agentic News Aggregator

A four-stage agentic pipeline that fetches your RSS feeds, scores them against a strict
offensive-security / OSINT / homelab profile, synthesizes the winners into a telegraphic
briefing, and pushes the result to a GitHub Gist. A static GitHub Pages dashboard reads
that Gist. **No server, no SMTP, no database.**

```
feeds.txt ─► ingest ─► gatekeeper (Haiku) ─► editor (Sonnet) ─► dispatch ─► Gist
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
│   ├── editor.py                 # stage 3: synthesis + URL re-attach
│   ├── dispatch.py               # stage 4: PATCH the Gist
│   ├── llm.py                    # shared Anthropic client
│   ├── run.py                    # orchestrator (runs 1→4)
│   ├── prompts/{gatekeeper,editor}.txt
│   └── requirements.txt
└── docs/                         # GitHub Pages root
    ├── index.html · style.css · script.js
```

## One-time setup
1. **Create the Gist.** New secret Gist with a file `briefing.json` containing `{}`.
   Copy its ID (the hash in the URL).
2. **Create a PAT** scoped to `gist` (classic) or fine-grained with Gist read/write.
3. **Repo secrets** (Settings → Secrets → Actions): `ANTHROPIC_API_KEY`,
   `GH_GIST_TOKEN`, `GIST_ID`.
4. **Dashboard config:** in `docs/script.js` set `CONFIG.GIST_ID` to your Gist ID.
5. **Enable Pages:** Settings → Pages → deploy from branch → `main` / `/docs`.

## Run it
- Manual: Actions tab → **daily-briefing** → *Run workflow*.
- Local: `cd pipeline && pip install -r requirements.txt`, export the three env vars,
  then `python run.py`. Intermediate artifacts (`raw_articles.json`,
  `scored_articles.json`, `briefing.json`) are written in place for inspection.

## Tuning
- **Volume:** raise/lower the score→tier thresholds in `prompts/gatekeeper.txt`.
- **Cost:** swap `GATEKEEPER_MODEL` / `EDITOR_MODEL` env vars.
- **Feeds:** edit `feeds.txt`. Sources with no discoverable RSS are logged and skipped.
