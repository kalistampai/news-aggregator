"""
Stage 4 — Automated Gist Dispatch (The Delivery)

PATCHes the briefing AND the feed health report into the target Gist:
  - briefing.json                -> latest day (dashboard opens this by default)
  - briefing-YYYY-MM-DD.json     -> dated archive copy, so past days stay browsable
  - feedreport.json              -> latest feed health snapshot
  - feedreport-YYYY-MM-DD.json   -> dated archive, so the dashboard can diff days
                                    and show which sources went dark / recovered

The report published here is a SLIM copy: ingest.py's feed_report.json embeds every
article it collected, which would bloat the Gist. We strip `items` and keep only
what the dashboard renders (url, source, status, detail, counts).

Archive files older than ARCHIVE_KEEP_DAYS are pruned in the same request, for both
briefings and reports, keeping the Gist bounded. Auth is a PAT scoped to `gist`,
read from GH_GIST_TOKEN. No server.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
from pathlib import Path

import requests

HERE = Path(__file__).parent
BRIEFING = HERE / "briefing.json"
FEED_REPORT = HERE / "feed_report.json"

GIST_ID = os.environ["GIST_ID"]
TOKEN = os.environ["GH_GIST_TOKEN"]
LATEST_FILENAME = os.environ.get("GIST_FILENAME", "briefing.json")
REPORT_FILENAME = os.environ.get("GIST_REPORT_FILENAME", "feedreport.json")
KEEP_DAYS = int(os.environ.get("ARCHIVE_KEEP_DAYS", "30"))

API = f"https://api.github.com/gists/{GIST_ID}"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
BRIEF_RE = re.compile(r"^briefing-(\d{4}-\d{2}-\d{2})\.json$")
REPORT_RE = re.compile(r"^feedreport-(\d{4}-\d{2}-\d{2})\.json$")


def _archive_date(payload: dict) -> str:
    """Use the briefing's own date field; fall back to today (UTC)."""
    return payload.get("date") or dt.date.today().isoformat()


def _slim_report(date: str) -> str | None:
    """Strip article payloads from the feed report so the Gist stays small."""
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

    return json.dumps({
        "date": date,
        "generated_at": full.get("generated_at"),
        "lookback_hours": full.get("lookback_hours"),
        "totals": full.get("totals", {}),
        "sources": sources,
    }, indent=2, ensure_ascii=False)


def main() -> None:
    content = BRIEFING.read_text(encoding="utf-8")
    payload = json.loads(content)
    date = _archive_date(payload)

    files: dict[str, object] = {
        LATEST_FILENAME: {"content": content},
        f"briefing-{date}.json": {"content": content},
    }

    report = _slim_report(date)
    if report:
        files[REPORT_FILENAME] = {"content": report}
        files[f"feedreport-{date}.json"] = {"content": report}
    else:
        print("[dispatch] no feed_report.json found — publishing briefing only",
              flush=True)

    # Prune archives beyond KEEP_DAYS. Best-effort: never block dispatch on it.
    pruned = 0
    try:
        cur = requests.get(API, headers=HEADERS, timeout=30)
        cur.raise_for_status()
        existing = cur.json().get("files", {})
        for rx, prefix in ((BRIEF_RE, "briefing"), (REPORT_RE, "feedreport")):
            dates = sorted(
                (m.group(1) for name in existing if (m := rx.match(name))),
                reverse=True,
            )
            for old in dates[KEEP_DAYS - 1:]:      # keep newest KEEP_DAYS-1 + today
                if old != date:
                    files[f"{prefix}-{old}.json"] = None   # null deletes the file
                    pruned += 1
    except requests.RequestException:
        pass

    resp = requests.patch(API, headers=HEADERS,
                          data=json.dumps({"files": files}), timeout=30)
    resp.raise_for_status()
    raw = resp.json()["files"][LATEST_FILENAME]["raw_url"]

    print(f"[dispatch] Gist updated -> {raw}", flush=True)
    print(f"[dispatch] archived briefing-{date}.json"
          + (f" + feedreport-{date}.json" if report else "")
          + (f", pruned {pruned} old" if pruned else ""), flush=True)


if __name__ == "__main__":
    main()
