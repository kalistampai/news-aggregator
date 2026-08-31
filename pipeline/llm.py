"""
Shared LLM client — OpenAI, Anthropic and Google Gemini behind one call path.

Exposes exactly the interface the pipeline expects:
  - GATEKEEPER_MODEL / EDITOR_MODEL             (primary model per stage)
  - GATEKEEPER_FALLBACK_MODELS / EDITOR_FALLBACK_MODELS
  - complete_json(system_prompt, user_payload, model, max_tokens, fallback_models)
        -> parsed JSON
  - stage_report(requested_model)   -> which model ACTUALLY answered (display)
  - alerts()                        -> problems that need a human, if any
  - apply_settings(overrides)       -> override the chain + keys from Supabase
  - api_key(provider)               -> credential, settings first then env

WHERE THE CONFIGURATION COMES FROM
The module-level constants below are DEFAULTS, read from the environment at
import. The real values normally arrive at runtime from the dashboard's Settings
tab, which writes `news_aggregator.app_settings`; settings.apply() reads that row
and calls apply_settings() before any stage runs. That is why the stages read
`llm.GATEKEEPER_MODEL` at call time instead of importing the name — a
`from llm import GATEKEEPER_MODEL` would freeze the pre-override default and
silently ignore whatever the dashboard was told.

MODEL IDS CARRY THEIR PROVIDER: "openai:gpt-5.6-sol", "anthropic:claude-sonnet-5",
"gemini:gemini-3.5-flash". An unprefixed id is inferred from its name (gpt-*/o* ->
openai, claude-* -> anthropic, gemini-* -> gemini) and otherwise falls back to
LLM_DEFAULT_PROVIDER. Because the provider travels with the id, the fallback
chain spans providers for free: the editor can try three OpenAI models and then
Gemini, using one ordered list and one code path.

THIS PIPELINE RUNS UNATTENDED AT 06:17 PACIFIC. Nobody is awake to restart it, so
the ordering principle throughout is: keep going if anything can still answer,
and make noise about what went wrong rather than aborting on it. The one
exception is a malformed request, which is identical on every model and would
just fail N more times.

Resilience:
  - GLOBAL RATE LIMIT, per provider. Every call passes through a throttle that
    enforces a minimum gap between requests to the same provider
    (<PROVIDER>_MIN_INTERVAL, else LLM_MIN_INTERVAL). This is what keeps the
    Gemini free tier under its RPM ceiling even while OpenAI runs much faster —
    retries and failovers are counted too.
  - Per-model exponential backoff WITH JITTER, so a short spike is ridden out
    rather than propagated.
  - `fallback_models`: the SAME request is retried down the chain, in order.
    Every failover is printed, because a silent one is impossible to debug from
    a briefing that looks subtly different than usual.
  - When EVERY candidate is busy, ModelsBusyError is raised with a distinct,
    deliberately non-actionable message: the run failed for a reason that fixes
    itself, and nothing downstream was written.

Error classification (the thing that decides retry vs. failover vs. raise):
  BUSY      5xx, "overloaded", "try again later", timeouts, dropped connections.
            Transient and says nothing about whether a model is usable ->
            retry, then fail over to the next candidate.
  SOFT      The call succeeded but the OUTPUT was unusable (empty, truncated,
            safety-blocked, not JSON) -> retry the same model, then fail over.
            Tracked separately from BUSY so "everything is overloaded" is never
            reported when the real problem was the response body.
  UNUSABLE  This candidate cannot serve the request, permanently, today: unknown
            model id, missing/invalid API key, exhausted credit. Advancing is the
            only way the morning report still ships, so the chain advances — but
            the cause is recorded in alerts(), printed as ACTION NEEDED, and
            carried into briefing.json so the dashboard shows it. A key problem
            skips every model from that provider; an unknown id skips only itself.
  FATAL     A malformed request (400). Identical on every candidate, so it is
            raised immediately.
  Unrecognised errors default to FATAL — surfacing an unknown failure is safer
  than silently absorbing it into the fallback chain.

  429 is BUSY when LLM_RETRY_429 is on (the default for unattended runs: quota
  spikes recover on their own), FATAL when it is off. An OpenAI 429 that is
  really "you are out of credit" is detected by message and treated as UNUSABLE,
  so the chain moves to the next provider instead of retrying a dry account.

Routing state vs. display state — these are deliberately two different things:
  - _BUSY_UNTIL / _DEAD_MODELS / _DEAD_PROVIDERS are ROUTING and SESSION-ONLY
    (plain in-memory dicts, never written to disk). A model that 503s is skipped
    for LLM_BUSY_COOLDOWN seconds so the remaining batches of this run don't each
    re-discover the outage, but the next process starts clean and tries the
    preferred model FIRST again.
  - _EFFECTIVE is DISPLAY and is what gets persisted downstream (into
    briefing.json, then the dashboard label). It records which model actually
    produced the output. It must never be read back into routing decisions.

Note on sampling params: neither provider is sent temperature/top_p/top_k. Gemini
3.x ignores them and the newest OpenAI models reject them; forced-JSON output plus
the schema in each prompt keep responses well-formed.
"""
from __future__ import annotations
import json
import os
import random
import re
import threading
import time

# ---- providers ---------------------------------------------------------------
OPENAI, ANTHROPIC, GEMINI = "openai", "anthropic", "gemini"
PROVIDERS = (OPENAI, ANTHROPIC, GEMINI)
LLM_DEFAULT_PROVIDER = os.environ.get("LLM_DEFAULT_PROVIDER", OPENAI).strip().lower()

# Credentials resolve through api_key(), NOT os.environ directly, so the
# dashboard's Settings tab can supply them (settings.apply -> apply_settings)
# without this module having to care where they came from. Values set here take
# precedence over the environment; the environment remains the fallback so a run
# with SETTINGS_FROM_DB=0, or one predating migration 004, behaves exactly as
# before. Deliberately NOT written into os.environ: that would leak the key into
# every subprocess and any library that dumps the environment on error.
_ENV_KEYS = {
    OPENAI: "OPENAI_API_KEY",
    ANTHROPIC: "ANTHROPIC_API_KEY",
    GEMINI: "GEMINI_API_KEY",
}
_API_KEYS: dict[str, str] = {}


