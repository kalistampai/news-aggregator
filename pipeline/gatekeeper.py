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
Aborting here means Supabase keeps yesterday's briefing untouched.

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
# Allow publishing a briefing with no items (a genuinely empty day).
EMPTY_OK = os.environ.get("EMPTY_OK", "").lower() in ("1", "true", "yes")

# SECTIONS SAFETY NET. A briefing with notable items but zero feature-tier cards
# renders as a bare link strip with no sections, which reads as broken even when
# the scoring was correct — it just means nothing cleared the promotion bar that
# day. Rather than show that at 06:17, promote the highest-scoring notable items.
# Set PROMOTE_ON_EMPTY=0 to keep the strict tiering and accept a section-less day.
PROMOTE_ON_EMPTY = os.environ.get("PROMOTE_ON_EMPTY", "1").lower() in ("1", "true", "yes")
PROMOTE_MIN_SCORE = int(os.environ.get("PROMOTE_MIN_SCORE", "6"))
PROMOTE_MAX = int(os.environ.get("PROMOTE_MAX", "6"))


class EmptyScoringError(RuntimeError):
    """Scoring produced nothing publishable — abort before overwriting the row."""
# The chain (primary + ordered fallbacks, provider-prefixed) is defined in llm.py
# so both stages and the workflow agree on one source of truth.
FALLBACK_MODELS = GATEKEEPER_FALLBACK_MODELS


def _batched(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


_VERDICT_KEYS = ("verdicts", "articles", "results", "items", "scores", "data",
                 "output", "response")


def _looks_like_verdict(v) -> bool:
    return isinstance(v, dict) and ("score" in v or "tier" in v)


def _unwrap(verdicts):
    """Normalise whatever wrapper the model used into a list of verdict dicts.

    This is load-bearing, not defensive padding. OpenAI's JSON-object mode
    CANNOT return a bare top-level array, so the response is always wrapped —
    and the wrapper key is the model's choice. An unrecognised wrapper used to
    yield an empty list, which scored zero articles and published an empty
    briefing over a good one without a single error in the log.
    """
    if isinstance(verdicts, list):
        return [v for v in verdicts if isinstance(v, dict)]
    if not isinstance(verdicts, dict):
        return []

    for key in _VERDICT_KEYS:                       # the expected shape first
        val = verdicts.get(key)
        if isinstance(val, list):
            return [v for v in val if isinstance(v, dict)]
        if isinstance(val, dict):
            return _unwrap(val)

    if _looks_like_verdict(verdicts):               # a single bare verdict
        return [verdicts]

    for val in verdicts.values():                   # any list of verdicts
        if isinstance(val, list) and any(_looks_like_verdict(v) for v in val):
            return [v for v in val if isinstance(v, dict)]

    # {"a1": {...}, "a2": {...}} — keyed by id, with the id only in the key.
    if all(_looks_like_verdict(v) for v in verdicts.values()) and verdicts:
        return [{"id": k, **v} for k, v in verdicts.items()]

    for val in verdicts.values():                   # one level of nesting
        if isinstance(val, dict):
            found = _unwrap(val)
            if found:
                return found
    return []


def _shape_of(obj) -> str:
    """One-line description of an unusable response, for the log."""
    if isinstance(obj, dict):
        return f"object with keys {sorted(obj)[:8]}"
    if isinstance(obj, list):
        kinds = {type(v).__name__ for v in obj[:5]}
        return f"array of {len(obj)} ({', '.join(sorted(kinds)) or 'empty'})"
    return type(obj).__name__


def main() -> None:
    articles = json.loads(IN_FILE.read_text(encoding="utf-8"))
    by_id = {str(a["id"]): a for a in articles}   # str both sides — see below
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
            raw = complete_json(
                PROMPT, payload, GATEKEEPER_MODEL, max_tokens=8000,
                fallback_models=FALLBACK_MODELS, stage="gatekeeper")
            verdicts = _unwrap(raw)
            if not verdicts:
                # Parsed fine, carried nothing we recognise. Say what came back:
                # a silent empty batch is how 130 articles became a blank page.
                print(f"[gatekeeper] batch {n}/{len(batches)}: NO VERDICTS in a "
                      f"valid response — {_shape_of(raw)}", flush=True)
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
        unmatched_ids = []
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            # str() both sides: a model that echoes "12" as 12 must still match.
            src = by_id.get(str(v.get("id")))
            if not src:
                unmatched_ids.append(v.get("id"))
                continue
            matched += 1
            scored.append({**src,
                           "score": v.get("score", 0),
                           "tier": v.get("tier", "reject"),
                           "gatekeeper_reasoning": v.get("reasoning", "")})

        print(f"[gatekeeper] batch {n}/{len(batches)}: "
              f"{matched}/{len(batch)} scored"
              + (f" | {len(unmatched_ids)} unknown id(s): "
                 f"{unmatched_ids[:3]}" if unmatched_ids else ""), flush=True)

    features = [a for a in scored if a["tier"] == "feature"]
    notable = [a for a in scored if a["tier"] == "notable"]
    features.sort(key=lambda a: a["score"], reverse=True)
    notable.sort(key=lambda a: a["score"], reverse=True)

    # No article cleared the feature bar. Promote the best of the notable tier so
    # the briefing still has sections — logged, never silent.
    if not features and notable and PROMOTE_ON_EMPTY:
        promoted = [a for a in notable if a["score"] >= PROMOTE_MIN_SCORE][:PROMOTE_MAX]
        if promoted:
            for a in promoted:
                a["tier"] = "feature"
                a["promoted"] = True
            features = promoted
            notable = [a for a in notable if not a.get("promoted")]
            print(f"[gatekeeper] no article reached feature tier; promoting the "
                  f"{len(promoted)} highest-scoring notable item(s) "
                  f"(score >= {PROMOTE_MIN_SCORE}) so the briefing has sections. "
                  f"Set PROMOTE_ON_EMPTY=0 to disable.", flush=True)

    # ZERO-YIELD GUARD. 130 articles in and nothing out is not a quiet news day,
    # it is a broken response contract — and continuing publishes a blank page
    # over yesterday's good briefing. Abort here instead: dispatch never runs and
    # the stored briefing stands. EMPTY_OK=1 overrides for a genuine empty day.
    if not articles:
        # Ingest kept nothing — usually every candidate was already published in
        # an earlier run today. There is nothing to publish, and writing an empty
        # briefing would replace a good one. Re-run with CROSS_RUN_DEDUPE=0 (the
        # "re-ingest already-published articles" checkbox) to recover a day.
        raise EmptyScoringError(
            "ingest produced no articles, so there is nothing to score or "
            "publish. The existing briefing is left untouched.")
    if articles and not scored:
        raise EmptyScoringError(
            f"{len(articles)} articles were sent to {GATEKEEPER_MODEL} and none "
            f"came back scored. The model answered — the verdicts did not match "
            f"the expected shape (see the NO VERDICTS / unknown id lines above). "
            f"Nothing was written; the published briefing is untouched.")
    if articles and not (features or notable) and not EMPTY_OK:
        raise EmptyScoringError(
            f"all {len(scored)} scored articles were rejected, so there is "
            f"nothing to publish. Keeping the previous briefing rather than "
            f"overwriting it with an empty one. Set EMPTY_OK=1 to publish anyway.")

    report = llm.stage_report("gatekeeper", GATEKEEPER_MODEL)
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
