"""
Stage 3 — Deep Synthesis & Structuring (The Editor Agent)

Takes feature-tier articles, synthesizes 3-bullet cards grouped by category, and
passes notable-tier items through verbatim. Writes briefing.json — the exact blob
the dashboard renders and dispatch.py pushes to the Gist.

URLs are stitched back in from the source records AFTER the model returns, so a
hallucinated link can never survive even if the model ignores the constraint.
"""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path

from llm import EDITOR_MODEL, complete_json

HERE = Path(__file__).parent
IN_FILE = HERE / "scored_articles.json"
OUT_FILE = HERE / "briefing.json"
PROMPT = (HERE / "prompts" / "editor.txt").read_text()

MAX_FEATURES = 40   # hard ceiling on cards sent to the expensive model


def main() -> None:
    data = json.loads(IN_FILE.read_text())
    features = data["features"][:MAX_FEATURES]
    notable = data["notable"]

    # Authoritative URL/source/score map -> re-attached after synthesis (anti-hallucination).
    truth = {a["id"]: {"url": a["url"], "source": a["source"],
                       "title": a["title"], "score": a["score"]}
             for a in features}

    payload = json.dumps({
        "today": dt.date.today().isoformat(),
        "features": [{"id": a["id"], "title": a["title"], "source": a["source"],
                      "url": a["url"], "snippet": a["snippet"],
                      "why": a["gatekeeper_reasoning"]} for a in features],
    }, ensure_ascii=False)

    briefing = complete_json(PROMPT, payload, EDITOR_MODEL, max_tokens=8000)

    # Overwrite every card URL with the trusted original, matched by title.
    title_to_truth = {v["title"].strip().lower(): v for v in truth.values()}
    for cards in briefing.get("categories", {}).values():
        for card in cards:
            match = title_to_truth.get(card.get("title", "").strip().lower())
            if match:
                card["url"] = match["url"]
                card["source"] = match["source"]
                card["score"] = match["score"]

    briefing["date"] = dt.date.today().isoformat()
    briefing["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    briefing["also_notable"] = [
        {"title": a["title"], "source": a["source"], "url": a["url"]}
        for a in notable
    ]

    OUT_FILE.write_text(json.dumps(briefing, indent=2, ensure_ascii=False))
    n_cards = sum(len(v) for v in briefing.get("categories", {}).values())
    print(f"[editor] {n_cards} feature cards, "
          f"{len(briefing['also_notable'])} notable -> {OUT_FILE.name}", flush=True)


if __name__ == "__main__":
    main()
