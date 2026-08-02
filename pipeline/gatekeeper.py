"""
Stage 2 — Semantic Relevance Filtering (The Gatekeeper Agent)

Scores raw_articles.json in batches with a fast model, assigns a tier, and writes
scored_articles.json. EVERY article is scored — batching is chunking, not a cap.

Pacing is handled centrally in llm.py (<PROVIDER>_MIN_INTERVAL), which enforces a
per-provider RPM ceiling across retries and failovers too — so the OpenAI models
run fast while the Gemini last-resort fallback stays under its free-tier limit.
No local time.sleep() needed.

A batch whose OUTPUT is unusable after all retries + fallback is skipped rather
than aborting the stage, so one bad chunk can't cost you the whole briefing. Set
GATEKEEPER_STRICT=1 to restore hard-fail behaviour.

Two failures are NOT skipped, at any strictness: every model being busy, and a
fatal error (bad key / malformed request / unknown model / 429). Skipping those
would silently score zero articles and publish an empty briefing over a good one.
Aborting here means the Gist keeps yesterday's briefing untouched.

scored_articles.json carries a `meta` block recording which model actually
answered — it rides through the editor into briefing.json so the dashboard can
label the output with the model that produced it, not the one in the settings.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

import llm
from llm import (GATEKEEPER_MODEL, GATEKEEPER_FALLBACK_MODELS, complete_json,
                 FatalLlmError, ModelsBusyError)

HERE = Path(__file__).parent
IN_FILE = HERE / "raw_articles.json"
OUT_FILE = HERE / "scored_articles.json"
PROMPT = (HERE / "prompts" / "gatekeeper.txt").read_text(encoding="utf-8")

# 30 keeps each response comfortably inside max_tokens and makes a failed batch
# cheap to lose. Raise toward 50 to cut request count if you are RPD-constrained.
BATCH_SIZE = int(os.environ.get("GATEKEEPER_BATCH_SIZE", "30"))
STRICT = os.environ.get("GATEKEEPER_STRICT", "").lower() in ("1", "true", "yes")
# The chain (primary + ordered fallbacks, provider-prefixed) is defined in llm.py
# so both stages and the workflow agree on one source of truth.
FALLBACK_MODELS = GATEKEEPER_FALLBACK_MODELS


def _batched(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _unwrap(verdicts):
    """Models occasionally wrap the array in a dict — normalise to a list."""
    if isinstance(verdicts, dict):
        verdicts = (verdicts.get("verdicts") or verdicts.get("articles")
                    or next(iter(verdicts.values()), []))
    return verdicts if isinstance(verdicts, list) else []


def main() -> None:
    articles = json.loads(IN_FILE.read_text(encoding="utf-8"))
    by_id = {a["id"]: a for a in articles}
    scored: list[dict] = []
    failed_batches = 0

    batches = list(_batched(articles, BATCH_SIZE))
    print(f"[gatekeeper] {len(articles)} articles -> {len(batches)} batches "
          f"of {BATCH_SIZE}", flush=True)

    for n, batch in enumerate(batches, 1):
        payload = json.dumps(
            [{"id": a["id"], "title": a["title"],
              "source": a["source"], "snippet": a["snippet"]} for a in batch],
            ensure_ascii=False,
        )
        try:
            verdicts = _unwrap(complete_json(
                PROMPT, payload, GATEKEEPER_MODEL,
                max_tokens=8000, fallback_models=FALLBACK_MODELS))
        except ModelsBusyError as exc:
            # Temporary upstream outage, not a data problem. Abort before
            # anything is written so the published briefing stays as it was.
            print(f"[gatekeeper] batch {n}/{len(batches)}: {exc}", flush=True)
            raise
        except FatalLlmError:
            # Key / request / model-id / quota problem: identical on every batch.
            raise
        except Exception as exc:  # noqa: BLE001
            if STRICT:
                raise
            failed_batches += 1
            print(f"[gatekeeper] batch {n}/{len(batches)} FAILED "
                  f"({type(exc).__name__}); {len(batch)} articles skipped",
                  flush=True)
            continue

        matched = 0
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            src = by_id.get(v.get("id"))
            if not src:
                continue
            matched += 1
            scored.append({**src,
                           "score": v.get("score", 0),
                           "tier": v.get("tier", "reject"),
                           "gatekeeper_reasoning": v.get("reasoning", "")})

        print(f"[gatekeeper] batch {n}/{len(batches)}: "
              f"{matched}/{len(batch)} scored", flush=True)

    features = [a for a in scored if a["tier"] == "feature"]
    notable = [a for a in scored if a["tier"] == "notable"]
    features.sort(key=lambda a: a["score"], reverse=True)
    notable.sort(key=lambda a: a["score"], reverse=True)

    report = llm.stage_report(GATEKEEPER_MODEL)
    OUT_FILE.write_text(json.dumps(
        {"features": features, "notable": notable,
         "meta": {"gatekeeper_model": report}},
        indent=2, ensure_ascii=False), encoding="utf-8")

    if report["fell_back"]:
        print(f"[gatekeeper] answered by {report['effective']} "
              f"(configured: {report['configured']})", flush=True)
    print(f"[gatekeeper] scored {len(scored)}/{len(articles)} | "
          f"feature={len(features)} notable={len(notable)} "
          f"reject={len(scored) - len(features) - len(notable)}"
          + (f" | {failed_batches} batch(es) failed" if failed_batches else ""),
          flush=True)


if __name__ == "__main__":
    main()
