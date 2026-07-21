"""
Stage 3 — Deep Synthesis & Structuring (The Editor Agent)

Takes feature-tier articles, synthesizes 3-bullet cards grouped by category, and
passes notable-tier items through verbatim. Writes briefing.json — the exact blob
the dashboard renders and dispatch.py pushes to the Gist.

Resilience (why this stage no longer takes down the whole run):
  - Features are synthesized in small BATCHES (like the gatekeeper), so one failed or
    overloaded request no longer nukes the entire stage, and each request is small
    enough to schedule cheaply on the free tier.
  - The editor model fails over to EDITOR_FALLBACK_MODELS (default: the gatekeeper
    model, which is already known-reachable this run) when it is transiently
    unavailable — see llm.complete_json.
  - If a batch STILL can't be synthesized after retries + fallback, those features are
    emitted as minimal "degraded" cards (title/source/link, no bullets) so the
    briefing still ships. Set EDITOR_STRICT=1 to restore the original behaviour:
    abort the whole pipeline on any unrecoverable editor failure, leaving yesterday's
    briefing in the Gist rather than shipping a partial one.

URLs are stitched back in from the source records AFTER the model returns, so a
hallucinated link can never survive even if the model ignores the constraint.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import time
from pathlib import Path

from llm import EDITOR_MODEL, GATEKEEPER_MODEL, complete_json

HERE = Path(__file__).parent
IN_FILE = HERE / "scored_articles.json"
OUT_FILE = HERE / "briefing.json"
PROMPT = (HERE / "prompts" / "editor.txt").read_text()

MAX_FEATURES = 40   # hard ceiling on cards sent to the expensive model
EDITOR_BATCH_SIZE = int(os.environ.get("EDITOR_BATCH_SIZE", "10"))
EDITOR_STRICT = os.environ.get("EDITOR_STRICT", "").lower() in ("1", "true", "yes")
# Models to fall back to (in order) if EDITOR_MODEL is overloaded.
EDITOR_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("EDITOR_FALLBACK_MODELS", GATEKEEPER_MODEL).split(",")
    if m.strip()
]
DEGRADED_BUCKET = "Unsorted"   # category used for features we couldn't synthesize


def _batched(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _synthesize(batch: list[dict], today: str) -> dict:
    """Return {category: [cards]} for one batch of feature articles."""
    payload = json.dumps({
        "today": today,
        "features": [{"id": a["id"], "title": a["title"], "source": a["source"],
                      "url": a["url"], "snippet": a["snippet"],
                      "why": a["gatekeeper_reasoning"]} for a in batch],
    }, ensure_ascii=False)
    result = complete_json(PROMPT, payload, EDITOR_MODEL, max_tokens=8000,
                           fallback_models=EDITOR_FALLBACK_MODELS)
    cats = result.get("categories", {}) if isinstance(result, dict) else {}
    return cats if isinstance(cats, dict) else {}


def _degraded(batch: list[dict]) -> dict:
    """Minimal cards when synthesis is impossible — keep the item visible, no bullets."""
    cards = [{
        "title": a["title"],
        "source": a["source"],
        "url": a["url"],
        "score": a.get("score", 0),
        "reasoning": a.get("gatekeeper_reasoning", "")
                     or "Synthesis unavailable; source linked directly.",
        "bullets": [],
    } for a in batch]
    return {DEGRADED_BUCKET: cards}


def main() -> None:
    data = json.loads(IN_FILE.read_text())
    features = data["features"][:MAX_FEATURES]
    notable = data["notable"]
    today = dt.date.today().isoformat()

    # Authoritative URL/source/score map -> re-attached after synthesis (anti-hallucination).
    truth = {a["id"]: {"url": a["url"], "source": a["source"],
                       "title": a["title"], "score": a["score"]}
             for a in features}
    title_to_truth = {v["title"].strip().lower(): v for v in truth.values()}

    categories: dict[str, list] = {}
    degraded = 0
    batches = list(_batched(features, EDITOR_BATCH_SIZE))
    for n, batch in enumerate(batches):
        try:
            cats = _synthesize(batch, today)
        except Exception as exc:  # noqa: BLE001
            if EDITOR_STRICT:
                raise
            degraded += len(batch)
            print(f"[editor] batch {n + 1}/{len(batches)} failed "
                  f"({type(exc).__name__}); degrading {len(batch)} feature(s)",
                  flush=True)
            cats = _degraded(batch)

        for name, cards in cats.items():
            if isinstance(cards, list):
                categories.setdefault(name, []).extend(cards)

        if n < len(batches) - 1:
            time.sleep(2)   # be polite to free-tier rate limits between calls

    # Overwrite every card URL/source/score with the trusted original, matched by title.
    for cards in categories.values():
        for card in cards:
            match = title_to_truth.get(card.get("title", "").strip().lower())
            if match:
                card["url"] = match["url"]
                card["source"] = match["source"]
                card["score"] = match["score"]

    briefing = {
        "categories": categories,
        "date": today,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "also_notable": [
            {"title": a["title"], "source": a["source"], "url": a["url"]}
            for a in notable
        ],
    }

    OUT_FILE.write_text(json.dumps(briefing, indent=2, ensure_ascii=False))
    n_cards = sum(len(v) for v in categories.values())
    print(f"[editor] {n_cards} feature cards"
          + (f" ({degraded} degraded)" if degraded else "")
          + f", {len(briefing['also_notable'])} notable -> {OUT_FILE.name}",
          flush=True)


if __name__ == "__main__":
    main()
