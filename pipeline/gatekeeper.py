"""
Stage 2 — Semantic Relevance Filtering (The Gatekeeper Agent)

Scores raw_articles.json in batches with a fast model, assigns a tier, and writes
scored_articles.json. Batching keeps each request small and cheap and avoids
context limits on busy news days.
"""
from __future__ import annotations
import json
from pathlib import Path

from llm import GATEKEEPER_MODEL, complete_json

HERE = Path(__file__).parent
IN_FILE = HERE / "raw_articles.json"
OUT_FILE = HERE / "scored_articles.json"
PROMPT = (HERE / "prompts" / "gatekeeper.txt").read_text()

BATCH_SIZE = 20   # articles per LLM call


def _batched(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main() -> None:
    articles = json.loads(IN_FILE.read_text())
    by_id = {a["id"]: a for a in articles}
    scored: list[dict] = []

    for batch in _batched(articles, BATCH_SIZE):
        payload = json.dumps(
            [{"id": a["id"], "title": a["title"],
              "source": a["source"], "snippet": a["snippet"]} for a in batch],
            ensure_ascii=False,
        )
        verdicts = complete_json(PROMPT, payload, GATEKEEPER_MODEL, max_tokens=4000)
        for v in verdicts:
            src = by_id.get(v.get("id"))
            if not src:
                continue
            # Merge the model's score/tier back onto the full article record.
            scored.append({**src,
                           "score": v.get("score", 0),
                           "tier": v.get("tier", "reject"),
                           "gatekeeper_reasoning": v.get("reasoning", "")})

    features = [a for a in scored if a["tier"] == "feature"]
    notable = [a for a in scored if a["tier"] == "notable"]
    features.sort(key=lambda a: a["score"], reverse=True)

    OUT_FILE.write_text(json.dumps(
        {"features": features, "notable": notable},
        indent=2, ensure_ascii=False,
    ))
    print(f"[gatekeeper] scored {len(scored)} | "
          f"feature={len(features)} notable={len(notable)} "
          f"reject={len(scored) - len(features) - len(notable)}", flush=True)


if __name__ == "__main__":
    main()
