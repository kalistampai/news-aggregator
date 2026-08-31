"""
Runtime model configuration, read from Supabase instead of the workflow file.

WHY THIS EXISTS
The dashboard's Settings tab is the single source of truth for which model
writes the briefing and which credentials it uses. Those values live in
`news_aggregator.app_settings` (migration 004); this module reads that row at
the start of a run and pushes it into llm.py before any stage is called.

Before this, model choice lived in .github/workflows/daily.yml, so changing it
meant editing a workflow file and pushing a commit. Now the workflow carries
only SUPABASE_URL / SUPABASE_SERVICE_KEY / SUPABASE_SCHEMA and is a dumb runner.

FAILURE POSTURE — the whole point of this file's error handling
This runs at 06:17 Pacific with nobody awake, and it is a CONFIGURATION read: it
produces no briefing content of its own. So every failure here degrades to the
environment defaults baked into llm.py rather than aborting. A missing table (no
migration yet), an unreachable Supabase, a malformed row — all of them log
loudly and let the run continue on whatever the environment already provided.
The alternative, aborting because we could not read a preference, would trade a
briefing that is merely configured the old way for no briefing at all.

The one thing that is NOT tolerated silently is a partially-applied config: a
row that names a provider whose key is blank is reported as an alert, because
that is the mistake that turns into "every model is unusable" three stages later
with no obvious cause.
"""
from __future__ import annotations

import os

import llm
import store

# The columns the dashboard writes. Kept here as one list so the log line, the
# validation and the llm hand-off cannot drift apart.
_MODEL_FIELDS = ("gatekeeper_model", "editor_model")
_CHAIN_FIELDS = ("gatekeeper_fallback_models", "editor_fallback_models")
_KEY_FIELDS = {
    "openai_api_key": llm.OPENAI,
    "anthropic_api_key": llm.ANTHROPIC,
    "gemini_api_key": llm.GEMINI,
}

# Set SETTINGS_FROM_DB=0 to ignore the table entirely and run purely on env vars
# — the way to reproduce a run locally, or to bypass a bad row without having to
# reach the dashboard first.
ENABLED = os.environ.get("SETTINGS_FROM_DB", "1").lower() in ("1", "true", "yes")


def _clean(value) -> str:
    """Normalise a nullable text column. NULL and '' both mean 'not set'."""
    return (value or "").strip() if isinstance(value, (str, type(None))) else ""


def _clean_list(value) -> list[str]:
    """Normalise a text[] column, dropping blanks a UI round-trip can leave."""
    if not isinstance(value, list):
        return []
    return [s.strip() for s in value if isinstance(s, str) and s.strip()]


def load() -> dict | None:
    """The settings row, or None when it cannot be read for any reason."""
    if not ENABLED:
        print("[settings] SETTINGS_FROM_DB=0 — using environment defaults.",
              flush=True)
        return None
    if not store.configured():
        print("[settings] SUPABASE_URL / SUPABASE_SERVICE_KEY are not set — "
              "using environment defaults.", flush=True)
        return None
    try:
        row = store.select_one("app_settings", {"id": "eq.1"})
    except Exception as exc:  # noqa: BLE001 — see FAILURE POSTURE above
        print(f"[settings] could not read app_settings ({exc}) — using "
              f"environment defaults.", flush=True)
        return None
    if row is None:
        print("[settings] no app_settings row (migration 004 not applied?) — "
              "using environment defaults.", flush=True)
    return row


def apply() -> dict | None:
    """Read the settings row and push it into llm.py. Returns the row, or None.

    Must be called BEFORE the stage modules read llm's model constants. run.py
    does this before importing them; gatekeeper.py and editor.py additionally
    read `llm.X` at call time rather than binding at import, so a future import
    reshuffle cannot silently disconnect the dashboard from the pipeline.
    """
    row = load()
    if not row:
        return None

    overrides: dict[str, object] = {}

    for field in _MODEL_FIELDS:
        value = _clean(row.get(field))
        if value:
            overrides[field] = value

    for field in _CHAIN_FIELDS:
        value = _clean_list(row.get(field))
        if value:
            overrides[field] = value

    keys = {}
    for column, provider in _KEY_FIELDS.items():
        value = _clean(row.get(column))
        if value:
            keys[provider] = value
    if keys:
        overrides["api_keys"] = keys

    effort = _clean(row.get("openai_reasoning_effort"))
    if effort:
        overrides["openai_reasoning_effort"] = effort

    if not overrides:
        print("[settings] app_settings row is empty — using environment "
              "defaults.", flush=True)
        return row

    llm.apply_settings(overrides)

    # Say out loud what the run is actually configured with. A briefing that
    # came out subtly different is otherwise impossible to attribute to a
    # setting somebody changed in a browser hours earlier.
    print(f"[settings] loaded from Supabase — "
          f"gatekeeper={llm.GATEKEEPER_MODEL}, editor={llm.EDITOR_MODEL}",
          flush=True)
    print(f"[settings] provider keys from Supabase: "
          f"{', '.join(sorted(keys)) or 'none (using environment)'}", flush=True)

    _warn_on_keyless_providers()
    return row


def _warn_on_keyless_providers() -> None:
    """Flag a configured model whose provider has no credential anywhere.

    This is the failure that is worst to debug from the far end: the chain
    reports every candidate as unusable, three stages after the actual mistake.
    Naming it here, at the moment the config is applied, is the difference
    between a one-line fix and reading a stack trace at 6am.
    """
    for label, model in (("gatekeeper", llm.GATEKEEPER_MODEL),
                         ("editor", llm.EDITOR_MODEL)):
        provider, _ = llm.split_model(model)
        if not llm.api_key(provider):
            print(f"[settings] WARNING — the {label} model ({model}) needs a "
                  f"{provider} key, and none is set in app_settings or the "
                  f"environment. The chain will fail over to a provider that "
                  f"has one.", flush=True)
