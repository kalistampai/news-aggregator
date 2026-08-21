"""
Cross-run deduplication — a seen-article set persisted in Supabase.

WHY THIS EXISTS
LOOKBACK_HOURS is 48, but the pipeline runs every 24h, so every article sits in
two consecutive windows and would be ingested, scored and published twice. The
per-run dedupe in ingest.py only looks inside a single run and cannot see that.

WHAT IS STORED
Only the 12-char SHA1 ids that ingest.py already computes from each article URL
— never the URLs themselves.

    seen_articles
      id       text primary key   -- "a1b2c3d4e5f6"
      seen_on  date               -- the run that first kept it

A ROW PER ID, not the single JSON document the Gist held. Pruning is a DELETE
against an index rather than a read-modify-write of the whole blob, two runs
cannot clobber each other's ids, and the ~1 MB inline-document ceiling that
forced a SEEN_MAX_IDS ceiling to exist is gone with it.

WHEN IT IS WRITTEN
ingest.py stages the ids it kept to disk; dispatch.py flushes them AFTER the
briefing is stored. A run that dies before dispatch therefore does not "spend"
its articles — the next run re-ingests them, which is the safe direction.

FAILURE POLICY
Every operation here is best-effort and never raises into the pipeline. If
Supabase is unreachable the seen-set reads as empty and the run proceeds with
duplicates rather than failing; a failed flush simply means the next run
re-processes. Publishing the briefing is what must not fail — that is
dispatch.py's job, and it does raise.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import requests

import store

HERE = Path(__file__).parent
PENDING_FILE = HERE / "seen_pending.json"   # ingest -> dispatch handoff

TABLE = "seen_articles"
RETENTION_DAYS = int(os.environ.get("SEEN_RETENTION_DAYS", "7"))
ENABLED = os.environ.get("CROSS_RUN_DEDUPE", "1").lower() in ("1", "true", "yes")


def _cutoff(today: dt.date | None = None) -> str:
    """Oldest day still inside the retention window."""
    today = today or dt.date.today()
    return (today - dt.timedelta(days=RETENTION_DAYS - 1)).isoformat()


def fetch_seen() -> set[str]:
    """
    Ids published inside the retention window. Returns an empty set on any
    failure — the run continues with duplicates rather than dying.
    """
    if not ENABLED:
        print("[seen] cross-run dedupe DISABLED (CROSS_RUN_DEDUPE=0) — "
              "already-published articles will be re-ingested", flush=True)
        return set()
    # Stated either way: "did the recovery switch actually apply?" is otherwise
    # only answerable by counting suppressed articles in the feed report.
    print("[seen] cross-run dedupe ENABLED (CROSS_RUN_DEDUPE=1)", flush=True)

    if not store.configured():
        print("[seen] SUPABASE_URL / SUPABASE_SERVICE_KEY not set — dedupe "
              "skipped", flush=True)
        return set()

    try:
        rows = store.select(TABLE, {"select": "id",
                                    "seen_on": f"gte.{_cutoff()}",
                                    "order": "id"})
        ids = {str(r["id"]) for r in rows if r.get("id")}
        print(f"[seen] {len(ids)} id(s) known from the last "
              f"{RETENTION_DAYS} day(s)", flush=True)
        return ids
    except (store.StoreError, requests.RequestException, ValueError) as exc:
        print(f"[seen] could not load seen-set ({type(exc).__name__}: {exc}) — "
              f"continuing WITHOUT cross-run dedupe", flush=True)
        return set()


def write_pending(ids: list[str], date: str) -> None:
    """
    ingest.py stages the ids it kept; dispatch.py uploads them once the briefing
    is safely stored. Staging on disk (rather than writing here) is what keeps a
    failed run from marking its articles published.
    """
    if not ENABLED:
        return
    try:
        PENDING_FILE.write_text(
            json.dumps({"date": date, "ids": sorted(set(ids))},
                       ensure_ascii=False),
            encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        print(f"[seen] could not stage seen-set: {exc}", flush=True)


def read_pending() -> tuple[str, list[str]] | None:
    if not PENDING_FILE.exists():
        return None
    try:
        d = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("ids"), list) and d.get("date"):
            return str(d["date"]), [str(i) for i in d["ids"]]
    except (OSError, json.JSONDecodeError):
        pass
    return None


def flush() -> int:
    """
    Called by dispatch.py after the briefing is stored. Upserts this run's ids
    and prunes anything past the retention window. Best-effort: a failure here
    costs a day of dedupe, never the briefing.
    """
    if not ENABLED:
        return 0
    pending = read_pending()
    if not pending:
        return 0
    date, ids = pending
    if not ids:
        return 0

    try:
        store.upsert(TABLE, [{"id": i, "seen_on": date} for i in ids])
        print(f"[seen] {len(ids)} id(s) recorded for {date}", flush=True)
        store.delete(TABLE, {"seen_on": f"lt.{_cutoff(dt.date.fromisoformat(date))}"})
        return len(ids)
    except (store.StoreError, requests.RequestException, ValueError) as exc:
        print(f"[seen] could not record seen-set ({type(exc).__name__}: {exc}) — "
              f"the next run will re-ingest today's articles", flush=True)
        return 0
