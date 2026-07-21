"""
Shared LLM client — Google Gemini free-tier drop-in.

Exposes exactly the interface the pipeline expects:
  - GATEKEEPER_MODEL / EDITOR_MODEL   (model-id constants, overridable via env)
  - complete_json(system_prompt, user_payload, model, max_tokens, fallback_models)
        -> parsed JSON

Resilience:
  - Per-model exponential backoff WITH JITTER, so a short 5xx/429 spike is ridden out
    rather than propagated.
  - Optional `fallback_models`: if the primary model stays transiently unavailable
    (503 / overload / quota / timeout) after all retries, the SAME request is retried
    on the next model in the list. This is what turns a single-model outage from a
    full-pipeline abort into a non-event (the failure that broke the 2026-07-21 run
    was exactly this: the editor model 503'd while the gatekeeper model was up).
  - A NON-transient error (bad request, auth failure, unknown model id) is raised
    immediately, without burning retries or fallbacks.
"""
from __future__ import annotations
import json
import os
import random
import re
import time

from google import genai
from google.genai import types

GATEKEEPER_MODEL = os.environ.get("GATEKEEPER_MODEL", "gemini-3.1-flash-lite")
EDITOR_MODEL = os.environ.get("EDITOR_MODEL", "gemini-3.5-flash")

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MAX_RETRIES = 6      # attempts per model
BACKOFF_BASE = 4     # seconds -> 4, 8, 16, 32, 60(capped) between retries, plus jitter
BACKOFF_CAP = 60     # never sleep longer than this between attempts
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Substrings marking an error as retryable / worth failing over to another model.
_TRANSIENT_MARKERS = (
    "429", "resource_exhausted", "rate", "quota",
    "500", "internal", "502", "503", "504", "unavailable", "overloaded",
    "timeout", "timed out", "deadline", "connection", "temporarily",
    "none", "empty", "blocked", "safety",
    "extra data", "unterminated", "expecting", "decode", "json",
)


def _extract_json(text: str | None):
    """Parse the first valid JSON object or array from the output, ignoring trailing extra data."""
    if not text:
        raise ValueError("Model returned None or empty text.")

    text = _FENCE.sub("", text).strip()

    # Find where the JSON payload actually begins ([ for lists, { for dicts).
    start = min([i for i in (text.find("["), text.find("{")) if i != -1], default=-1)
    if start == -1:
        raise ValueError("No JSON structure found in model output.")

    # raw_decode parses exactly ONE valid JSON value starting at `start` and stops as
    # soon as it ends, ignoring any trailing text/JSON blocks.
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text, idx=start)
    return obj


def _is_transient(exc: Exception) -> bool:
    """True if the error is worth retrying and/or failing over to another model."""
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return True   # truncated / malformed / empty output — a retry may succeed
    msg = str(exc).lower()
    return any(s in msg for s in _TRANSIENT_MARKERS)


def _config(system_prompt: str, max_tokens: int) -> "types.GenerateContentConfig":
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
    return types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        max_output_tokens=max_tokens,
        temperature=0,
        safety_settings=safety_settings,
    )


def _complete_one_model(system_prompt: str, user_payload: str, model: str,
                        max_tokens: int):
    """Call ONE model with forced-JSON output, retrying transient failures. Raises on exhaustion."""
    config = _config(system_prompt, max_tokens)
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = _client.models.generate_content(
                model=model, contents=user_payload, config=config,
            )
            return _extract_json(resp.text)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < MAX_RETRIES - 1 and _is_transient(exc):
                sleep = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))
                sleep += random.uniform(0, BACKOFF_BASE)   # jitter
                time.sleep(sleep)
                continue
            raise
    assert last_err is not None  # pragma: no cover
    raise last_err


def complete_json(system_prompt: str, user_payload: str, model: str,
                  max_tokens: int = 8000, fallback_models: list[str] | None = None):
    """
    Call Gemini with forced-JSON output and return the parsed object.

    Tries `model` first (with retries). If it stays transiently unavailable
    (overload / quota / timeout) after every retry, fails over to each model in
    `fallback_models`, in order. A non-transient error is raised immediately.
    """
    models = [model] + [m for m in (fallback_models or []) if m and m != model]
    last_err: Exception | None = None
    for i, m in enumerate(models):
        try:
            return _complete_one_model(system_prompt, user_payload, m, max_tokens)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            is_last = i == len(models) - 1
            if not is_last and _is_transient(exc):
                print(f"[llm] '{m}' unavailable after retries "
                      f"({type(exc).__name__}); failing over to '{models[i + 1]}'",
                      flush=True)
                continue
            raise
    assert last_err is not None  # pragma: no cover
    raise last_err