def api_key(provider: str) -> str:
    """The credential for `provider`: settings first, then the environment."""
    override = _API_KEYS.get(provider, "").strip()
    if override:
        return override
    return os.environ.get(_ENV_KEYS.get(provider, ""), "").strip()


def _split(csv: str) -> list[str]:
    return [m.strip() for m in csv.split(",") if m.strip()]


# Model chain. "Best first, then the next one that answers" — the order below is
# the priority order; every entry is overridable by env or from the dashboard.
#
# DEPTH WITHIN A PROVIDER IS THE POINT, not just breadth across providers.
# Quotas are metered PER MODEL, so the single most likely failure — one model
# hitting its own cap — is answered by the next model on the SAME key, without
# needing a second vendor to be configured at all. Two Gemini entries mean a
# Gemini-only setup still has somewhere to go; the cross-provider hops after
# them cover the rarer case of an outage or a dead account.
_OPENAI_BEST = os.environ.get("OPENAI_BEST_MODEL", "gpt-5.6-sol")
_OPENAI_NEXT = os.environ.get("OPENAI_NEXT_MODEL", "gpt-5.6-terra")
_OPENAI_LAST = os.environ.get("OPENAI_LAST_MODEL", "gpt-5.6-luna")
_GEMINI_MAIN = os.environ.get("GEMINI_MAIN_MODEL", "gemini-3.5-flash")
_GEMINI_FLOOR = os.environ.get("GEMINI_FLOOR_MODEL", "gemini-3.1-flash-lite")
_DEFAULT_CHAIN = ",".join((
    f"{OPENAI}:{_OPENAI_NEXT}", f"{OPENAI}:{_OPENAI_LAST}",
    f"{GEMINI}:{_GEMINI_MAIN}", f"{GEMINI}:{_GEMINI_FLOOR}",
))

GATEKEEPER_MODEL = os.environ.get("GATEKEEPER_MODEL", f"{OPENAI}:{_OPENAI_BEST}")
EDITOR_MODEL = os.environ.get("EDITOR_MODEL", f"{OPENAI}:{_OPENAI_BEST}")
GATEKEEPER_FALLBACK_MODELS = _split(
    os.environ.get("GATEKEEPER_FALLBACK_MODELS", _DEFAULT_CHAIN))
EDITOR_FALLBACK_MODELS = _split(
    os.environ.get("EDITOR_FALLBACK_MODELS", _DEFAULT_CHAIN))

MAX_RETRIES = 6      # attempts per model
BACKOFF_BASE = 4     # seconds -> 4, 8, 16, 32, 60(capped) between retries, + jitter
BACKOFF_CAP = 60     # never sleep longer than this between attempts
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Seconds a model stays skipped after it answered BUSY. Session-only (see above).
# 0 disables the skip, so every call re-tries the preferred model first.
LLM_BUSY_COOLDOWN = float(os.environ.get("LLM_BUSY_COOLDOWN", "120"))

# Default ON: an unattended run should ride out a quota spike, not die on it.
# Set LLM_RETRY_429=0 to make 429 fatal instead (useful when debugging by hand).
RETRY_429 = os.environ.get("LLM_RETRY_429", "1").lower() in ("1", "true", "yes")

# Verify configured OpenAI ids against /v1/models once per process, and correct
# a near-miss (e.g. a family name where a dated snapshot id is required) rather
# than 404-ing all night. Set LLM_RESOLVE_MODELS=0 to use ids exactly as given.
RESOLVE_MODELS = os.environ.get("LLM_RESOLVE_MODELS", "1").lower() in ("1", "true", "yes")

# GPT-5.6 reasoning controls. 'low' suits scoring and summarisation; raise to
# "medium"/"high" if card quality suffers, knowing reasoning tokens bill as
# output. The headroom is added on top of each caller's max_tokens so a long
# think cannot truncate the JSON answer.
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "low")
OPENAI_TOKEN_HEADROOM = int(os.environ.get("OPENAI_TOKEN_HEADROOM", "4000"))


def split_model(model_id: str) -> tuple[str, str]:
    """'openai:gpt-5.6-sol' -> ('openai', 'gpt-5.6-sol'). Infers a missing prefix."""
    raw = (model_id or "").strip()
    if ":" in raw:
        prefix, bare = raw.split(":", 1)
        if prefix.strip().lower() in PROVIDERS:
            return prefix.strip().lower(), bare.strip()
    low = raw.lower()
    if low.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return OPENAI, raw
    if low.startswith("claude"):
        return ANTHROPIC, raw
    if low.startswith(("gemini", "models/gemini")):
        return GEMINI, raw
    return LLM_DEFAULT_PROVIDER, raw


def apply_settings(overrides: dict) -> None:
    """Override the module's model chain and credentials from stored settings.

    Called once, by settings.apply(), before any stage runs. Only keys present
    in `overrides` are touched, so a partially-filled settings row leaves the
    rest of the environment-derived configuration intact.

    These are module-level rebinds, which is why gatekeeper.py and editor.py
    read `llm.GATEKEEPER_MODEL` at call time rather than importing the name:
    a `from llm import GATEKEEPER_MODEL` elsewhere would freeze the pre-override
    value and quietly ignore everything the dashboard was told to do.
    """
    global GATEKEEPER_MODEL, EDITOR_MODEL
    global GATEKEEPER_FALLBACK_MODELS, EDITOR_FALLBACK_MODELS
    global OPENAI_REASONING_EFFORT

    if overrides.get("gatekeeper_model"):
        GATEKEEPER_MODEL = overrides["gatekeeper_model"]
    if overrides.get("editor_model"):
        EDITOR_MODEL = overrides["editor_model"]
    if overrides.get("gatekeeper_fallback_models"):
        GATEKEEPER_FALLBACK_MODELS = list(overrides["gatekeeper_fallback_models"])
    if overrides.get("editor_fallback_models"):
        EDITOR_FALLBACK_MODELS = list(overrides["editor_fallback_models"])
    if overrides.get("openai_reasoning_effort"):
        OPENAI_REASONING_EFFORT = overrides["openai_reasoning_effort"]

    for provider, key in (overrides.get("api_keys") or {}).items():
        if provider in PROVIDERS and str(key).strip():
            _API_KEYS[provider] = str(key).strip()


