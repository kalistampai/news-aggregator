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
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests

HERE = Path(__file__).parent
FEEDS_FILE = HERE / "feeds.txt"
OUT_FILE = HERE / "raw_articles.json"
REPORT_JSON = HERE / "feed_report.json"
REPORT_MD = HERE / "feed_report.md"

# ---- tunables (all env-overridable) ----------------------------------------
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "25"))       # entries per feed
MAX_PER_SOURCE = int(os.environ.get("MAX_PER_SOURCE", "12"))   # after dedupe, per host
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
        key = line.rstrip("/").lower()
        if key in seen:                            # de-dupe the source list itself
            continue
        seen.add(key)
        urls.append(line)
    return urls


def _http_get(url: str):
    """GET a URL. Returns (response|None, status_code, detail)."""
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
    if DENY_TITLE.search(t):
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


def write_report(records: list[dict], kept_after_dedupe: int) -> None:
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
                   **{k: v for k, v in sorted(counts.items())}},
        "sources": records,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [f"# Feed health report — {dt.date.today().isoformat()}", "",
             f"- Sources checked: **{len(records)}**",
             f"- Producing articles: **{counts.get('OK', 0)}**",
             f"- Not contributing: **{len(failures)}**",
             f"- Unique articles after dedupe: **{kept_after_dedupe}**", ""]
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

    # Dedupe by URL, then normalized title; cap per source; global safety valve.
    seen_url, seen_title, per_source = set(), set(), Counter()
    deduped: list[dict] = []
    for rec in sorted(records, key=lambda r: r["source"]):
        for it in rec["items"]:
            if len(deduped) >= MAX_TOTAL:
                break
            tkey = re.sub(r"\W+", "", it["title"].lower())[:80]
            if it["url"] in seen_url or tkey in seen_title:
                continue
            if per_source[it["source"]] >= MAX_PER_SOURCE:
                continue
            seen_url.add(it["url"]); seen_title.add(tkey)
            per_source[it["source"]] += 1
            deduped.append(it)

    OUT_FILE.write_text(json.dumps(deduped, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    write_report(records, len(deduped))

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
