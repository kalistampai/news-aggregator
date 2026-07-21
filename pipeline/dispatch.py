"""
Stage 4 — Automated Gist Dispatch (The Delivery)

PATCHes the briefing into the target Gist via the GitHub API:
  - briefing.json                -> always the latest day (dashboard opens this by default)
  - briefing-YYYY-MM-DD.json     -> a dated archive copy, so past days stay browsable

Archive files older than ARCHIVE_KEEP_DAYS are pruned in the same request to keep
the Gist bounded. Auth is a PAT scoped to `gist`, read from GH_GIST_TOKEN. No server.
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

GIST_ID = os.environ["GIST_ID"]
TOKEN = os.environ["GH_GIST_TOKEN"]
LATEST_FILENAME = os.environ.get("GIST_FILENAME", "briefing.json")
KEEP_DAYS = int(os.environ.get("ARCHIVE_KEEP_DAYS", "30"))

API = f"https://api.github.com/gists/{GIST_ID}"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
ARCHIVE_RE = re.compile(r"^briefing-(\d{4}-\d{2}-\d{2})\.json$")


def _archive_date(payload: dict) -> str:
    """Use the briefing's own date field; fall back to today (UTC)."""
    return payload.get("date") or dt.date.today().isoformat()


def main() -> None:
    content = BRIEFING.read_text()
    payload = json.loads(content)
    date = _archive_date(payload)
    dated_name = f"briefing-{date}.json"

    # A single PATCH carries the latest, today's dated copy, and any deletions.
    files: dict[str, object] = {
        LATEST_FILENAME: {"content": content},
        dated_name: {"content": content},
    }

    # Prune archives beyond KEEP_DAYS. Best-effort: never block dispatch on it.
    try:
        cur = requests.get(API, headers=HEADERS, timeout=30)
        cur.raise_for_status()
        existing = cur.json().get("files", {})
        dates = sorted(
            (m.group(1) for name in existing if (m := ARCHIVE_RE.match(name))),
            reverse=True,
        )
        for old in dates[KEEP_DAYS - 1:]:      # keep newest KEEP_DAYS-1 + today
            if old != date:
                files[f"briefing-{old}.json"] = None   # null deletes the file
    except requests.RequestException:
        pass

    resp = requests.patch(API, headers=HEADERS,
                          data=json.dumps({"files": files}), timeout=30)
    resp.raise_for_status()
    raw = resp.json()["files"][LATEST_FILENAME]["raw_url"]

    pruned = sum(1 for v in files.values() if v is None)
    print(f"[dispatch] Gist updated -> {raw}", flush=True)
    print(f"[dispatch] archived {dated_name}"
          + (f", pruned {pruned} old" if pruned else ""), flush=True)


if __name__ == "__main__":
    main()
