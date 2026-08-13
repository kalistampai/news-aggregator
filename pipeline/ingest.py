"""
Stage 1 — Automated Ingestion (The Fetch)

Reads feeds.txt, resolves an RSS/Atom feed for each URL, parses recent entries,
pre-filters, dedupes, and writes raw_articles.json.

Also writes a FEED HEALTH REPORT (feed_report.json + feed_report.md) classifying
every source as OK or a specific failure reason — 404, 403/WAF, CAPTCHA wall,
paywall, timeout, DNS, malformed XML, empty feed, stale feed, or fully filtered.
Nothing is silently skipped: every URL in feeds.txt appears in the report.

No LLM calls here. Pure network + parsing. One dead source never fails the run.
"""
from __future__ import annotations
import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import json
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests

import seen as seen_store

HERE = Path(__file__).parent
FEEDS_FILE = HERE / "feeds.txt"
OUT_FILE = HERE / "raw_articles.json"
REPORT_JSON = HERE / "feed_report.json"
REPORT_MD = HERE / "feed_report.md"

# ---- tunables (all env-overridable) ----------------------------------------
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "25"))       # entries per feed
MAX_PER_SOURCE = int(os.environ.get("MAX_PER_SOURCE", "12"))   # after dedupe, per host
# PRIORITY SECTIONS get a bigger budget, counted PER FEED instead of per host.
# Two reasons the per-host cap throttled the lead section badly:
#   1. reddit.com is one host for 14 feeds, so all 11 scam subreddits were
#      fighting over a single 12-article budget.
#   2. Seven scam sources hit exactly 12 on 2026-08-13 and were truncated.
# Per-feed counting means one scam subreddit can no longer starve the others.
MAX_PER_PRIORITY_SOURCE = int(os.environ.get("MAX_PER_PRIORITY_SOURCE", "30"))
PRIORITY_SECTIONS = {"scam / fraud alerts"}
MAX_TOTAL = int(os.environ.get("MAX_TOTAL", "1500"))           # global safety valve
SNIPPET_CHARS = int(os.environ.get("SNIPPET_CHARS", "600"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "10"))
MAX_WORKERS = int(os.environ.get("INGEST_WORKERS", "16"))      # 194 feeds -> raise this
# "keep" (default) retains entries with no publish date; "drop" enforces a strict
# <LOOKBACK_HOURS window. Many good blogs omit dates entirely — "drop" silently
# costs you those sources. See the README note before switching.
DATELESS_POLICY = os.environ.get("DATELESS_POLICY", "keep").lower()

USER_AGENT = ("Mozilla/5.0 (compatible; BriefingBot/1.0; "
              "+https://github.com/) FeedFetcher")

# ---- per-host throttle ------------------------------------------------------
# INGEST_WORKERS threads ignore whose server they are hitting. That is fine when
# a host owns one feed, but feeds.txt now carries 14 reddit.com feeds, and
# firing them at once made Reddit 429 EVERY ONE of them — the whole scam-report
# tier silently contributed zero. Spacing requests per HOST fixes it without
# slowing the other ~195 sources, since only the crowded host ever waits.
HOST_MIN_INTERVAL = float(os.environ.get("HOST_MIN_INTERVAL", "1.0"))
# Reddit's unauthenticated ceiling is ~10 requests/min/IP. 14 feeds at 6s is
# exactly 10/min — right ON the limit, and it still 429'd. 10s is 6/min, a real
# margin, and costs ~140s of a run that has a 60-minute budget.
# NOTE: a 429 here is a BURST problem and this fixes it. A 403 is different —
# that is Reddit refusing the IP class (datacenter), which no spacing can fix;
# see the Reddit note in feeds.txt.
HOST_INTERVAL_OVERRIDES = {
    "reddit.com": float(os.environ.get("REDDIT_MIN_INTERVAL", "10")),
}
_host_last: dict[str, float] = {}
_host_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _host_interval(host: str) -> float:
    for domain, gap in HOST_INTERVAL_OVERRIDES.items():
        if host == domain or host.endswith("." + domain):
            return gap
    return HOST_MIN_INTERVAL


