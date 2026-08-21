"""
Stage 3 — Deep Synthesis & Structuring (The Editor Agent)

Takes feature-tier articles, synthesizes 3-bullet cards grouped by category, and
passes notable-tier items through verbatim. Writes briefing.json — the exact blob
the dashboard renders and dispatch.py stores in Supabase.

CEILING: MAX_FEATURES is the only hard cap on how many articles get summarized.
Anything above it is NO LONGER DISCARDED — the overflow spills into "also_notable"
as headline+link+score, so a busy day degrades gracefully instead of silently
deleting scored articles. Notable-tier has no cap.

also_notable entries carry {title, source, url, score, tier}. `tier` is
"overflow" for feature-tier articles pushed past MAX_FEATURES (score >= 7) and
"notable" for genuine notable-tier items (score 5-6). The list is sorted by score
descending. Downstream (the dashboard) relies on `score` being present to apply
its relevance threshold; briefings archived before this change lack the field and
are treated as unscored.

Resilience:
  - Features are synthesized in small BATCHES, so one failed request no longer
    nukes the stage, and each request stays well inside max_tokens.
  - The editor model fails over to EDITOR_FALLBACK_MODELS when transiently
    unavailable (see llm.complete_json).
  - If a batch still can't be synthesized, those features are emitted as minimal
    "degraded" cards so the briefing still ships. EDITOR_STRICT=1 restores
    abort-on-failure. Exception: an all-models-busy outage or a fatal error
    (bad key / malformed request / unknown model / 429) aborts instead of
    degrading — those repeat identically on every batch, and degrading them
    would ship a gutted briefing over yesterday's good one.
  - Pacing is central (llm.LLM_MIN_INTERVAL); no local sleeps.

briefing.json carries a `models` block naming the model that ACTUALLY answered
for each stage (plus the configured one and the failover log). The dashboard
labels the day with it, so a run that quietly failed over is visible.

URLs are stitched back in from the source records AFTER the model returns, so a
hallucinated link can never survive even if the model ignores the constraint.
"""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path

import llm
from llm import (EDITOR_MODEL, GATEKEEPER_MODEL, EDITOR_FALLBACK_MODELS,
                 complete_json, FatalLlmError, ModelsBusyError)

HERE = Path(__file__).parent
IN_FILE = HERE / "scored_articles.json"
OUT_FILE = HERE / "briefing.json"
PROMPT = (HERE / "prompts" / "editor.txt").read_text(encoding="utf-8")

# Hard ceiling on synthesized cards. 181 feeds => roughly 70-90 feature-tier
# articles/day, so 120 clears a normal day with headroom. Each +10 features is
# +1 Gemini request at EDITOR_BATCH_SIZE=10.
MAX_FEATURES = int(os.environ.get("MAX_FEATURES", "120"))
EDITOR_BATCH_SIZE = int(os.environ.get("EDITOR_BATCH_SIZE", "10"))
EDITOR_STRICT = os.environ.get("EDITOR_STRICT", "").lower() in ("1", "true", "yes")
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
                           fallback_models=EDITOR_FALLBACK_MODELS, stage="editor")
    cats = result.get("categories", {}) if isinstance(result, dict) else {}
    return cats if isinstance(cats, dict) else {}


def _model_block(gate_meta) -> dict:
    """Which model ACTUALLY answered, per stage, plus the failover log.

    DISPLAY VALUE ONLY. Nothing reads this back to decide what to call next —
    routing always starts from the configured model (see llm.py), so a model
    that was merely busy today is still tried first tomorrow.

    The gatekeeper's record is read from scored_articles.json when present, so
    this is correct whether the stages ran in one process (run.py) or separately.
    """
    editor = llm.stage_report("editor", EDITOR_MODEL)
    gate = (gate_meta if isinstance(gate_meta, dict) and gate_meta.get("configured")
            else llm.stage_report("gatekeeper", GATEKEEPER_MODEL))

    def merged(key: str) -> list[str]:
        out: list[str] = []
        for e in list(gate.get(key) or []) + list(editor.get(key) or []):
            if e not in out:
                out.append(e)
        return out

    trim = lambda r: {k: v for k, v in r.items()                    # noqa: E731
                      if k not in ("events", "alerts")}
    # `alerts` is what survived the run but still needs a human — an unusable
    # key, a model id this account cannot see. The briefing ships anyway; the
    # dashboard flags it so a silent degradation isn't discovered a week later.
    return {"editor": trim(editor), "gatekeeper": trim(gate),
            "events": merged("events"), "alerts": merged("alerts")}


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
        except ModelsBusyError as exc:
            # Temporary and total: degrading every batch would overwrite a good
            # briefing with headline-only cards. Abort before dispatch runs.
            print(f"[editor] batch {n}/{len(batches)}: {exc}", flush=True)
            raise
        except FatalLlmError:
            raise
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

    # also_notable = feature-tier overflow (above the ceiling) + notable tier.
    #
    # SCORE IS CARRIED THROUGH. Overflow items are feature-tier (score >= 7) and
    # were previously written with no score at all, which made a 9-scoring
    # spillover indistinguishable from a 5-scoring notable item and caused the
    # dashboard's score filter to hide the best content on the busiest days.
    # "tier" lets the UI label overflow separately from true notable-tier.
    also_notable = (
        [{"title": a["title"], "source": a["source"], "url": a["url"],
          "score": a.get("score", 0), "tier": "overflow"} for a in overflow] +
        [{"title": a["title"], "source": a["source"], "url": a["url"],
          "score": a.get("score", 0), "tier": "notable"} for a in notable]
    )
    also_notable.sort(key=lambda a: a["score"], reverse=True)

    # ZERO-YIELD GUARD, matching the gatekeeper's. Features went in, no cards
    # came out: publishing that would replace a good briefing with a blank page.
    if features and not categories:
        raise RuntimeError(
            f"{len(features)} feature-tier articles produced zero cards. Nothing "
            f"was written; the published briefing is untouched. Check the batch "
            f"lines above for the shape the model returned.")

    models = _model_block(data.get("meta", {}).get("gatekeeper_model")
                          if isinstance(data, dict) else None)
    briefing = {
        "categories": categories,
        "date": today,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "also_notable": also_notable,
        "models": models,
    }

    OUT_FILE.write_text(json.dumps(briefing, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    n_cards = sum(len(v) for v in categories.values())
    scored = sum(1 for a in also_notable if a.get("score"))
    ed = models["editor"]
    print(f"[editor] output produced by {ed['effective'] or '(none)'}"
          + (f" — FAILED OVER from {ed['primary']}" if ed["fell_back"] else "")
          + f" | gatekeeper: {models['gatekeeper'].get('effective') or '(unknown)'}",
          flush=True)
    for a in models["alerts"]:
        print(f"[editor] ACTION NEEDED — {a}", flush=True)
    print(f"[editor] {n_cards} feature cards"
          + (f" ({degraded} degraded)" if degraded else "")
          + f", {len(also_notable)} notable ({scored} scored"
          + (f", {len(overflow)} overflow" if overflow else "") + ")"
          + f" -> {OUT_FILE.name}", flush=True)


if __name__ == "__main__":
    main()
