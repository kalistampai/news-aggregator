"""
Shared LLM client — Google Gemini free-tier drop-in.

Exposes exactly the interface the pipeline expects:
  - GATEKEEPER_MODEL / EDITOR_MODEL   (model-id constants, overridable via env)
  - complete_json(system_prompt, user_payload, model, max_tokens) -> parsed JSON

Only gatekeeper.py and editor.py import this module, and they use just those
three symbols, so this is a straight replacement for the Anthropic version.

Auth: reads GEMINI_API_KEY from the environment (set as a GitHub Actions secret).
Get a free key at https://aistudio.google.com/apikey — no billing required.
"""
from __future__ import annotations
import json
import os
import time

from google import genai
from google.genai import types

# Free-tier models (no billing). Flash-Lite = cheapest / highest throughput ->
# the high-volume gatekeeper; Flash = stronger synthesis -> the editor.
GATEKEEPER_MODEL = os.environ.get("GATEKEEPER_MODEL", "gemini-2.5-flash-lite")
EDITOR_MODEL = os.environ.get("EDITOR_MODEL", "gemini-2.5-flash")

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MAX_RETRIES = 5
BACKOFF_BASE = 4  # seconds -> 4, 8, 16, 32 between retries


def _extract_json(text: str | None):
    """Parse model output as JSON, tolerating an accidental ```json fence or None text."""
    if not text:
        raise ValueError("Model returned None or empty text (likely blocked by safety filters or recitation checks).")
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").lstrip()
        if t[:4].lower() == "json":
            t = t[4:]
    return json.loads(t.strip())


def complete_json(system_prompt: str, user_payload: str, model: str,
                  max_tokens: int = 4000):
    """Call Gemini with forced-JSON output and return the parsed object.

    Returns whatever the model emits — a list (gatekeeper) or a dict (editor).
    Retries transient / rate-limit errors with exponential backoff, since the
    free tier enforces per-minute request caps.
    """
    # Lower safety thresholds to prevent false positives on offensive security/CVE topics
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
        response_mime_type="application/json",  # enforce clean JSON, no fences
        max_output_tokens=max_tokens,
        temperature=0,                          # deterministic scoring/synthesis
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
            # Added "none", "empty", and "blocked" so safety filter hits trigger a retry
            transient = any(s in msg for s in (
                "429", "resource_exhausted", "rate", "quota",
                "503", "unavailable", "500", "internal", "timeout",
                "none", "empty", "blocked", "safety",
            ))
            if attempt < MAX_RETRIES - 1 and transient:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
                continue
            raise
    raise last_err  # pragma: no cover