def _throttle(host: str) -> None:
    """Block until this host's minimum gap has elapsed. Serial per host only."""
    with _locks_guard:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:                       # held across the sleep: serializes the host
        wait = _host_interval(host) - (time.monotonic() - _host_last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.monotonic()

FEED_CANDIDATES = [
    "feed/", "feed", "rss/", "rss", "rss.xml", "feed.xml", "atom.xml",
    "index.xml", "feeds/posts/default", "?feed=rss2", "blog/feed/",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": ("application/rss+xml, application/atom+xml, application/xml, "
               "text/xml, text/html;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
})

# ---- pre-filter -------------------------------------------------------------
# NARROW commercial/spam denylist only. Deliberately does NOT filter "roundup",
# "top 10", "best of" etc: prompts/gatekeeper.txt explicitly rules that
# authoritative patch/CVE roundups are FEATURE-tier, so a format-based denylist
# here would contradict the scoring prompt and delete real signal.
DENY_TITLE = re.compile(
    r"\b(coupon|promo code|discount code|deal of the day|daily deals|"
    r"giveaway|sweepstakes|horoscope|black friday|cyber monday|"
    r"sponsored (post|content)|advertorial|"
    r"prime day|gift guide)\b", re.I,
)
MIN_TITLE_CHARS = 12
JUNK_TITLES = {"comments", "untitled", "no title", "(no title)", "rss", "feed"}

# SCAM EXEMPTION — load-bearing for the Scams & Fraud section.
# "Black Friday refund scam", "Facebook giveaway scam" and "sweepstakes fraud"
# are precisely the headlines that section exists to surface, and every one of
# them trips a DENY_TITLE term. That denylist is aimed at RETAIL PROMOS, so a
# title plainly ABOUT a scam is exempted instead of dropped as commercial spam.
# This only widens what reaches the gatekeeper — a real promo that sneaks
# through still scores 0-2 there, whereas a drop here is silent and permanent.
SCAM_SIGNAL = re.compile(
    r"\b(scam|scams|scammer|scammers|scamming|fraud|frauds|fraudster|"
    r"fraudulent|phish|phishing|smishing|vishing|imposter|impostor|"
    r"impersonat\w*|counterfeit|swindle|con artist|rip-?off|bogus|"
    r"identity theft|romance scam|pig butchering|money mule|fake)\b", re.I,
)

# Keyword ALLOWLIST is off by default. A hard allowlist upstream of the model
# defeats the gatekeeper's whole purpose (semantic relevance) and will drop
# novel/oddly-titled items the model would have scored 9. Enable only if you are
# genuinely quota-constrained; see README.
ALLOWLIST_MODE = os.environ.get("ALLOWLIST_MODE", "").lower() in ("1", "true", "yes")
ALLOW_TERMS = [t.strip().lower() for t in os.environ.get(
    "ALLOW_TERMS",
    "cve,exploit,vulnerability,malware,ransomware,breach,0day,zero-day,patch,"
    "advisory,reverse engineer,firmware,sdr,osint,recon,kernel,linux,homelab,"
    "self-host,llm,model,prompt,agent,ml,ai,red team,c2,payload,threat,privacy,"
    "surveillance,leak,forensic,router,iot,embedded,glitch,side-channel"
).split(",") if t.strip()]

# ---- failure taxonomy -------------------------------------------------------
CAPTCHA_MARKERS = (
    "just a moment", "attention required", "cf-browser-verification",
    "checking your browser", "captcha", "ddos protection",
    "enable javascript and cookies", "cf-chl", "px-captcha",
)
PAYWALL_MARKERS = (
    "subscribe to continue", "subscribers only", "this content is for members",
    "become a member to", "sign in to read", "paywall", "metered access",
)