# ---- errors -----------------------------------------------------------------
class LlmError(RuntimeError):
    """Base for errors this module raises on the caller's behalf."""


class ModelsBusyError(LlmError):
    """Every candidate model was transiently unavailable. Retrying later fixes it."""

    def __init__(self, models: list[str], detail: str = ""):
        self.models = list(models)
        # Deliberately says nothing about keys, model ids or settings: none of
        # them is implicated by an overload, and pointing at them sends the
        # reader off to "fix" something that was never wrong.
        super().__init__(
            "Every model is busy right now ("
            + ", ".join(self.models) + ")"
            + (f": {detail}" if detail else "")
            + ". This is temporary — the models are overloaded upstream, which "
              "says nothing about whether they work. Nothing was written and no "
              "data was changed. Re-run when they free up; no configuration "
              "change is needed."
        )


class FatalLlmError(LlmError):
    """A real problem: malformed request, or nothing left that can serve it."""


class ProviderUnavailable(LlmError):
    """A provider cannot be used at all this run (no key, SDK missing)."""


# ---- throttle (per provider) -------------------------------------------------
# Minimum seconds between two calls to the SAME provider in this process.
#   OpenAI paid tier: ~1s is plenty.
#   Gemini free tier: 13s == 4.6 RPM, under the 5 RPM ceiling on 3.5-flash.
# An explicit LLM_MIN_INTERVAL applies to both providers; the per-provider vars
# override it. The Gemini default stays at the free-tier-safe 13s so the
# last-resort fallback cannot 429 itself the moment it is called.
_INTERVAL_DEFAULTS = {OPENAI: "1", ANTHROPIC: "1", GEMINI: "13"}
LLM_MIN_INTERVAL = os.environ.get("LLM_MIN_INTERVAL")
_INTERVALS = {
    p: float(os.environ.get(f"{p.upper()}_MIN_INTERVAL",
                            LLM_MIN_INTERVAL if LLM_MIN_INTERVAL is not None
                            else _INTERVAL_DEFAULTS[p]))
    for p in PROVIDERS
}
_throttle_lock = threading.Lock()
_last_call_at: dict[str, float] = {}
_call_count = 0


def _throttle(provider: str) -> None:
    """Block until this provider's minimum inter-call gap has elapsed."""
    global _call_count
    gap = _INTERVALS.get(provider, 1.0)
    with _throttle_lock:
        _call_count += 1
        if gap <= 0:
            return
        wait = (_last_call_at.get(provider, 0.0) + gap) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at[provider] = time.monotonic()


def call_count() -> int:
    """Total model requests issued this process (including retries)."""
    return _call_count


# ---- routing state (SESSION-ONLY — never persisted) -------------------------
_BUSY_UNTIL: dict[str, float] = {}    # model -> monotonic deadline
_DEAD_MODELS: set[str] = set()        # unknown/unpermitted ids, this run only
_DEAD_PROVIDERS: dict[str, str] = {}  # provider -> why it can't be used at all
_route_lock = threading.Lock()


def _mark_busy(model: str) -> None:
    if LLM_BUSY_COOLDOWN <= 0:
        return
    with _route_lock:
        _BUSY_UNTIL[model] = time.monotonic() + LLM_BUSY_COOLDOWN


def _cooling(model: str) -> bool:
    with _route_lock:
        return _BUSY_UNTIL.get(model, 0.0) > time.monotonic()


def _mark_dead(model: str, provider_wide: bool, why: str) -> None:
    with _route_lock:
        if provider_wide:
            _DEAD_PROVIDERS.setdefault(split_model(model)[0], why)
        else:
            _DEAD_MODELS.add(model)


def _is_dead(model: str) -> bool:
    with _route_lock:
        return model in _DEAD_MODELS or split_model(model)[0] in _DEAD_PROVIDERS


def _candidates(model: str, fallback_models: list[str] | None) -> list[str]:
    """Preferred model first, then fallbacks; minus what cannot answer right now.

    Two filters, in order of permanence:
      - dead: no key, unknown id -> dropped entirely (it cannot succeed today)
      - cooling: 503'd moments ago -> skipped, ADVISORY. If the filters would
        empty the list, cooling is ignored, because trying a maybe-busy model
        always beats not calling at all.
    """
    ordered = [model] + [m for m in (fallback_models or []) if m and m != model]
    ordered = [_resolve(m) for m in ordered]
    seen, unique = set(), []
    for m in ordered:                       # resolution can collapse two ids
        if m not in seen:
            seen.add(m)
            unique.append(m)
    alive = [m for m in unique if not _is_dead(m)]
    live = [m for m in alive if not _cooling(m)]
    return live or alive or unique


# ---- display state (persisted downstream — NEVER read back into routing) ----
# Keyed by STAGE ("gatekeeper" / "editor"), not by model id. Keying by model id
# silently merged the two stages whenever they were configured to the same model
# — which made the editor's label claim a model that only the gatekeeper had
# called. A stage that issued no successful request must report effective=None.
_EFFECTIVE: dict[str, dict[str, int]] = {}   # stage -> {model answered: count}
_PRIMARY: dict[str, str] = {}                # stage -> resolved first choice
_EVENTS: list[str] = []                      # human-readable failover log
_ALERTS: list[str] = []                      # things a human must fix
_USAGE: dict[str, dict[str, int]] = {}       # stage -> token totals for the run
_MAX_EVENTS = 40
_display_lock = threading.Lock()


def _record_success(stage: str, answered: str) -> None:
    with _display_lock:
        _EFFECTIVE.setdefault(stage, {})
        _EFFECTIVE[stage][answered] = _EFFECTIVE[stage].get(answered, 0) + 1


# Token accounting. Both stages call the chain many times per run (one request
# per gatekeeper/editor batch), so these are RUN TOTALS, not per-request values.
#
# NOT A BILLING FIGURE. Only responses that actually came back are counted: a
# request that errored or timed out has no usage object to read, yet may still
# have been charged. Treat this as "what the published briefing cost", which is
# the question the dashboard is answering, and expect it to read slightly low on
# a night with retries.
_USAGE_FIELDS = ("prompt", "completion", "reasoning", "total")


