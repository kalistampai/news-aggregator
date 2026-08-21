"""
Supabase (PostgREST) client — the pipeline's persistence layer.

Replaces the GitHub Gist that used to hold the briefing, the feed report and the
cross-run seen-set. Three tables, defined in supabase/schema.sql:

    briefings      (date PK, payload jsonb, updated_at)
    feed_reports   (date PK, payload jsonb, updated_at)
    seen_articles  (id PK, seen_on date)

AUTH
The pipeline runs headless in GitHub Actions on behalf of nobody, so it uses the
SERVICE ROLE key, which bypasses RLS. That key is a full-database credential: it
belongs in Actions secrets and must never appear in docs/ or reach a browser.
The dashboard reads the same tables with the ANON key, which RLS restricts to
SELECT on briefings and feed_reports.

WHY RAW REST RATHER THAN supabase-py
Every call here is one table hit — no auth session, no realtime, no storage.
`requests` is already a dependency and the Gist code it replaces had the same
shape, so this stays four functions instead of a new dependency tree.
"""
from __future__ import annotations

import json
import os

import requests

_TIMEOUT = 30
_PAGE = 1000       # rows requested per GET; the server may return fewer
_CHUNK = 500       # rows per write request, so one POST body stays small
_MAX_PAGES = 200   # runaway guard if a server ignores limit/offset


class StoreError(RuntimeError):
    """Supabase rejected the request, or is not configured."""


def configured() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and
                os.environ.get("SUPABASE_SERVICE_KEY"))


def _conf() -> tuple[str, str]:
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or ""
    if not url or not key:
        raise StoreError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY are not set — nothing can be "
            "published. See README 'One-time setup'.")
    return url, key


def _headers(extra: dict | None = None) -> dict:
    _, key = _conf()
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _endpoint(table: str) -> str:
    url, _ = _conf()
    return f"{url}/rest/v1/{table}"


def _check(resp: requests.Response, what: str) -> None:
    """
    Surface PostgREST's own {message, hint, code} body. The two mistakes that
    actually happen — a wrong key and a missing table — are both a bare 401/404
    without it, and indistinguishable from each other.
    """
    if resp.ok:
        return
    raise StoreError(f"{what} failed: HTTP {resp.status_code} "
                     f"{resp.text.strip()[:400]}")


def select(table: str, params: dict) -> list[dict]:
    """
    GET every matching row, paging past whatever per-response row cap the
    project carries (`db-max-rows`, which a project can set and Supabase can
    change under you).

    Two things here are deliberate:

    * `limit`/`offset` rather than a Range header. PostgREST answers a Range
      that starts past the end of the collection with 416, which would surface
      as a hard error at the exact moment the table's size crosses a multiple
      of _PAGE. limit/offset just returns [].
    * Paging by the number of rows ACTUALLY returned, not by _PAGE. If the
      server's cap is lower than we asked for, stepping by _PAGE would skip
      every row in between — and a silent short read here quietly resurrects
      already-published articles, which is the one bug this table exists to
      prevent.

    `order` is required: offset paging over an unordered result is not stable,
    so rows could be seen twice or missed entirely between pages.
    """
    if "order" not in params:
        raise StoreError(f"select from {table} needs an explicit `order` — "
                         f"offset paging over an unordered result is not stable")
    out: list[dict] = []
    for _ in range(_MAX_PAGES):
        r = requests.get(_endpoint(table), headers=_headers(),
                         params={**params, "limit": _PAGE, "offset": len(out)},
                         timeout=_TIMEOUT)
        _check(r, f"select from {table}")
        batch = r.json()
        if not batch:
            return out
        out.extend(batch)
    print(f"[store] WARNING: stopped paging {table} at {len(out)} rows", flush=True)
    return out


def upsert(table: str, rows: list[dict]) -> int:
    """INSERT ... ON CONFLICT DO UPDATE, keyed by the table's primary key."""
    if not rows:
        return 0
    headers = _headers({"Prefer": "resolution=merge-duplicates,return=minimal"})
    for i in range(0, len(rows), _CHUNK):
        chunk = rows[i:i + _CHUNK]
        r = requests.post(
            _endpoint(table), headers=headers,
            data=json.dumps(chunk, ensure_ascii=False).encode("utf-8"),
            timeout=_TIMEOUT)
        _check(r, f"upsert into {table}")
    return len(rows)


def delete(table: str, params: dict) -> None:
    """
    DELETE with a PostgREST filter, e.g. delete("briefings", {"date": "lt.…"}).
    PostgREST refuses an unfiltered DELETE, so a dropped filter cannot empty a
    table by accident.
    """
    if not params:
        raise StoreError(f"refusing to DELETE from {table} with no filter")
    r = requests.delete(_endpoint(table), headers=_headers({"Prefer": "return=minimal"}),
                        params=params, timeout=_TIMEOUT)
    _check(r, f"delete from {table}")