STATUS_HELP = {
    "OK": "Feed parsed and contributed articles.",
    "STALE": f"Feed works but every entry is older than {LOOKBACK_HOURS}h.",
    "FILTERED": "Fresh entries existed but all were dropped by the pre-filter.",
    "EMPTY": "Feed parsed successfully but contains zero entries.",
    "DUPLICATE": "Resolved to a feed another entry already provides — remove this line.",
    "NO_FEED": "No RSS/Atom feed could be discovered at this URL.",
    "HTTP_404": "Dead URL (404/410). Feed moved or removed.",
    "HTTP_403": "Blocked (403). WAF/Cloudflare/bot protection or UA ban.",
    "CAPTCHA": "Served a CAPTCHA/JS interstitial instead of content.",
    "PAYWALL": "Paywalled or requires authentication (401/402).",
    "HTTP_429": "Rate limited by the source.",
    "HTTP_5XX": "Source server error.",
    "HTTP_OTHER": "Unexpected HTTP status.",
    "TIMEOUT": f"No response within {REQUEST_TIMEOUT}s.",
    "DNS_ERROR": "Hostname did not resolve.",
    "SSL_ERROR": "TLS/certificate failure.",
    "CONN_ERROR": "Connection failed or was reset.",
    "PARSE_ERROR": "Response was not parseable XML/RSS.",
}
FAILING = {k for k in STATUS_HELP if k != "OK"}


def dedupe_key(url: str) -> str:
    """Normalize a feed URL for duplicate detection.

    Catches the cheap duplicates BEFORE any network cost: scheme differences,
    a leading "www.", and a trailing slash. Path variants that resolve to one
    document (/feed/ -> /rss.xml) and acquisition redirects (egress.com ->
    blog.knowbe4.com) are INVISIBLE from the URL alone — those are collapsed
    after discovery, in main().
    """
    u = re.sub(r"^https?://", "", url.strip().lower())
    return re.sub(r"^www\.", "", u).rstrip("/")


# Top-level sections are "# --- Name ---" (3+ dashes). The "# -- Name --"
# sub-headings nested inside them are deliberately NOT boundaries, so a feed
# under "-- Consumer scam trackers --" still counts as "Scam / Fraud Alerts".
_SECTION_RE = re.compile(r"^#\s*-{3,}\s*(.+?)\s*-{3,}\s*$")


def load_feed_sections() -> dict[str, str]:
    """Map each active feed URL to the '# --- Section ---' heading above it.

    Used only to decide which feeds get the larger priority budget, so the
    section names live in feeds.txt (one source of truth) instead of being
    duplicated as a hardcoded host list here that would silently drift.
    """
    sections, current = {}, ""
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1).strip().lower()
            continue
        if not line or line.startswith("#"):
            continue
        url = line.split("#", 1)[0].strip().split(" ")[0]
        if url.lower().startswith(("http://", "https://")):
            sections[url] = current
    return sections


def load_feed_urls() -> list[str]:
    """One URL per line. Ignores blanks, # comments, and inline # annotations."""
    urls, seen = [], set()
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split("#", 1)[0].strip()      # tolerate "url  # note"
        parts = line.split()
        line = parts[0] if parts else ""
        if not line.lower().startswith(("http://", "https://")):
            continue
        key = dedupe_key(line)
        if key in seen:                            # de-dupe the source list itself
            continue
        seen.add(key)
        urls.append(line)
    return urls


def _http_get(url: str):
    """GET a URL. Returns (response|None, status_code, detail)."""
    _throttle(urlparse(url).netloc.lower().replace("www.", ""))
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.exceptions.Timeout:
        return None, "TIMEOUT", ""
    except requests.exceptions.SSLError as e:
        return None, "SSL_ERROR", str(e)[:160]
    except requests.exceptions.ConnectionError as e:
        msg = str(e).lower()
        code = "DNS_ERROR" if ("name or service" in msg or "nodename" in msg
                               or "getaddrinfo" in msg) else "CONN_ERROR"
        return None, code, str(e)[:160]
    except requests.RequestException as e:
        return None, "CONN_ERROR", str(e)[:160]

    sc = r.status_code
    if sc in (401, 402):
        return None, "PAYWALL", f"HTTP {sc}"
    if sc == 403:
        body = (r.text or "")[:4000].lower()
        if any(m in body for m in CAPTCHA_MARKERS):
            return None, "CAPTCHA", "HTTP 403 + interstitial"
        return None, "HTTP_403", "HTTP 403"
    if sc in (404, 410):
        return None, "HTTP_404", f"HTTP {sc}"
    if sc == 429:
        return None, "HTTP_429", "HTTP 429"
    if 500 <= sc < 600:
        return None, "HTTP_5XX", f"HTTP {sc}"
    if sc >= 400:
        return None, "HTTP_OTHER", f"HTTP {sc}"

    body = (r.text or "")[:4000].lower()
    if any(m in body for m in CAPTCHA_MARKERS):
        return None, "CAPTCHA", "interstitial in 200 response"
    if (any(m in body for m in PAYWALL_MARKERS)
            and "<item" not in body and "<entry" not in body):
        return None, "PAYWALL", "paywall markers in 200 response"
    return r, "OK", ""