def _first_int(obj, *names) -> int:
    """First present, non-None, int-able attribute among `names`. 0 if none."""
    for n in names:
        v = getattr(obj, n, None)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return 0


def _usage_of(resp) -> dict[str, int] | None:
    """Token counts from a provider response, or None when it reports none.

    Spans three shapes without branching on which provider we are in: OpenAI
    Responses (input/output_tokens), OpenAI chat.completions (prompt/completion
    _tokens) and Gemini (usage_metadata.*_token_count). Returning None rather
    than zeros keeps a provider that omits usage — or a test double that never
    had it — out of the totals instead of silently reporting 0.
    """
    u = getattr(resp, "usage", None) or getattr(resp, "usage_metadata", None)
    if u is None:
        return None
    prompt = _first_int(u, "input_tokens", "prompt_tokens", "prompt_token_count")
    completion = _first_int(u, "output_tokens", "completion_tokens",
                            "candidates_token_count")
    # Reasoning tokens are billed as output and are already inside the output
    # count on both providers; they are broken out only so the tooltip can show
    # how much of the spend was the model thinking rather than writing.
    details = (getattr(u, "output_tokens_details", None)
               or getattr(u, "completion_tokens_details", None))
    reasoning = (_first_int(details, "reasoning_tokens") if details is not None
                 else _first_int(u, "thoughts_token_count"))
    total = _first_int(u, "total_tokens", "total_token_count") or (prompt + completion)
    if not total:
        return None
    return {"prompt": prompt, "completion": completion,
            "reasoning": reasoning, "total": total}


def _record_usage(stage: str, usage: dict[str, int] | None) -> None:
    if not usage:
        return
    with _display_lock:
        bucket = _USAGE.setdefault(stage, {k: 0 for k in _USAGE_FIELDS})
        bucket["calls"] = bucket.get("calls", 0) + 1
        for k in _USAGE_FIELDS:
            bucket[k] += usage.get(k, 0)


def _event(msg: str) -> None:
    print(f"[llm] {msg}", flush=True)
    with _display_lock:
        if len(_EVENTS) < _MAX_EVENTS:
            _EVENTS.append(msg)


def _alert(msg: str) -> None:
    """A problem that will not fix itself. Loud, deduped, and carried to the UI."""
    with _display_lock:
        if msg in _ALERTS:
            return
        _ALERTS.append(msg)
    print(f"[llm] ACTION NEEDED — {msg}", flush=True)
    _event(f"ACTION NEEDED — {msg}")


def alerts() -> list[str]:
    with _display_lock:
        return list(_ALERTS)


def stage_report(stage: str, configured: str | None = None) -> dict:
    """What actually answered for `stage` — the value the UI labels output with.

    `effective` is the model that answered the most calls for this stage, which
    is the honest single-value answer when a run was split across a failover, and
    None when the stage never got a successful response. `primary` is the
    first-choice id after resolution, so `fell_back` stays False when a configured
    family name simply resolved to a concrete snapshot id.
    """
    with _display_lock:
        counts = dict(_EFFECTIVE.get(stage, {}))
        primary = _PRIMARY.get(stage, configured or stage)
        events = list(_EVENTS)
        alerted = list(_ALERTS)
        usage = dict(_USAGE.get(stage, {})) or None
    effective = max(counts, key=counts.get) if counts else None
    return {
        "configured": configured or stage,
        "primary": primary,
        "effective": effective,
        "counts": counts,
        "fell_back": bool(effective and effective != primary),
        "events": events,
        "alerts": alerted,
        # None (not zeros) when no response carried usage, so the dashboard can
        # tell "this run reported nothing" apart from "this run cost nothing".
        "usage": usage,
    }


def reset_state() -> None:
    """Clear routing + display state. For tests; a fresh process starts clean."""
    global _AVAILABLE_IDS
    _AVAILABLE_IDS = None
    _API_KEYS.clear()          # settings-supplied credentials are per-run too
    with _route_lock:
        _BUSY_UNTIL.clear()
        _DEAD_MODELS.clear()
        _DEAD_PROVIDERS.clear()
    with _display_lock:
        _EFFECTIVE.clear()
        _PRIMARY.clear()
        _EVENTS.clear()
        _ALERTS.clear()
        _USAGE.clear()
    _RESOLVED.clear()


# ---- error classification ---------------------------------------------------
BUSY, SOFT, UNUSABLE, FATAL = "busy", "soft", "unusable", "fatal"

_BUSY_STATUS = {408, 500, 502, 503, 504, 529}
_BUSY_STATUS_NAMES = {"unavailable", "internal", "deadline_exceeded", "aborted"}
_UNUSABLE_STATUS = {401, 403, 404}
_UNUSABLE_STATUS_NAMES = {"permission_denied", "unauthenticated", "not_found"}
# Matched against the lowercased message ONLY when no status code is available.
# Kept to distinctive multi-word phrases: an earlier version matched bare
# substrings like "rate", which also matches "generateContent" and so classified
# every unknown-model 404 as transient.
_BUSY_MARKERS = (
    "overloaded", "try again later", "temporarily unavailable",
    "service unavailable", "server unavailable", "backend error",
    "internal error", "deadline exceeded", "timed out", "timeout",
    "connection reset", "connection aborted", "connection refused",
    "connection error", "remote end closed", "server disconnected",
    "temporary failure",
)
# "This candidate is out of the game" — advance, but tell somebody.
_UNUSABLE_MARKERS = (
    "api key not valid", "api_key_invalid", "invalid api key",
    "incorrect api key", "no api key", "not set", "permission denied",
    "unauthenticated", "does not exist or you do not have access",
    "is not found for api version", "is not supported for",
    "insufficient_quota", "insufficient quota", "exceeded your current quota",
    "billing", "credit balance", "payment", "not installed",
)
_FATAL_MARKERS = ("invalid argument", "invalid_request_error", "unknown parameter")

