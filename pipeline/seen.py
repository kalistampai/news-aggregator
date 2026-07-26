"""
Cross-run deduplication — a seen-article set persisted in the Gist.

WHY THIS EXISTS
LOOKBACK_HOURS is 48, but the pipeline runs every 24h, so every article sits in
two consecutive windows and would be ingested, scored and published twice. The
per-run dedupe in ingest.py only looks inside a single run and cannot see that.

WHAT IS STORED
Only the 12-char SHA1 ids that ingest.py already computes from each article URL
— never the URLs themselves. 7 days of ids is ~33 KB versus ~168 KB for raw
URLs, and it keeps the Gist far below the ~1 MB inline-truncation threshold.

    seen_urls.json
    {
      "updated_at": "2026-07-26T13:17:04+00:00",
      "retention_days": 7,
      "days": { "2026-07-26": ["a1b2c3d4e5f6", ...], ... }
    }

Keyed by day so pruning is a dict comprehension rather than a scan.

API COST
  read : +1 authenticated GET, in ingest.py (stage 1) — the ONLY extra call
  write: +0 — ingest stages the merged blob on disk and dispatch.py folds it
         into the PATCH it already sends
Against GitHub's 5,000/hr authenticated limit that is ~3 calls/day, 0.06%.

FAILURE POLICY
Every operation here is best-effort and never raises into the pipeline. If the
Gist is unreachable the seen-set is treated as empty: the run proceeds with
duplicates rather than failing. If the pipeline dies before dispatch, the set is
simply not updated and the next run re-processes — the safe direction.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import requests

HERE = Path(__file__).parent
PENDING_FILE = HERE / "seen_pending.json"   # ingest -> dispatch handoff

SEEN_FILENAME = os.environ.get("GIST_SEEN_FILENAME", "seen_urls.json")
RETENTION_DAYS = int(os.environ.get("SEEN_RETENTION_DAYS", "7"))
ENABLED = os.environ.get("CROSS_RUN_DEDUPE", "1").lower() in ("1", "true", "yes")

# Hard ceiling. If the set ever exceeds this the oldest days are dropped early,
# so a runaway feed list can never bloat the Gist toward the 1 MB inline limit.
MAX_IDS = int(os.environ.get("SEEN_MAX_IDS", "50000"))

API_ROOT = "https://api.github.com/gists"
_TIMEOUT = 30


def _headers() -> dict | None:
    token = os.environ.get("GH_GIST_TOKEN")
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _empty() -> dict:
    return {"updated_at": None, "retention_days": RETENTION_DAYS, "days": {}}


def fetch_seen() -> dict:
    """Read the seen-set from the Gist. Returns an empty structure on any failure."""
    if not ENABLED:
        print("[seen] cross-run dedupe disabled (CROSS_RUN_DEDUPE=0)", flush=True)
        return _empty()

    gist_id = os.environ.get("GIST_ID")
    headers = _headers()
    if not gist_id or not headers:
        print("[seen] GIST_ID / GH_GIST_TOKEN not set — dedupe skipped", flush=True)
        return _empty()

    try:
        r = requests.get(f"{API_ROOT}/{gist_id}", headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        files = r.json().get("files", {})
        entry = files.get(SEEN_FILENAME)
        if not entry:
            print(f"[seen] no {SEEN_FILENAME} in Gist yet — first run", flush=True)
            return _empty()

        content = entry.get("content")
        if entry.get("truncated") and entry.get("raw_url"):
            content = requests.get(entry["raw_url"], timeout=_TIMEOUT).text

        data = json.loads(content or "{}")
        days = data.get("days")
        if not isinstance(days, dict):
            return _empty()
        # normalise: every value must be a list of strings
        days = {k: [str(i) for i in v]
                for k, v in days.items() if isinstance(v, list)}
        data["days"] = days
        return data
    except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
        print(f"[seen] could not load seen-set ({type(exc).__name__}) — "
              f"continuing WITHOUT cross-run dedupe", flush=True)
        return _empty()


def seen_ids(data: dict) -> set[str]:
    """Flatten the day-keyed structure into one lookup set."""
    out: set[str] = set()
    for ids in (data.get("days") or {}).values():
        out.update(ids)
    return out


def write_pending(existing: dict, ids: list[str], date: str) -> None:
    """
    ingest.py stages the ALREADY-MERGED next state here; dispatch.py uploads it
    verbatim. Merging at ingest time means the Gist is read exactly ONCE per run
    — dispatch does not need a second GET.
    """
    if not ENABLED:
        return
    try:
        merged = merge_and_prune(existing, date, ids)
        PENDING_FILE.write_text(json.dumps(merged, ensure_ascii=False),
                                encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        print(f"[seen] could not stage seen-set: {exc}", flush=True)


def read_pending() -> dict | None:
    if not PENDING_FILE.exists():
        return None
    try:
        d = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("days"), dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def merge_and_prune(existing: dict, date: str, new_ids: list[str]) -> dict:
    """Fold this run's ids into `date`, then drop days beyond the retention window."""
    days = dict(existing.get("days") or {})
    days[date] = sorted(set(days.get(date, [])) | set(new_ids))

    # keep the newest RETENTION_DAYS keys
    for old in sorted(days, reverse=True)[RETENTION_DAYS:]:
        days.pop(old, None)

    # hard ceiling: shed oldest days until under MAX_IDS
    while sum(len(v) for v in days.values()) > MAX_IDS and len(days) > 1:
        days.pop(min(days), None)

    return {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "retention_days": RETENTION_DAYS,
        "days": days,
    }


def build_gist_payload() -> tuple[str, str] | None:
    """
    Called by dispatch.py. Returns (filename, json_content) to fold into the
    PATCH it already makes, or None if there is nothing to write. Performs NO
    network I/O — ingest.py already did the single read and staged the merge.
    """
    if not ENABLED:
        return None
    merged = read_pending()
    if not merged:
        return None
    total = sum(len(v) for v in merged["days"].values())
    blob = json.dumps(merged, ensure_ascii=False)
    print(f"[seen] {total} ids across {len(merged['days'])} day(s), "
          f"{len(blob) / 1024:.1f} KB -> {SEEN_FILENAME}", flush=True)
    return SEEN_FILENAME, blob
