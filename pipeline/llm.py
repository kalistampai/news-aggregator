"""
Thin Anthropic client wrapper shared by the gatekeeper and editor stages.
Handles the API call, strips accidental markdown fences, and parses JSON with
one automatic retry (models occasionally add stray prose despite instructions).
"""
from __future__ import annotations
import json
import os
import re
import time

from anthropic import Anthropic

_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Fast + cheap for the gatekeeper; stronger reasoning for the editor.
GATEKEEPER_MODEL = os.environ.get("GATEKEEPER_MODEL", "claude-haiku-4-5-20251001")
EDITOR_MODEL = os.environ.get("EDITOR_MODEL", "claude-sonnet-5")

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str):
    text = _FENCE.sub("", text).strip()
    # Grab the outermost JSON array or object if the model wrapped it in prose.
    start = min([i for i in (text.find("["), text.find("{")) if i != -1], default=-1)
    if start == -1:
        raise ValueError("no JSON found in model output")
    end = max(text.rfind("]"), text.rfind("}"))
    return json.loads(text[start:end + 1])


def complete_json(system: str, user_payload: str, model: str,
                  max_tokens: int = 8000, retries: int = 1):
    """Call the model, expect JSON, return the parsed object."""
    attempt = 0
    while True:
        resp = _client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_payload}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text")
        try:
            return _extract_json(raw)
        except (ValueError, json.JSONDecodeError):
            if attempt >= retries:
                raise
            attempt += 1
            time.sleep(2)