# ---- rate limits vs. dead accounts ------------------------------------------
# THE DISTINCTION THIS SECTION EXISTS FOR, and why it is not a one-line check:
#
# Gemini's per-model quota error and OpenAI's out-of-credit error carry the SAME
# English prose — "You exceeded your current quota, please check your plan and
# billing details." Matching "billing" therefore used to classify a Gemini model
# running out of its own free-tier requests as a dead ACCOUNT, which marked the
# whole provider unusable and skipped every remaining Gemini model in the chain.
# Each Gemini model carries its own separate free-tier bucket, so those siblings
# would have answered. That single substring was the difference between a
# briefing and an empty morning.
#
# The reliable discriminators are the machine-readable parts, not the prose:
#   - a per-model/per-minute quota names its metric (Google's QuotaFailure
#     details, or an OpenAI rate_limit_exceeded code)
#   - a dead account names insufficient_quota / a credit balance
_RATE_LIMIT_MARKERS = (
    "quotafailure", "quotametric", "quota_metric", "quotaid", "quota_id",
    "rate limit", "rate-limit", "rate_limit", "ratelimit",
    "requests per minute", "requests per day", "tokens per minute",
    "per minute", "per day", "perday", "perminute", "permodel",
    "resource has been exhausted", "resource_exhausted", "check quota",
    "too many requests",
)
# Unambiguous "this account cannot pay", which DOES kill every model on the key.
_ACCOUNT_DEAD_MARKERS = (
    "insufficient_quota", "insufficient quota", "credit balance",
    "billing not active", "account is not active", "payment required",
    "spending limit", "hard limit",
)
# A quota that resets tomorrow, not in a minute. Retrying it is pure waste: the
# backoff burns ~2 minutes of a 60-minute budget to earn six more refusals.
_DAILY_LIMIT_MARKERS = (
    "per day", "perday", "requestsperday", "requests per day",
    "daily limit", "per-day", "quota exceeded for quota metric",
)


def _is_rate_limit(exc: Exception) -> bool:
    """True when the failure is a throughput cap, not a dead account."""
    return any(s in str(exc).lower() for s in _RATE_LIMIT_MARKERS)


def _is_daily_limit(exc: Exception) -> bool:
    """True when the cap resets on a day boundary — advance, do not retry."""
    msg = str(exc).lower()
    return (_is_rate_limit(exc)
            and any(s in msg for s in _DAILY_LIMIT_MARKERS)
            # "per minute" and "per day" can both appear in one violations list;
            # only treat it as daily when nothing says the limit is per-minute.
            and "per minute" not in msg and "perminute" not in msg)