def _parse_bytes(content: bytes):
    """feedparser on raw bytes. Returns (parsed, has_entries)."""
    parsed = feedparser.parse(content)
    return parsed, bool(getattr(parsed, "entries", None))


def discover_feed(url: str):
    """Resolve a parseable feed. Returns (feed_url, parsed, status, detail)."""
    host = urlparse(url).netloc
    if "reddit.com" in host:
        u = url.rstrip("/")
        url = u if u.endswith("/.rss") else u + "/.rss"
    elif "news.ycombinator.com" in host:
        url = "https://news.ycombinator.com/rss"

    resp, status, detail = _http_get(url)
    if resp is None:
        return None, None, status, detail

    parsed, ok = _parse_bytes(resp.content)
    if ok:
        return url, parsed, "OK", ""

    # Looks like HTML: try <link rel="alternate" type="application/rss+xml">.
    text = resp.text or ""
    m = re.search(
        r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', text, re.I)
    if m:
        href = re.search(r'href=["\']([^"\']+)["\']', m.group(0), re.I)
        if href:
            cand = urljoin(url, href.group(1))
            r2, _s2, _d2 = _http_get(cand)
            if r2 is not None:
                p2, ok2 = _parse_bytes(r2.content)
                if ok2:
                    return cand, p2, "OK", ""

    # Brute-force common paths.
    base = url if url.endswith("/") else url + "/"
    for c in FEED_CANDIDATES:
        cand = urljoin(base, c)
        r3, _s3, _d3 = _http_get(cand)
        if r3 is None:
            continue
        p3, ok3 = _parse_bytes(r3.content)
        if ok3:
            return cand, p3, "OK", ""

    if "<html" in text[:2000].lower():
        return None, None, "NO_FEED", "HTML page, no discoverable feed"
    return None, None, "PARSE_ERROR", "response was not parseable RSS/Atom"


def _entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _clean(text: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _prefilter(title: str, snippet: str) -> str | None:
    """Return a drop-reason, or None to keep."""
    t = title.strip()
    if len(t) < MIN_TITLE_CHARS or t.lower() in JUNK_TITLES:
        return "junk_title"
    if DENY_TITLE.search(t) and not SCAM_SIGNAL.search(f"{t} {snippet}"):
        return "commercial_spam"
    if ALLOWLIST_MODE:
        blob = f"{t} {snippet}".lower()
        if not any(term in blob for term in ALLOW_TERMS):
            return "allowlist_miss"
    return None


def parse_feed(url: str, cutoff: dt.datetime) -> dict:
    """Fetch one source. Returns a report record including its items."""
    rec = {"url": url, "source": urlparse(url).netloc.replace("www.", ""),
           "feed_url": None, "status": None, "detail": "",
           "entries_seen": 0, "kept": 0, "dropped": {}, "items": []}

    feed_url, parsed, status, detail = discover_feed(url)
    rec["feed_url"], rec["detail"] = feed_url, detail
    if parsed is None:
        rec["status"] = status
        return rec

    all_entries = list(parsed.entries)
    rec["entries_seen"] = len(all_entries)
    entries = all_entries[:MAX_PER_FEED]
    if not entries:
        rec["status"] = "EMPTY"
        return rec

    drops = Counter()
    fresh_seen = 0
    for e in entries:
        published = _entry_time(e)
        if published is None:
            if DATELESS_POLICY == "drop":
                drops["no_date"] += 1
                continue
        elif published < cutoff:
            drops["too_old"] += 1
            continue
        fresh_seen += 1

        link = (e.get("link") or "").strip()
        title = _clean(e.get("title", ""))
        if not link or not title:
            drops["missing_fields"] += 1
            continue

        body = e.get("summary", "") or (
            e.get("content", [{}])[0].get("value", "") if e.get("content") else "")
        snippet = _clean(body)[:SNIPPET_CHARS]

        reason = _prefilter(title, snippet)
        if reason:
            drops[reason] += 1
            continue

        rec["items"].append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:12],
            "title": title,
            "url": link,
            "source": rec["source"],
            "snippet": snippet,
            "published": published.isoformat() if published else None,
        })

    rec["dropped"] = dict(drops)
    rec["kept"] = len(rec["items"])
    if rec["kept"]:
        rec["status"] = "OK"
    elif fresh_seen == 0:
        rec["status"] = "STALE"
    else:
        rec["status"] = "FILTERED"
    return rec


