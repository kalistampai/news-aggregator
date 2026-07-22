"""
Feed auditor — standalone dead-link / blocked-source checker.

Runs the SAME fetch + classification logic as ingest.py but makes zero LLM calls
and writes nothing the pipeline depends on. Use it to vet feeds.txt any time,
especially right after adding sources in bulk.

  cd pipeline
  python feedcheck.py                 # audit every URL, print a table
  python feedcheck.py --failures      # only sources contributing nothing
  python feedcheck.py --json out.json # machine-readable dump

Exit code is 0 unless --strict is passed, in which case any failing source
exits 1 (handy if you ever want CI to block on a broken feed list).
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import sys
from collections import Counter

from ingest import (FAILING, LOOKBACK_HOURS, MAX_WORKERS, STATUS_HELP,
                    load_feed_urls, parse_feed)
import concurrent.futures as cf


def audit(urls: list[str]) -> list[dict]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)
    out: list[dict] = []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(parse_feed, u, cutoff): u for u in urls}
        for fut in cf.as_completed(futures):
            u = futures[fut]
            try:
                rec = fut.result()
            except Exception as exc:                       # noqa: BLE001
                rec = {"url": u, "source": u, "feed_url": None,
                       "status": "CONN_ERROR",
                       "detail": f"{type(exc).__name__}: {exc}"[:160],
                       "entries_seen": 0, "kept": 0, "dropped": {}, "items": []}
            out.append(rec)
            done += 1
            if done % 25 == 0:
                print(f"  ...{done}/{len(urls)} checked", flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit feeds.txt for dead/blocked sources.")
    ap.add_argument("--failures", action="store_true",
                    help="only show sources contributing nothing")
    ap.add_argument("--json", metavar="PATH", help="write full results as JSON")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any source is failing")
    args = ap.parse_args()

    urls = load_feed_urls()
    print(f"[feedcheck] auditing {len(urls)} sources "
          f"(lookback {LOOKBACK_HOURS}h, {MAX_WORKERS} workers)\n", flush=True)

    records = audit(urls)
    records.sort(key=lambda r: (r["status"] != "OK", r["status"], r["source"]))
    counts = Counter(r["status"] for r in records)
    failures = [r for r in records if r["status"] in FAILING]

    shown = failures if args.failures else records
    print(f"\n{'STATUS':<12} {'KEPT':>4}  {'SEEN':>4}  SOURCE")
    print("-" * 78)
    for r in shown:
        print(f"{r['status']:<12} {r['kept']:>4}  {r['entries_seen']:>4}  "
              f"{r['source']}"
              + (f"   [{r['detail']}]" if r["detail"] else ""))

    print("\n" + "=" * 78)
    print(f"{len(records)} sources | OK {counts.get('OK', 0)} | "
          f"not contributing {len(failures)}")
    for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if status != "OK":
            print(f"  {status:<12} {n:>3}   {STATUS_HELP.get(status, '')}")

    if failures:
        print("\nURLs to fix or remove from feeds.txt:")
        for r in failures:
            print(f"  [{r['status']}] {r['url']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "totals": dict(counts), "sources": records},
                      fh, indent=2, ensure_ascii=False)
        print(f"\n[feedcheck] wrote {args.json}")

    if args.strict and failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