def _status_code(exc: Exception) -> int | None:
    """HTTP status of an SDK error, from the attribute or the leading token.

    google-genai's APIError carries .code; the OpenAI SDK carries .status_code;
    requests-style errors carry .response.status_code. Gemini errors stringify as
    "<code> <STATUS>. {...}", so the leading-token regex is anchored and cannot
    match a number from the body.
    """
    for attr in ("status_code", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and 100 <= val <= 599:
            return val
    resp = getattr(exc, "response", None)
    val = getattr(resp, "status_code", None)
    if isinstance(val, int):
        return val
    m = re.match(r"\s*(\d{3})\b", str(exc))
    return int(m.group(1)) if m else None


def _status_name(exc: Exception) -> str:
    """Canonical status string (UNAVAILABLE, RESOURCE_EXHAUSTED, ...), if present."""
    val = getattr(exc, "status", None)
    if isinstance(val, str):
        return val.strip().lower()
    m = re.match(r"\s*\d{3}\s+([A-Z_]+)\b", str(exc))
    return m.group(1).lower() if m else ""


def classify(exc: Exception) -> str:
    """BUSY, SOFT, UNUSABLE (advance + alert), or FATAL (raise now)."""
    # Our own extraction failures — the request worked, the body did not.
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return SOFT
    if isinstance(exc, ProviderUnavailable):
        return UNUSABLE
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return BUSY

    msg = str(exc).lower()
    code = _status_code(exc)

    # A 429 is either "slow down" (recovers, and says nothing about the account)
    # or "you are out of credit" (never recovers). ORDER MATTERS HERE: the
    # rate-limit test runs FIRST because a Gemini per-model quota error repeats
    # OpenAI's out-of-credit prose verbatim, so the account check would claim it.
    if code == 429 or "resource_exhausted" in _status_name(exc):
        if _is_rate_limit(exc):
            return BUSY if RETRY_429 else FATAL
        if any(s in msg for s in _ACCOUNT_DEAD_MARKERS):
            return UNUSABLE
        return BUSY if RETRY_429 else FATAL
    if code in _BUSY_STATUS:
        return BUSY
    if code in _UNUSABLE_STATUS:
        return UNUSABLE
    if code is not None and 400 <= code < 500:
        return UNUSABLE if any(s in msg for s in _UNUSABLE_MARKERS) else FATAL

    name = _status_name(exc)
    if name in _BUSY_STATUS_NAMES:
        return BUSY
    if name in _UNUSABLE_STATUS_NAMES:
        return UNUSABLE

    if any(s in msg for s in _BUSY_MARKERS):
        return BUSY
    if any(s in msg for s in _UNUSABLE_MARKERS):
        return UNUSABLE
    if any(s in msg for s in _FATAL_MARKERS):
        return FATAL
    return FATAL          # unknown => surface it, never swallow it


def _provider_wide(exc: Exception) -> bool:
    """True when the cause kills every model from that provider, not just one.

    A missing key or a dead account takes the whole provider with it. A THROUGHPUT
    CAP DOES NOT: quotas are metered per model, so the next Gemini model on the
    same key has its own bucket and is very likely to answer. Returning True here
    for a rate limit is what used to skip the entire rest of a provider's chain.
    """
    if _is_rate_limit(exc):
        return False
    msg = str(exc).lower()
    if _status_code(exc) in (401, 403) or isinstance(exc, ProviderUnavailable):
        return True
    # NOTE: bare "billing" / "payment" are deliberately NOT here — they appear in
    # the advisory text of ordinary rate-limit errors. The guard above catches
    # most of those; these markers are the unambiguous ones.
    return any(s in msg for s in (
        "api key", "api_key", "unauthenticated", "permission denied",
        "not installed", "not set") + _ACCOUNT_DEAD_MARKERS)


def _reason(exc: Exception) -> str:
    """Short human tag for the log line: the status if known, else the type."""
    code = _status_code(exc)
    if code:
        return str(code)
    name = _status_name(exc)
    return name.upper() if name else type(exc).__name__


# ---- provider: OpenAI --------------------------------------------------------
_openai_client = None
_RESOLVED: dict[str, str] = {}       # configured id -> id that actually exists


def _openai_client_or_new():
    """Build the OpenAI client, or explain why this provider is unusable.

    Both failure modes here (SDK absent, key unset) are ProviderUnavailable
    rather than a crash: at 06:17 with nobody awake, "skip OpenAI and let Gemini
    write the briefing" beats "no briefing".
    """
    global _openai_client
    if _openai_client is None:
        try:
            import openai
        except ImportError as exc:
            raise ProviderUnavailable(
                "the `openai` package is not installed (pip install -r "
                "requirements.txt)") from exc
        key = api_key(OPENAI)
        if not key:
            raise ProviderUnavailable(
                "no OpenAI key — set one in the dashboard's Settings tab, or "
                "expose OPENAI_API_KEY to the workflow step")
        _openai_client = openai.OpenAI(api_key=key)
    return _openai_client


_AVAILABLE_IDS: list[str] | None = None


def _openai_ids() -> list[str]:
    """Every model id this key can see. Empty list if discovery is unavailable.

    ONE request per process, cached: the chain holds three OpenAI ids and both
    stages resolve them, which would otherwise be six identical round trips.
    """
    global _AVAILABLE_IDS
    if _AVAILABLE_IDS is not None:
        return _AVAILABLE_IDS
    try:
        listed = _openai_client_or_new().models.list()
    except ProviderUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort
        _event(f"could not list OpenAI models ({_reason(exc)}); "
               f"using the configured ids as written")
        _AVAILABLE_IDS = []
        return _AVAILABLE_IDS
    out = []
    for m in getattr(listed, "data", listed) or []:
        mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
        if mid:
            out.append(mid)
    _AVAILABLE_IDS = out
    return _AVAILABLE_IDS


def _resolve(model_id: str) -> str:
    """Correct a configured OpenAI id against what the account can actually see.

    Nicknames and family names are how people talk about models ("Sol"), but the
    API wants exact ids, and a wrong id is a 404 on every single call. One
    discovery request per process turns that into a logged substitution.

    Deliberately NOT called from the failure path — see complete_json.
    """
    provider, bare = split_model(model_id)
    if provider != OPENAI or not RESOLVE_MODELS:
        return model_id
    if model_id in _RESOLVED:
        return _RESOLVED[model_id]

    try:
        available = _openai_ids()
    except ProviderUnavailable:
        return model_id          # the call itself will report it properly
    if not available or bare in available:
        _RESOLVED[model_id] = model_id
        return model_id

    # Nearest match: prefer ids containing the distinctive last token ("sol"),
    # then the shortest (a base id beats a longer dated snapshot).
    token = bare.split("-")[-1].lower()
    hits = [m for m in available if token and token in m.lower()]
    if not hits:
        hits = [m for m in available if bare.lower() in m.lower()]
    picked = f"{OPENAI}:{sorted(hits, key=lambda m: (len(m), m))[0]}" if hits else model_id
    if picked != model_id:
        _event(f"'{bare}' is not an exact model id for this key — using "
               f"'{split_model(picked)[1]}' instead")
    else:
        _alert(f"no model matching '{bare}' is available to this OpenAI key; "
               f"the chain will skip it")
    _RESOLVED[model_id] = picked
    return picked


def _openai_text(model: str, system_prompt: str, user_payload: str,
                 max_tokens: int) -> str:
    """One forced-JSON completion. Returns the raw text the model produced.

    Tries the Responses API when the installed SDK has it (that is where the
    newest models live) and falls back to chat.completions if this model is only
    served there. Unknown-parameter 400s drop the offending argument and retry
    once, so a per-model API quirk costs one request instead of the whole run.
    """
    client = _openai_client_or_new()
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload}]
    # GPT-5.6 is a reasoning family: reasoning tokens are billed as output AND
    # come out of the same budget as the answer. Without headroom a long think
    # truncates the JSON, which reads as "unusable output" and burns the whole
    # chain on every batch. Effort is 'low' because both stages are extraction
    # and summarisation, not hard problems — the default 'medium' pays for
    # deliberation this workload does not need.
    budget = max_tokens + OPENAI_TOKEN_HEADROOM

    if hasattr(client, "responses"):
        kwargs = {
            "model": model,
            "input": messages,
            "max_output_tokens": budget,
            "text": {"format": {"type": "json_object"}},
            "reasoning": {"effort": OPENAI_REASONING_EFFORT},
        }
        try:
            return _openai_call(client.responses.create, kwargs, "output_text")
        except Exception as exc:  # noqa: BLE001
            if not _wrong_endpoint(exc):
                raise
            _event(f"{model}: the Responses API rejected this model "
                   f"({_reason(exc)}) — using chat.completions instead")

    kwargs = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": budget,
        "response_format": {"type": "json_object"},
    }
    return _openai_call(client.chat.completions, kwargs, "choices")   # (text, usage)


# A parameter the API rejects, and the equivalent to try instead (if any).
_PARAM_ALIAS = {"max_completion_tokens": "max_tokens",
                "max_output_tokens": "max_tokens"}


