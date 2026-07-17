"""
Stage 1 — Automated Ingestion (The Fetch)

Reads feeds.txt, auto-discovers an RSS/Atom feed for each URL (most entries are
homepage/blog-index links, not raw feeds), parses entries published in the last
LOOKBACK_HOURS, dedupes, and writes raw_articles.json.

No LLM calls here. Pure network + parsing. Designed to never hard-fail on one
dead source: every feed is wrapped in try/except and failures are collected into
a run summary.
"""
from __future__ import annotations
import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests

HERE = Path(__file__).parent
FEEDS_FILE = HERE / "feeds.txt"
OUT_FILE = HERE / "raw_articles.json"

LOOKBACK_HOURS = 24
MAX_PER_FEED = 25          # cap entries pulled from any single feed
SNIPPET_CHARS = 600        # trim long bodies before they hit the LLM
REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; BriefingBot/1.0; +https://github.com/)"

# Common feed paths tried during auto-discovery, in order.
FEED_CANDIDATES = [
    "feed/", "feed", "rss/", "rss", "rss.xml", "feed.xml", "atom.xml",
    "index.xml", "feeds/posts/default", "?feed=rss2", "blog/feed/",
]
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def load_feed_urls() -> list[str]:
    urls = []
    for line in FEEDS_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def discover_feed(url: str) -> str | None:
    """Return a parseable feed URL for a page, or None."""
    # Special cases
    host = urlparse(url).netloc
    if "reddit.com" in host:
        return url.rstrip("/") + "/.rss"
    if "news.ycombinator.com" in host:
        return "https://news.ycombinator.com/rss"

    # 1. Maybe it's already a feed.
    parsed = feedparser.parse(url, agent=USER_AGENT)
    if parsed.entries:
        return url

    # 2. Look for <link rel="alternate" type="...rss/atom"> in the HTML head.
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        m = re.search(
            r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>',
            r.text, re.I,
        )
        if m:
            href = re.search(r'href=["\']([^"\']+)["\']', m.group(0), re.I)
            if href:
                return urljoin(url, href.group(1))
    except requests.RequestException:
        pass

    # 3. Brute-force common feed paths.
    base = url if url.endswith("/") else url + "/"
    for cand in FEED_CANDIDATES:
        test = urljoin(base, cand)
        try:
            p = feedparser.parse(test, agent=USER_AGENT)
            if p.entries:
                return test
        except Exception:
            continue
    return None


def _entry_time(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    return None


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")   # strip tags
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_feed(url: str, cutoff: dt.datetime) -> tuple[list[dict], str]:
    feed_url = discover_feed(url)
    if not feed_url:
        return [], f"NO FEED   {url}"

    parsed = feedparser.parse(feed_url, agent=USER_AGENT)
    source = urlparse(url).netloc.replace("www.", "")
    items = []
    for e in parsed.entries[:MAX_PER_FEED]:
        published = _entry_time(e)
        # Keep entries with no date (many blogs omit it) but drop clearly-old ones.
        if published and published < cutoff:
            continue
        link = e.get("link", "").strip()
        title = _clean(e.get("title", ""))
        if not link or not title:
            continue
        body = e.get("summary", "") or (e.get("content", [{}])[0].get("value", "")
                                        if e.get("content") else "")
        items.append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:12],
            "title": title,
            "url": link,
            "source": source,
            "snippet": _clean(body)[:SNIPPET_CHARS],
            "published": published.isoformat() if published else None,
        })
    return items, f"OK  {len(items):>3}  {source}  <-  {feed_url}"


def main() -> None:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)
    feeds = load_feed_urls()
    print(f"[ingest] {len(feeds)} sources, lookback {LOOKBACK_HOURS}h", flush=True)

    all_items: list[dict] = []
    log: list[str] = []
    # Feeds are I/O-bound -> thread pool. Keep it modest to be polite.
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(parse_feed, u, cutoff): u for u in feeds}
        for fut in cf.as_completed(futures):
            try:
                items, status = fut.result()
            except Exception as exc:                       # noqa: BLE001
                items, status = [], f"ERR       {futures[fut]}: {exc}"
            all_items.extend(items)
            log.append(status)

    # Dedupe by URL, then by normalized title.
    seen_url, seen_title, deduped = set(), set(), []
    for it in all_items:
        tkey = re.sub(r"\W+", "", it["title"].lower())[:80]
        if it["url"] in seen_url or tkey in seen_title:
            continue
        seen_url.add(it["url"]); seen_title.add(tkey)
        deduped.append(it)

    OUT_FILE.write_text(json.dumps(deduped, indent=2, ensure_ascii=False))
    print("\n".join(sorted(log)), flush=True)
    print(f"\n[ingest] {len(deduped)} unique articles -> {OUT_FILE.name}", flush=True)


if __name__ == "__main__":
    main()
