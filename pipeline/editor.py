"""
Stage 3 — Deep Synthesis & Structuring (The Editor Agent)

Takes feature-tier articles, synthesizes 3-bullet cards grouped by category, and
passes notable-tier items through verbatim. Writes briefing.json — the exact blob
the dashboard renders and dispatch.py pushes to the Gist.

CEILING: MAX_FEATURES is the only hard cap on how many articles get summarized.
Anything above it is NO LONGER DISCARDED — the overflow now spills into
"also_notable" as headline+link, so a busy day degrades gracefully instead of
silently deleting scored articles. Notable-tier has no cap.

Resilience:
  - Features are synthesized in small BATCHES, so one failed request no longer
    nukes the stage, and each request stays well inside max_tokens.
  - The editor model fails over to EDITOR_FALLBACK_MODELS when transiently
    unavailable (see llm.complete_json).
  - If a batch still can't be synthesized, those features are emitted as minimal
    "degraded" cards so the briefing still ships. EDITOR_STRICT=1 restores
    abort-on-failure.
  - Pacing is central (llm.LLM_MIN_INTERVAL); no local sleeps.

URLs are stitched back in from the source records AFTER the model returns, so a
hallucinated link can never survive even if the model ignores the constraint.
"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path

from llm import EDITOR_MODEL, GATEKEEPER_MODEL, complete_json

HERE = Path(__file__).parent
IN_FILE = HERE / "scored_articles.json"
OUT_FILE = HERE / "briefing.json"
PROMPT = (HERE / "prompts" / "editor.txt").read_text(encoding="utf-8")

# Hard ceiling on synthesized cards. 194 feeds => roughly 70-90 feature-tier
# articles/day, so 120 clears a normal day with headroom. Each +10 features is
# +1 Gemini request at EDITOR_BATCH_SIZE=10.
MAX_FEATURES = int(os.environ.get("MAX_FEATURES", "120"))
EDITOR_BATCH_SIZE = int(os.environ.get("EDITOR_BATCH_SIZE", "10"))
EDITOR_STRICT = os.environ.get("EDITOR_STRICT", "").lower() in ("1", "true", "yes")
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
    """Minimal cards when synthesis is impossible — keep the item, drop bullets."""
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
    data = json.loads(IN_FILE.read_text(encoding="utf-8"))
    all_features = data["features"]
    features = all_features[:MAX_FEATURES]
    overflow = all_features[MAX_FEATURES:]      # spilled, not deleted
    notable = data["notable"]
    today = dt.date.today().isoformat()

    # Authoritative URL/source/score map -> re-attached after synthesis.
    truth = {a["id"]: {"url": a["url"], "source": a["source"],
                       "title": a["title"], "score": a["score"]}
             for a in features}
    title_to_truth = {v["title"].strip().lower(): v for v in truth.values()}

    categories: dict[str, list] = {}
    degraded = 0
    batches = list(_batched(features, EDITOR_BATCH_SIZE))
    print(f"[editor] {len(all_features)} feature-tier | synthesizing "
          f"{len(features)} in {len(batches)} batch(es)"
          + (f" | {len(overflow)} spilled to also_notable" if overflow else ""),
          flush=True)

    for n, batch in enumerate(batches, 1):
        try:
            cats = _synthesize(batch, today)
        except Exception as exc:  # noqa: BLE001
            if EDITOR_STRICT:
                raise
            degraded += len(batch)
            print(f"[editor] batch {n}/{len(batches)} failed "
                  f"({type(exc).__name__}); degrading {len(batch)} feature(s)",
                  flush=True)
            cats = _degraded(batch)

        for name, cards in cats.items():
            if isinstance(cards, list):
                categories.setdefault(name, []).extend(cards)

    # Overwrite every card URL/source/score with the trusted original.
    for cards in categories.values():
        for card in cards:
            match = title_to_truth.get(card.get("title", "").strip().lower())
            if match:
                card["url"] = match["url"]
                card["source"] = match["source"]
                card["score"] = match["score"]

    # also_notable = notable tier + any feature-tier overflow above the ceiling.
    also_notable = [
        {"title": a["title"], "source": a["source"], "url": a["url"]}
        for a in (overflow + notable)
    ]

    briefing = {
        "categories": categories,
        "date": today,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "also_notable": also_notable,
    }

    OUT_FILE.write_text(json.dumps(briefing, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    n_cards = sum(len(v) for v in categories.values())
    print(f"[editor] {n_cards} feature cards"
          + (f" ({degraded} degraded)" if degraded else "")
          + f", {len(also_notable)} notable"
          + (f" (incl. {len(overflow)} overflow)" if overflow else "")
          + f" -> {OUT_FILE.name}", flush=True)


if __name__ == "__main__":
    main()
