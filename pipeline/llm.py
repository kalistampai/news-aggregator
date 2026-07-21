"""
Shared LLM client — Google Gemini free-tier drop-in.

Exposes exactly the interface the pipeline expects:
  - GATEKEEPER_MODEL / EDITOR_MODEL   (model-id constants, overridable via env)
  - complete_json(system_prompt, user_payload, model, max_tokens) -> parsed JSON
"""
from __future__ import annotations
import json
import os
import re
import time

from google import genai
from google.genai import types

GATEKEEPER_MODEL = os.environ.get("GATEKEEPER_MODEL", "gemini-2.5-flash-lite")
EDITOR_MODEL = os.environ.get("EDITOR_MODEL", "gemini-2.5-flash")

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MAX_RETRIES = 5
BACKOFF_BASE = 4  # seconds -> 4, 8, 16, 32 between retries
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str | None):
    """Parse the first valid JSON object or array from the output, ignoring trailing extra data."""
    if not text:
        raise ValueError("Model returned None or empty text.")

    text = _FENCE.sub("", text).strip()

    # Find where the JSON payload actually begins ([ for lists, { for dicts)
    start = min([i for i in (text.find("["), text.find("{")) if i != -1], default=-1)
    if start == -1:
        raise ValueError("No JSON structure found in model output.")

    # raw_decode parses exactly ONE valid JSON object starting at `start`
    # and stops as soon as that object ends, completely ignoring trailing text/JSON blocks.
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text, idx=start)
    return obj


def complete_json(system_prompt: str, user_payload: str, model: str,
                  max_tokens: int = 4000):
    """Call Gemini with forced-JSON output and return the parsed object."""
    safety_settings = [
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
        ),
    ]

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        max_output_tokens=max_tokens,
        temperature=0,
        safety_settings=safety_settings,
    )

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = _client.models.generate_content(
                model=model, contents=user_payload, config=config,
            )
            return _extract_json(resp.text)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            msg = str(exc).lower()
            transient = any(s in msg for s in (
                "429", "resource_exhausted", "rate", "quota",
                "503", "unavailable", "500", "internal", "timeout",
                "none", "empty", "blocked", "safety", "extra data",
            ))
            if attempt < MAX_RETRIES - 1 and transient:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
                continue
            raise
    raise last_err  # pragma: no cover
