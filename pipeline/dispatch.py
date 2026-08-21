"""
Stage 4 — Publish (The Delivery)

Writes the day's output to Supabase Postgres:
  - briefings.<date>      -> the briefing the dashboard renders
  - feed_reports.<date>   -> the slim feed-health snapshot, so the dashboard can
                             diff days and show which sources went dark
  - seen_articles         -> ids already published, so a 48h lookback on a 24h
                             schedule cannot republish (see seen.py)

The report stored here is a SLIM copy: ingest.py's feed_report.json embeds every
article it collected, which would bloat the row for nothing — the dashboard
renders only (url, source, status, detail, counts).

Rows older than ARCHIVE_KEEP_DAYS are pruned after the write, keeping the
archive bounded exactly as the Gist prune did.

Auth is the Supabase SERVICE ROLE key from SUPABASE_SERVICE_KEY, which bypasses
RLS. No server.
"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path

import requests

import seen as seen_store
import store

HERE = Path(__file__).parent
BRIEFING = HERE / "briefing.json"
FEED_REPORT = HERE / "feed_report.json"

KEEP_DAYS = int(os.environ.get("ARCHIVE_KEEP_DAYS", "30"))


def _archive_date(payload: dict) -> str:
    """Use the briefing's own date field; fall back to today (UTC)."""
    return payload.get("date") or dt.date.today().isoformat()


def _slim_report(date: str) -> dict | None:
    """Strip article payloads from the feed report so the stored row stays small."""
    if not FEED_REPORT.exists():
        return None
    try:
        full = json.loads(FEED_REPORT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    sources = []
    for s in full.get("sources", []):
        sources.append({
            "url": s.get("url"),
            "source": s.get("source"),
            "status": s.get("status"),
            "detail": s.get("detail", ""),
            "kept": s.get("kept", 0),
            "entries_seen": s.get("entries_seen", 0),
        })
    sources.sort(key=lambda s: (s["status"] == "OK", s["source"] or ""))

    return {
        "date": date,
        "generated_at": full.get("generated_at"),
        "lookback_hours": full.get("lookback_hours"),
        "totals": full.get("totals", {}),
        "sources": sources,
    }


def _prune(date: str) -> None:
    """
    Drop archive rows outside the retention window. A calendar window, not a
    row count — with daily runs the two are identical, and after a gap the
    window is the honest reading of "keep 30 days". Best-effort: pruning must
    never fail a run whose briefing is already stored.
    """
    cutoff = (dt.date.fromisoformat(date) -
              dt.timedelta(days=KEEP_DAYS - 1)).isoformat()
    try:
        for table in ("briefings", "feed_reports"):
            store.delete(table, {"date": f"lt.{cutoff}"})
        print(f"[dispatch] pruned archive before {cutoff} "
              f"(keeping {KEEP_DAYS} days)", flush=True)
    except (store.StoreError, requests.RequestException) as exc:
        print(f"[dispatch] prune skipped ({type(exc).__name__}: {exc})", flush=True)


def main() -> None:
    payload = json.loads(BRIEFING.read_text(encoding="utf-8"))
    date = _archive_date(payload)
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    # The briefing itself is the one write that is allowed to fail the run: if
    # it does not land, there is nothing to publish and run.py should say so.
    store.upsert("briefings",
                 [{"date": date, "payload": payload, "updated_at": now}])
    print(f"[dispatch] briefings <- {date}", flush=True)

    report = _slim_report(date)
    if report:
        store.upsert("feed_reports",
                     [{"date": date, "payload": report, "updated_at": now}])
        print(f"[dispatch] feed_reports <- {date} "
              f"({len(report['sources'])} sources)", flush=True)
    else:
        print("[dispatch] no feed_report.json found — publishing briefing only",
              flush=True)

    seen_store.flush()
    _prune(date)


if __name__ == "__main__":
    main()