def _openai_call(target, kwargs: dict, shape: str) -> tuple[str, dict | None]:
    """Invoke the SDK, correcting one rejected parameter per attempt.

    Returns (text, usage). `usage` is None when the response carried no token
    counts — see _usage_of.
    """
    create = target if callable(target) else target.create
    for _ in range(3):                     # at most two parameter corrections
        try:
            resp = create(**kwargs)
            break
        except Exception as exc:  # noqa: BLE001
            bad = _unsupported_param(exc, kwargs)
            if not bad:
                raise
            value = kwargs.pop(bad)
            alias = _PARAM_ALIAS.get(bad)
            if alias and alias not in kwargs:
                kwargs[alias] = value
                _event(f"{kwargs.get('model')}: '{bad}' is not accepted here — "
                       f"retrying with '{alias}'")
            else:
                _event(f"{kwargs.get('model')}: '{bad}' is not accepted here — "
                       f"retrying without it")
    else:
        raise RuntimeError("exhausted parameter corrections")

    usage = _usage_of(resp)
    if shape == "output_text":
        text = getattr(resp, "output_text", None)
        if text:
            return text, usage
        # Older/newer response shapes: walk the content blocks.
        chunks = []
        for item in getattr(resp, "output", None) or []:
            for block in getattr(item, "content", None) or []:
                chunks.append(getattr(block, "text", "") or "")
        return "".join(chunks), usage
    choices = getattr(resp, "choices", None) or []
    text = (getattr(choices[0].message, "content", "") or "") if choices else ""
    return text, usage


def _unsupported_param(exc: Exception, kwargs: dict) -> str | None:
    """The kwarg an API 400 is complaining about, if it is one we can correct.

    Matches the name where the SDK puts it — quoted ("Unsupported parameter:
    'max_tokens'") — before falling back to a whole-word match, so a stray
    "text" inside an unrelated sentence cannot strip the JSON format directive.
    """
    if _status_code(exc) != 400:
        return None
    msg = str(exc).lower()
    if not any(s in msg for s in ("unsupported", "unrecognized", "unknown parameter",
                                  "not supported", "invalid_request_error")):
        return None
    for name in ("reasoning", "max_output_tokens", "max_completion_tokens",
                 "max_tokens", "response_format", "text"):
        if name not in kwargs:
            continue
        if re.search(rf"['\"`]{re.escape(name)}['\"`]", msg):
            return name
        if re.search(rf"\b{re.escape(name)}\b", msg):
            return name
    return None


def _wrong_endpoint(exc: Exception) -> bool:
    """True when a model exists but is not served by the endpoint we just used."""
    code = _status_code(exc)
    msg = str(exc).lower()
    if code == 404 and ("endpoint" in msg or "responses" in msg):
        return True
    return code in (400, 404) and "not supported" in msg and (
        "endpoint" in msg or "api" in msg)


# ---- provider: Anthropic -----------------------------------------------------
# Raw REST rather than the `anthropic` SDK, for the reason store.py gives for
# PostgREST: this is one POST with no streaming, no tools and no session, and
# `requests` is already a dependency. Adding an SDK would buy nothing here.
ANTHROPIC_BASE = os.environ.get("ANTHROPIC_BASE", "https://api.anthropic.com/v1")
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")


