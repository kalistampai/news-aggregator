"""Tests for the old.reddit HTML listing parser.

Reddit's .rss endpoint is capped at roughly one request per 50 seconds, so scam
subreddits are read from old.reddit HTML instead (see feeds.txt). This parser is
the one piece of the pipeline that depends on someone else's markup, so it is
tested against a fixture built from a real listing.

Offline: no network, no API keys.

    python test_reddit_html.py
"""
from __future__ import annotations
import datetime as dt
import sys
import traceback

import ingest
from ingest import is_reddit_html, parse_reddit_html

NOW = dt.datetime.now(dt.timezone.utc)
RECENT_MS = int((NOW - dt.timedelta(hours=2)).timestamp() * 1000)
OLD_MS = int((NOW - dt.timedelta(days=30)).timestamp() * 1000)
CUTOFF = NOW - dt.timedelta(hours=48)


def thing(*, fullname="t3_abc123", sub="Scams", permalink=None, title="A perfectly ordinary scam report title",
          ts=RECENT_MS, score="42", comments="7", promoted="false",
          nsfw="false", flair="Is this a scam?") -> str:
    """Build one div.thing the way old.reddit emits it."""
    permalink = permalink if permalink is not None else f"/r/{sub}/comments/abc123/some_slug/"
    flair_html = (f'<span class="flairrichtext linkflairlabel " title="{flair}">'
                  f"<span>{flair}</span></span>" if flair else "")
    return (
        f'<div class=" thing id-{fullname} odd link self" id="thing_{fullname}"'
        f' data-fullname="{fullname}" data-type="link" data-subreddit="{sub}"'
        f' data-subreddit-prefixed="r/{sub}" data-timestamp="{ts}"'
        f' data-url="{permalink}" data-permalink="{permalink}"'
        f' data-comments-count="{comments}" data-score="{score}"'
        f' data-promoted="{promoted}" data-nsfw="{nsfw}">'
        f'<div class="entry unvoted"><div class="top-matter"><p class="title">'
        f'<a class="title may-blank " href="{permalink}">{title}</a>{flair_html}'
        f'</p></div></div></div>'
    )


def listing(*things: str) -> str:
    return ('<div id="siteTable" class="sitetable linklisting">'
            + "".join(things) + "</div>")


# --------------------------------------------------------------------------- #
def test_url_routing():
    """HTML listings route to the scraper; .rss keeps the feedparser path."""
    assert is_reddit_html("https://old.reddit.com/r/Scams/new/?limit=100")
    assert is_reddit_html("https://www.reddit.com/r/Scams/")
    assert not is_reddit_html("https://www.reddit.com/r/Scams/.rss")
    assert not is_reddit_html("https://www.reddit.com/r/a+b/.rss?limit=100")
    assert not is_reddit_html("https://example.com/r/Scams/new/")


def test_extracts_the_fields_the_pipeline_needs():
    items, seen, _ = parse_reddit_html(listing(thing()), CUTOFF, 100)
    assert seen == 1, seen
    assert len(items) == 1, items
    it = items[0]
    assert it["title"] == "A perfectly ordinary scam report title"
    assert it["url"] == "https://www.reddit.com/r/Scams/comments/abc123/some_slug/"
    assert it["source"] == "reddit.com", it["source"]
    assert len(it["id"]) == 12
    assert it["published"] is not None


def test_snippet_carries_flair_and_engagement():
    """Listings have no post body, so the snippet must be synthesised or the
    gatekeeper scores every Reddit item on a bare title."""
    items, _, _ = parse_reddit_html(listing(thing(score="118", comments="44")),
                                    CUTOFF, 100)
    snip = items[0]["snippet"]
    for expected in ("r/Scams", "Is this a scam?", "118 points", "44 comments"):
        assert expected in snip, (expected, snip)


def test_promoted_posts_are_dropped():
    html = listing(thing(fullname="t3_ad", promoted="true"), thing())
    items, seen, drops = parse_reddit_html(html, CUTOFF, 100)
    assert seen == 2
    assert len(items) == 1, items
    assert drops["promoted"] == 1


def test_old_posts_are_dropped_by_cutoff():
    html = listing(thing(fullname="t3_old", ts=OLD_MS), thing())
    items, _, drops = parse_reddit_html(html, CUTOFF, 100)
    assert len(items) == 1, items
    assert drops["too_old"] == 1


def test_score_is_read_from_the_attribute_not_the_visible_bullet():
    """A post in its first hour renders its score as a bullet; data-score is
    still correct, which is why the parser reads attributes."""
    html = listing(thing(score="0")).replace(
        '<div class="entry', '<div class="score unvoted">&bull;</div><div class="entry')
    items, _, _ = parse_reddit_html(html, CUTOFF, 100)
    assert "0 points" in items[0]["snippet"]


def test_max_entries_is_respected():
    html = listing(*[thing(fullname=f"t3_{i}", permalink=f"/r/Scams/comments/{i}/s/")
                     for i in range(50)])
    items, _, _ = parse_reddit_html(html, CUTOFF, 10)
    assert len(items) == 10, len(items)


def test_ids_are_unique_per_post():
    html = listing(*[thing(fullname=f"t3_{i}", permalink=f"/r/Scams/comments/{i}/s/")
                     for i in range(20)])
    items, _, _ = parse_reddit_html(html, CUTOFF, 100)
    assert len({i["id"] for i in items}) == 20


def test_scam_titles_survive_the_commercial_denylist():
    """Reddit scam titles routinely contain denylist words. The SCAM_SIGNAL
    exemption must keep them; losing these would gut the lead section."""
    html = listing(
        thing(fullname="t3_a", permalink="/r/Scams/comments/a/s/",
              title="Black Friday refund scam text going around"),
        thing(fullname="t3_b", permalink="/r/Scams/comments/b/s/",
              title="Facebook giveaway scam targeting small businesses"))
    items, _, drops = parse_reddit_html(html, CUTOFF, 100)
    assert len(items) == 2, (items, drops)


def test_junk_and_malformed_things_do_not_crash():
    html = listing(
        '<div class=" thing"></div>',                          # matched, no data-*
        thing(title="short"),                                  # below MIN_TITLE_CHARS
        thing(fullname="t3_x", permalink="/user/someone/"),     # not a /r/ permalink
        thing(fullname="t3_ok", permalink="/r/Scams/comments/ok/s/"))
    items, seen, _ = parse_reddit_html(html, CUTOFF, 100)
    assert seen == 4, seen
    assert len(items) == 1, items


def test_empty_and_garbage_html_are_survivable():
    for junk in ("", "<html><body>nothing here</body></html>", "not html at all"):
        items, seen, _ = parse_reddit_html(junk, CUTOFF, 100)
        assert items == [] and seen == 0


def test_real_listing_shape():
    """The fixture mirrors real markup, but assert the contract we rely on:
    100 posts per ?limit=100 page, all with the data-* attributes we read."""
    html = listing(*[thing(fullname=f"t3_{i}", permalink=f"/r/Scams/comments/{i}/s/",
                           title=f"Scam report number {i} with a real title")
                     for i in range(100)])
    items, seen, _ = parse_reddit_html(html, CUTOFF, 100)
    assert seen == 100 and len(items) == 100


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception:                                    # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