def write_report(records: list[dict], kept_after_dedupe: int,
                 cross_run_suppressed: int = 0) -> None:
    counts = Counter(r["status"] for r in records)
    failures = [r for r in records if r["status"] in FAILING]
    failures.sort(key=lambda r: (r["status"], r["source"]))

    REPORT_JSON.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "lookback_hours": LOOKBACK_HOURS,
        "dateless_policy": DATELESS_POLICY,
        "allowlist_mode": ALLOWLIST_MODE,
        "totals": {"sources": len(records),
                   "articles_kept": kept_after_dedupe,
                   "cross_run_suppressed": cross_run_suppressed,
                   **{k: v for k, v in sorted(counts.items())}},
        "sources": records,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [f"# Feed health report — {dt.date.today().isoformat()}", "",
             f"- Sources checked: **{len(records)}**",
             f"- Producing articles: **{counts.get('OK', 0)}**",
             f"- Not contributing: **{len(failures)}**",
             f"- Unique articles after dedupe: **{kept_after_dedupe}**",
             (f"- Suppressed as already published: **{cross_run_suppressed}**"
              if cross_run_suppressed else ""), ""]
    if failures:
        lines += ["## Sources contributing nothing", "",
                  "| Source | Status | What it means | Detail |",
                  "|---|---|---|---|"]
        for r in failures:
            lines.append(f"| {r['source']} | `{r['status']}` | "
                         f"{STATUS_HELP.get(r['status'], '')} | {r['detail'] or '—'} |")
        lines += ["", "<details><summary>Full URLs</summary>", ""]
        for r in failures:
            lines.append(f"- `{r['status']}` {r['url']}")
        lines += ["", "</details>", ""]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)
    feeds = load_feed_urls()
    print(f"[ingest] {len(feeds)} sources | lookback {LOOKBACK_HOURS}h | "
          f"workers {MAX_WORKERS} | dateless={DATELESS_POLICY} | "
          f"allowlist={'on' if ALLOWLIST_MODE else 'off'}", flush=True)

    records: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(parse_feed, u, cutoff): u for u in feeds}
        for fut in cf.as_completed(futures):
            u = futures[fut]
            try:
                records.append(fut.result())
            except Exception as exc:                       # noqa: BLE001
                records.append({"url": u,
                                "source": urlparse(u).netloc.replace("www.", ""),
                                "feed_url": None, "status": "CONN_ERROR",
                                "detail": f"{type(exc).__name__}: {exc}"[:160],
                                "entries_seen": 0, "kept": 0,
                                "dropped": {}, "items": []})

    # ---- resolved-feed dedupe ---------------------------------------------
    # Two different lines in feeds.txt can serve the SAME feed, and neither is
    # visible to the URL key in load_feed_urls(): a path variant (knowbe4
    # /feed/ -> /rss.xml) or an acquisition redirect (egress.com/blog/rss ->
    # blog.knowbe4.com/rss.xml, a different host entirely). Collapse them on the
    # feed actually resolved, and report the loser as DUPLICATE so the health
    # panel names the line to delete instead of hiding it.
    # Walked in feeds.txt order so the winner is deterministic — the first
    # listing of a feed keeps it, however the threads happened to finish.
    # A second key catches mirrors that resolve to DIFFERENT urls but serve the
    # same articles (gacs.app /rss.xml vs /api/public/feed.xml). Only applied to
    # feeds that actually returned items — otherwise every STALE feed, which has
    # an empty item set, would collapse into a single entry.
    rank = {u: i for i, u in enumerate(feeds)}
    by_feed: dict[str, dict] = {}
    by_content: dict[frozenset, dict] = {}
    for rec in sorted(records, key=lambda r: rank.get(r["url"], len(rank))):
        if not rec.get("feed_url") or rec["status"] in FAILING:
            continue
        keys = [("resolves to", dedupe_key(rec["feed_url"]), by_feed)]
        if rec["items"]:
            keys.append(("serves the same articles as",
                         frozenset(i["id"] for i in rec["items"]), by_content))
        for phrasing, key, table in keys:
            winner = table.get(key)
            if winner is None:
                table[key] = rec
                continue
            rec.update(status="DUPLICATE", items=[], kept=0,
                       detail=f"same feed as {winner['url']}")
            print(f"[ingest] DUPLICATE: {rec['url']} {phrasing} "
                  f"{winner['url']} — drop one of the two lines", flush=True)
            break

    # Dedupe by URL, then normalized title; cap per source; global safety valve.
    # Priority-section feeds are counted per FEED and against the larger budget;
    # everything else keeps the original per-host cap.
    sections = load_feed_sections()
    seen_url, seen_title, per_source = set(), set(), Counter()
    deduped: list[dict] = []
    for rec in sorted(records, key=lambda r: r["source"]):
        priority = sections.get(rec["url"], "") in PRIORITY_SECTIONS
        cap = MAX_PER_PRIORITY_SOURCE if priority else MAX_PER_SOURCE
        cap_key = rec["url"] if priority else rec["source"]
        for it in rec["items"]:
            if len(deduped) >= MAX_TOTAL:
                break
            tkey = re.sub(r"\W+", "", it["title"].lower())[:80]
            if it["url"] in seen_url or tkey in seen_title:
                continue
            if per_source[cap_key] >= cap:
                continue
            seen_url.add(it["url"]); seen_title.add(tkey)
            per_source[cap_key] += 1
            deduped.append(it)

    # ---- cross-run dedupe -------------------------------------------------
    # LOOKBACK_HOURS (48) is twice the run interval (24h), so every article
    # falls inside two consecutive windows. The per-source/per-title dedupe
    # above only sees ONE run, so without this every story would be scored and
    # published twice. Best-effort: if the Gist is unreachable, `previously` is
    # empty and the run proceeds with duplicates rather than failing.
    seen_state = seen_store.fetch_seen()
    previously = seen_store.seen_ids(seen_state)
    if previously:
        before = len(deduped)
        deduped = [it for it in deduped if it["id"] not in previously]
        suppressed = before - len(deduped)
        print(f"[ingest] cross-run dedupe: {suppressed} already-published "
              f"article(s) suppressed ({len(previously)} ids known)", flush=True)
    else:
        suppressed = 0

    OUT_FILE.write_text(json.dumps(deduped, indent=2, ensure_ascii=False),
                        encoding="utf-8")

    # Stage the merged next state for dispatch.py. Only ids that survived to
    # raw_articles.json are recorded — those are the ones that cost LLM budget.
    seen_store.write_pending(seen_state, [it["id"] for it in deduped],
                             dt.date.today().isoformat())

    write_report(records, len(deduped), suppressed)

    counts = Counter(r["status"] for r in records)
    ok = counts.get("OK", 0)
    print(f"\n[ingest] {len(deduped)} unique articles -> {OUT_FILE.name}", flush=True)
    print(f"[ingest] sources OK={ok}  not-contributing={len(records) - ok}", flush=True)
    for status, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if status != "OK":
            print(f"           {status:<12} {n:>3}  {STATUS_HELP.get(status, '')}",
                  flush=True)
    print(f"[ingest] health report -> {REPORT_MD.name} / {REPORT_JSON.name}",
          flush=True)


if __name__ == "__main__":
    main()