class _AnthropicError(RuntimeError):
    """Carries the HTTP status so classify() can route it like any SDK error."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _anthropic_text(model: str, system_prompt: str, user_payload: str,
                    max_tokens: int) -> tuple[str, dict | None]:
    """One Messages-API completion. Returns (text, usage).

    NOTE ON JSON: unlike OpenAI (response_format) and Gemini (response_mime_type),
    the Messages API has no forced-JSON switch. The obvious substitute — prefilling
    an assistant turn with "{" — is deliberately NOT used: the gatekeeper prompt
    may legitimately answer with a top-level ARRAY, and a "{" prefill would
    corrupt exactly that response. Both stages already carry their schema in the
    prompt, _extract_json tolerates prose and code fences around the payload, and
    a genuinely unparseable body classifies as SOFT and gets retried, then failed
    over. That is the same safety net every provider relies on for a bad body.
    """
    import requests   # local: keeps a missing dependency a ProviderUnavailable

    key = api_key(ANTHROPIC)
    if not key:
        raise ProviderUnavailable(
            "no Anthropic key — set one in the dashboard's Settings tab, or "
            "expose ANTHROPIC_API_KEY to the workflow step")

    try:
        resp = requests.post(
            f"{ANTHROPIC_BASE}/messages",
            headers={
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
            data=json.dumps({
                "model": model,
                # Required by /v1/messages, unlike the other two providers.
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_payload}],
            }).encode("utf-8"),
            timeout=180,
        )
    except requests.RequestException as exc:
        # Connection/timeout: BUSY by classify()'s ConnectionError/marker rules,
        # so this fails over rather than aborting the run.
        raise _AnthropicError(f"Anthropic request failed: {exc}") from exc

    if not resp.ok:
        # Surface Anthropic's own {type, message} body — a bare status is
        # indistinguishable between a dead key and an unknown model id, which
        # are UNUSABLE for opposite reasons (provider-wide vs. this model only).
        detail = resp.text.strip()[:400]
        raise _AnthropicError(f"{resp.status_code} Anthropic request failed: "
                              f"{detail}", resp.status_code)

    data = resp.json()
    text = "".join(
        block.get("text", "") or ""
        for block in (data.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )

    # Built by hand rather than through _usage_of: that helper walks ATTRIBUTES
    # of an SDK object, and this response is a plain dict.
    u = data.get("usage") or {}
    prompt = int(u.get("input_tokens") or 0)
    completion = int(u.get("output_tokens") or 0)
    total = prompt + completion
    usage = ({"prompt": prompt, "completion": completion,
              "reasoning": 0, "total": total} if total else None)
    return text, usage


# ---- provider: Gemini --------------------------------------------------------
_client = None          # kept at this name: tests inject a fake here
_genai_types = None


def _gemini_sdk():
    """Import google-genai lazily so a missing SDK degrades like a missing key."""
    global _genai_types
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ProviderUnavailable(
            "the `google-genai` package is not installed") from exc
    _genai_types = types
    return genai, types


def _client_or_new():
    global _client
    if _client is None:
        genai, _ = _gemini_sdk()
        key = api_key(GEMINI)
        if not key:
            raise ProviderUnavailable(
                "no Gemini key — set one in the dashboard's Settings tab, or "
                "expose GEMINI_API_KEY to the workflow step")
        _client = genai.Client(api_key=key)
    return _client


def _gemini_config(system_prompt: str, max_tokens: int):
    types = _genai_types or _gemini_sdk()[1]
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
    # No temperature/top_p/top_k: deprecated on Gemini 3.x.
    return types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        max_output_tokens=max_tokens,
        safety_settings=safety_settings,
    )


def _gemini_text(model: str, system_prompt: str, user_payload: str,
                 max_tokens: int) -> tuple[str, dict | None]:
    resp = _client_or_new().models.generate_content(
        model=model, contents=user_payload,
        config=_gemini_config(system_prompt, max_tokens),
    )
    return resp.text, _usage_of(resp)


# One adapter per provider, all with the same
# (model, system, user, max_tokens) -> (text, usage) signature. Declared after
# all three exist so the table holds functions rather than forward references.
_GENERATORS = {
    OPENAI: _openai_text,
    ANTHROPIC: _anthropic_text,
    GEMINI: _gemini_text,
}


# ---- request ----------------------------------------------------------------
def _extract_json(text: str | None):
    """Parse the first valid JSON object/array, ignoring trailing extra data."""
    if not text:
        raise ValueError("Model returned None or empty text.")

    text = _FENCE.sub("", text).strip()

    start = min([i for i in (text.find("["), text.find("{")) if i != -1], default=-1)
    if start == -1:
        raise ValueError("No JSON structure found in model output.")

    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text, idx=start)
    return obj


def _complete_one_model(system_prompt: str, user_payload: str, model: str,
                        max_tokens: int):
    """Call ONE model with forced-JSON output, retrying BUSY/SOFT failures.

    Returns (parsed, usage); usage is None when the response reported none.
    """
    provider, bare = split_model(model)
    generate = _GENERATORS.get(provider, _gemini_text)
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            _throttle(provider)
            text, usage = generate(bare, system_prompt, user_payload, max_tokens)
            return _extract_json(text), usage
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            # A DAILY cap will not clear before tomorrow, so the retry ladder
            # spends ~2 minutes of a 60-minute budget to collect six more
            # refusals. Advancing immediately leaves that time to a model that
            # can actually answer — which is the whole point of the chain.
            if _is_daily_limit(exc):
                _event(f"{model}: daily quota reached — not retrying, the cap "
                       f"resets tomorrow; moving on")
                raise
            if attempt < MAX_RETRIES - 1 and classify(exc) in (BUSY, SOFT):
                sleep = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))
                sleep += random.uniform(0, BACKOFF_BASE)   # jitter
                time.sleep(sleep)
                continue
            raise
    assert last_err is not None  # pragma: no cover
    raise last_err


def complete_json(system_prompt: str, user_payload: str, model: str,
                  max_tokens: int = 8000, fallback_models: list[str] | None = None,
                  stage: str | None = None):
    """
    Call the model chain with forced-JSON output and return the parsed object.

    Tries `model` first (with retries), then each entry of `fallback_models` in
    order, across providers. Every failover prints one line saying which model
    failed, why, and what is being tried next.

    Raises:
      ModelsBusyError — every candidate was BUSY. Temporary; nothing was written.
      FatalLlmError   — a malformed request, or nothing left that can serve it.
      the original error — when the last candidate failed on unusable OUTPUT.
    """
    key = stage or model                  # attribution bucket — see _EFFECTIVE
    candidates = _candidates(model, fallback_models)
    # The first CHOICE, not the first live candidate: if the preferred model is
    # skipped today, the label must still say the run fell back.
    primary = _resolve(model)
    with _display_lock:
        _PRIMARY.setdefault(key, primary)

    if not candidates:
        raise FatalLlmError(
            f"no usable model for '{model}': " + "; ".join(alerts() or ["chain empty"]))

    requested = [model] + [m for m in (fallback_models or []) if m]
    skipped = [m for m in requested if _resolve(m) not in candidates]
    if skipped:
        _event(f"skipping {', '.join(sorted(set(skipped)))} — unavailable or "
               f"still cooling down from a recent outage (this run only)")

    busy_only = True
    last_err: Exception | None = None
    for i, m in enumerate(candidates):
        # Re-checked every hop, not just when the list was built: a dead key
        # discovered on the first candidate rules out that provider's other
        # models immediately, instead of failing three more times to learn it.
        if _is_dead(m):
            _event(f"skipping {m} — already ruled out earlier in this run")
            continue
        try:
            result, usage = _complete_one_model(system_prompt, user_payload, m,
                                                max_tokens)
            _record_success(key, m)            # DISPLAY ONLY — never routing
            _record_usage(key, usage)          # DISPLAY ONLY — never routing
            if m != primary:
                _event(f"{m} answered this request (first choice was {primary})")
            return result
        except Exception as exc:               # noqa: BLE001
            kind = classify(exc)
            last_err = exc
            if kind == FATAL:
                # The same request would fail identically everywhere; walking the
                # chain would only hide it behind N more failures.
                raise FatalLlmError(
                    f"{m} rejected the request ({_reason(exc)}): {exc}") from exc

            if kind == BUSY:
                _mark_busy(m)                  # ROUTING, session-only
            else:
                busy_only = False
            if kind == UNUSABLE:
                wide = _provider_wide(exc)
                _mark_dead(m, wide, str(exc))
                scope = (f"every {split_model(m)[0]} model is" if wide
                         else f"{m} is")
                _alert(f"{scope} unusable this run ({_reason(exc)}): {exc}")

            nxt = next((c for c in candidates[i + 1:] if not _is_dead(c)), None)
            if nxt:
                what = {BUSY: f"is overloaded ({_reason(exc)})",
                        UNUSABLE: f"cannot be used ({_reason(exc)})"}.get(
                            kind, f"returned unusable output ({_reason(exc)})")
                _event(f"{m} {what} — trying the next: {nxt}")

    if busy_only:
        # NOTE: no model-discovery call here, deliberately. The ids were already
        # resolved before the first request, and an outage is not a discovery
        # problem — it would be a wasted round trip against an API that is
        # already failing.
        raise ModelsBusyError(candidates, _reason(last_err) if last_err else "")
    if alerts():
        raise FatalLlmError(
            "no model in the chain could serve this request. "
            + " | ".join(alerts())) from last_err
    assert last_err is not None  # pragma: no cover
    raise last_err
