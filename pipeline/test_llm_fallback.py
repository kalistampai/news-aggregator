"""
Model-chain simulation — no network, no API key, no pytest required.

    python test_llm_fallback.py          # from pipeline/

Drives llm.complete_json against FAKE OpenAI and Gemini clients whose responses
are scripted per model, and asserts the behaviours that are impossible to verify
from a live run without deliberately breaking production.

The pipeline runs unattended at 06:17 Pacific, so the governing question in every
case below is: *does a briefing still get written, and is the reason visible?*

  1. best model first; a 503 on it fails over and the run recovers
  2. an OpenAI outage/dead key/dry account walks the chain to Gemini, records an
     ACTION NEEDED alert, and still produces output
  3. a malformed request is raised immediately — it fails identically everywhere
  4. 503 on EVERY candidate raises the distinct "temporary, nothing changed"
     error and issues no model-discovery call
"""
from __future__ import annotations
import importlib.util
import io
import json
import os
import sys
import types as pytypes
from contextlib import redirect_stdout

# ---- stub the Gemini SDK when it isn't installed ----------------------------
# llm.py imports it lazily, and the tests never reach a real client, so a stub
# is enough for the Gemini branch of the chain.
if importlib.util.find_spec("google") is None:
    class _Names:                      # types.HarmCategory.WHATEVER -> "WHATEVER"
        def __getattr__(self, name): return name

    google_mod = pytypes.ModuleType("google")
    genai_mod = pytypes.ModuleType("google.genai")
    types_mod = pytypes.ModuleType("google.genai.types")
    types_mod.HarmCategory = _Names()
    types_mod.HarmBlockThreshold = _Names()
    types_mod.SafetySetting = lambda **kw: kw
    types_mod.GenerateContentConfig = lambda **kw: kw
    genai_mod.types = types_mod
    genai_mod.Client = lambda **kw: (_ for _ in ()).throw(
        AssertionError("the simulation must never build a real client"))
    google_mod.genai = genai_mod
    sys.modules["google"] = google_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = types_mod

os.environ.setdefault("LLM_MIN_INTERVAL", "0")     # no throttle sleeps in tests
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

import llm  # noqa: E402

llm.MAX_RETRIES = 3        # keep the scripts short
llm.BACKOFF_BASE = 0       # and instant
llm.BACKOFF_CAP = 0

SOL = "openai:gpt-5.6-sol"
TERRA = "openai:gpt-5.6-terra"
LUNA = "openai:gpt-5.6-luna"
FLOOR = "gemini:gemini-3.1-flash-lite"
CHAIN = (TERRA, LUNA, FLOOR)
GOOD_JSON = '{"categories": {"AI News": [{"title": "ok"}]}}'
WANT = json.loads(GOOD_JSON)


# ---- fake errors ------------------------------------------------------------
class OpenAIError(Exception):
    """Shaped like openai.APIStatusError: .status_code and 'Error code: NNN - ...'."""

    def __init__(self, status_code: int, message: str, kind: str = "error"):
        self.status_code = status_code
        body = {"error": {"message": message, "type": kind}}
        super().__init__(f"Error code: {status_code} - {body}")


class GeminiError(Exception):
    """Shaped like google.genai.errors.APIError: .code, .status, matching str()."""

    def __init__(self, code: int, status: str, message: str):
        self.code = code
        self.status = status
        body = {"error": {"code": code, "message": message, "status": status}}
        super().__init__(f"{code} {status}. {body}")


def busy_openai(msg="The model is overloaded. Please try again later."):
    return OpenAIError(503, msg, "server_error")


def busy_gemini(code=503, status="UNAVAILABLE",
                msg="The model is overloaded. Please try again later."):
    return GeminiError(code, status, msg)


# ---- fake clients -----------------------------------------------------------
def _dispense(script, model, calls):
    calls.append(model)
    item = script.get(model, OpenAIError(
        404, f"The model `{model}` does not exist or you do not have access to it."))
    if isinstance(item, list):
        item = item.pop(0) if len(item) > 1 else item[0]
    if isinstance(item, BaseException):
        raise item
    return item


class _Endpoint:
    """responses.create / chat.completions.create over the same script."""

    def __init__(self, parent, shape):
        self.parent, self.shape = parent, shape

    def create(self, **kwargs):
        self.parent.kwargs.append(kwargs)
        model = kwargs.get("model")
        key = f"{self.shape}:{model}"
        script = self.parent.script
        text = _dispense(script if key in script else script,
                         key if key in script else model, self.parent.calls)
        if self.shape == "responses":
            resp = pytypes.SimpleNamespace(output_text=text)
            if self.parent.usage:
                p, c, r = self.parent.usage
                resp.usage = pytypes.SimpleNamespace(
                    input_tokens=p, output_tokens=c, total_tokens=p + c,
                    output_tokens_details=pytypes.SimpleNamespace(reasoning_tokens=r))
            return resp
        msg = pytypes.SimpleNamespace(content=text)
        resp = pytypes.SimpleNamespace(choices=[pytypes.SimpleNamespace(message=msg)])
        if self.parent.usage:
            p, c, r = self.parent.usage
            resp.usage = pytypes.SimpleNamespace(
                prompt_tokens=p, completion_tokens=c, total_tokens=p + c,
                completion_tokens_details=pytypes.SimpleNamespace(reasoning_tokens=r))
        return resp


class _OpenAIModels:
    def __init__(self, parent): self.parent = parent

    def list(self):
        self.parent.list_calls += 1
        if isinstance(self.parent.ids, BaseException):
            raise self.parent.ids
        return pytypes.SimpleNamespace(
            data=[pytypes.SimpleNamespace(id=i) for i in (self.parent.ids or [])])


class FakeOpenAI:
    """`script` maps a bare model id (or 'responses:<id>' / 'chat:<id>') to a
    response string, an exception, or a list consumed in order."""

    def __init__(self, script, ids=None, no_responses_api=False, usage=None):
        self.script = script
        self.ids = ids
        self.usage = usage          # (prompt, completion, reasoning) or None
        self.calls: list[str] = []
        self.kwargs: list[dict] = []
        self.list_calls = 0
        self.chat = pytypes.SimpleNamespace(
            completions=_Endpoint(self, "chat"))
        self.models = _OpenAIModels(self)
        if not no_responses_api:
            self.responses = _Endpoint(self, "responses")


class _GeminiModels:
    def __init__(self, parent): self.parent = parent

    def generate_content(self, model, contents, config):
        resp = pytypes.SimpleNamespace(
            text=_dispense(self.parent.script, model, self.parent.calls))
        if self.parent.usage:
            p, c, r = self.parent.usage
            resp.usage_metadata = pytypes.SimpleNamespace(
                prompt_token_count=p, candidates_token_count=c,
                total_token_count=p + c, thoughts_token_count=r)
        return resp

    def list(self, *a, **kw):
        self.parent.list_calls += 1
        return []


class FakeGemini:
    def __init__(self, script, usage=None):
        self.script = script
        self.usage = usage          # (prompt, completion, reasoning) or None
        self.calls: list[str] = []
        self.list_calls = 0
        self.models = _GeminiModels(self)


def scripted(openai_script=None, gemini_script=None, ids=None,
             no_responses_api=False, usage=None):
    """Install fake clients for both providers and clear all per-process state."""
    llm.reset_state()
    oa = FakeOpenAI(openai_script or {}, ids=ids,
                    no_responses_api=no_responses_api, usage=usage)
    gm = FakeGemini(gemini_script or {}, usage=usage)
    llm._openai_client = oa
    llm._client = gm
    return oa, gm


def run(model=SOL, fallbacks=CHAIN, **kw):
    """complete_json against scripted clients; returns (result|exc, log, oa, gm)."""
    oa, gm = scripted(**kw)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            out = llm.complete_json("sys", "payload", model,
                                    fallback_models=list(fallbacks))
    except Exception as exc:  # noqa: BLE001
        out = exc
    return out, buf.getvalue(), oa, gm


def bare(model_id):
    return llm.split_model(model_id)[1]


# ---- 1. best model first, failover on 503 ----------------------------------
def test_best_model_answers_first():
    out, log, oa, gm = run(openai_script={bare(SOL): GOOD_JSON})
    assert out == WANT, out
    assert oa.calls == [bare(SOL)], oa.calls
    assert not gm.calls, "Gemini was called while OpenAI was healthy"
    rep = llm.stage_report(SOL)
    assert rep["effective"] == SOL, rep
    assert rep["fell_back"] is False, rep
    assert rep["alerts"] == [], rep


def test_503_on_best_model_falls_over_to_next():
    out, log, oa, gm = run(openai_script={bare(SOL): busy_openai(),
                                          bare(TERRA): GOOD_JSON})
    assert out == WANT, out
    assert oa.calls.count(bare(SOL)) == llm.MAX_RETRIES, oa.calls
    assert f"{SOL} is overloaded (503) — trying the next: {TERRA}" in log, log
    rep = llm.stage_report(SOL)
    assert rep["effective"] == TERRA, rep
    assert rep["fell_back"] is True, rep


# ---- 2. the morning report survives an OpenAI-wide problem ------------------
def test_dead_openai_key_falls_through_to_gemini():
    out, log, oa, gm = run(
        openai_script={bare(SOL): OpenAIError(401, "Incorrect API key provided.")},
        gemini_script={bare(FLOOR): GOOD_JSON})
    assert out == WANT, f"no briefing was produced: {out!r}"
    # a key problem kills every OpenAI model, so terra/luna are not tried
    assert oa.calls == [bare(SOL)], oa.calls
    assert gm.calls == [bare(FLOOR)], gm.calls
    alerts = llm.alerts()
    assert alerts and "openai" in alerts[0].lower(), alerts
    assert "ACTION NEEDED" in log, log
    assert llm.stage_report(SOL)["effective"] == FLOOR


def test_out_of_credit_falls_through_to_gemini():
    err = OpenAIError(429, "You exceeded your current quota, please check your "
                           "plan and billing details.", "insufficient_quota")
    out, log, oa, gm = run(openai_script={bare(SOL): err},
                           gemini_script={bare(FLOOR): GOOD_JSON})
    assert out == WANT, f"no briefing was produced: {out!r}"
    # never retried: a dry account does not recover by waiting
    assert oa.calls == [bare(SOL)], oa.calls
    assert any("quota" in a.lower() or "billing" in a.lower() for a in llm.alerts()), \
        llm.alerts()


def test_unknown_model_id_skips_only_that_model():
    out, log, oa, gm = run(
        openai_script={bare(SOL): OpenAIError(
            404, f"The model `{bare(SOL)}` does not exist or you do not have "
                 f"access to it."),
            bare(TERRA): GOOD_JSON},
        ids=[bare(SOL), bare(TERRA), bare(LUNA)])   # discovery says it exists
    assert out == WANT, out
    assert oa.calls == [bare(SOL), bare(TERRA)], oa.calls   # one try each
    assert any(bare(SOL) in a for a in llm.alerts()), llm.alerts()
    assert not gm.calls, "fell all the way to Gemini when terra could answer"


def test_rate_limit_backs_off_and_fails_over_by_default():
    # LLM_RETRY_429 defaults ON for unattended runs: a quota spike must not end
    # the night. The alternative (fatal) is asserted in the next test.
    assert llm.RETRY_429 is True, "the unattended default changed"
    out, log, oa, gm = run(
        openai_script={bare(SOL): OpenAIError(429, "Rate limit reached for "
                                                   "requests.", "rate_limit_error"),
                       bare(TERRA): GOOD_JSON})
    assert out == WANT, out
    assert oa.calls.count(bare(SOL)) == llm.MAX_RETRIES, oa.calls
    assert llm.alerts() == [], "a transient 429 must not raise an ACTION NEEDED"


def test_rate_limit_is_fatal_when_retry_429_is_off():
    llm.RETRY_429 = False
    try:
        out, log, oa, gm = run(
            openai_script={bare(SOL): OpenAIError(429, "Rate limit reached.",
                                                  "rate_limit_error"),
                           bare(TERRA): GOOD_JSON})
        assert isinstance(out, llm.FatalLlmError), f"got {type(out).__name__}: {out}"
        assert oa.calls == [bare(SOL)], oa.calls
    finally:
        llm.RETRY_429 = True


# ---- 3. genuine request errors still stop the run --------------------------
def test_malformed_request_raises_immediately():
    out, log, oa, gm = run(
        openai_script={bare(SOL): OpenAIError(
            400, "Invalid schema for response_format: 'categories' is required.",
            "invalid_request_error"),
            bare(TERRA): GOOD_JSON})
    assert isinstance(out, llm.FatalLlmError), f"got {type(out).__name__}: {out}"
    assert oa.calls == [bare(SOL)], oa.calls          # no retry, no failover
    assert not gm.calls, gm.calls
    assert not isinstance(out, llm.ModelsBusyError)


def test_gemini_unknown_model_id_is_not_mistaken_for_transient():
    # REGRESSION: an early substring classifier matched "rate" inside
    # "generateContent", so this 404 burned every retry and fallback.
    out, log, oa, gm = run(
        model=FLOOR, fallbacks=(),
        gemini_script={bare(FLOOR): GeminiError(
            404, "NOT_FOUND", "models/x is not found for API version v1beta, or "
                              "is not supported for generateContent.")})
    assert isinstance(out, llm.FatalLlmError), f"got {type(out).__name__}: {out}"
    assert gm.calls == [bare(FLOOR)], gm.calls


# ---- 4. everything busy ----------------------------------------------------
def test_all_models_busy_is_temporary_and_skips_discovery():
    out, log, oa, gm = run(
        openai_script={bare(SOL): busy_openai(), bare(TERRA): busy_openai(),
                       bare(LUNA): busy_openai()},
        gemini_script={bare(FLOOR): busy_gemini(500, "INTERNAL", "Internal error.")})
    assert isinstance(out, llm.ModelsBusyError), f"got {type(out).__name__}: {out}"
    text = str(out).lower()
    assert "temporary" in text, out
    assert "no data was changed" in text, out
    for wrong in ("api key", "check your", "invalid", "quota"):
        assert wrong not in text, f"all-busy message blames '{wrong}': {out}"
    # discovery is resolution-time only: at most one for the whole run, never
    # once per failure, and never from the failure path itself
    assert oa.list_calls <= 1, f"{oa.list_calls} discovery calls"
    assert gm.list_calls == 0, "Gemini model discovery called during an outage"
    assert llm.alerts() == [], "an outage is not an ACTION NEEDED"


def test_busy_everywhere_still_reports_each_hop():
    out, log, oa, gm = run(
        openai_script={bare(SOL): busy_openai(), bare(TERRA): busy_openai(),
                       bare(LUNA): busy_openai()},
        gemini_script={bare(FLOOR): busy_gemini()})
    for a, b in ((SOL, TERRA), (TERRA, LUNA), (LUNA, FLOOR)):
        assert f"{a} is overloaded (503) — trying the next: {b}" in log, log


# ---- model id resolution ---------------------------------------------------
def test_resolution_corrects_a_near_miss_id():
    real = "gpt-5.6-sol-2026-07-14"
    out, log, oa, gm = run(
        openai_script={real: GOOD_JSON},
        ids=["gpt-4o", real, "gpt-5.6-terra"])
    assert out == WANT, out
    assert oa.calls == [real], oa.calls
    assert "using 'gpt-5.6-sol-2026-07-14' instead" in log, log
    rep = llm.stage_report(SOL)
    # a resolved id is NOT a failover — the first choice still answered
    assert rep["effective"] == f"openai:{real}", rep
    assert rep["fell_back"] is False, rep
    assert rep["configured"] == SOL, rep


def test_resolution_failure_uses_configured_ids():
    out, log, oa, gm = run(openai_script={bare(SOL): GOOD_JSON},
                           ids=OpenAIError(500, "server error"))
    assert out == WANT, out
    assert oa.calls == [bare(SOL)], oa.calls
    assert "using the configured ids as written" in log, log


def test_missing_model_family_alerts_and_moves_on():
    out, log, oa, gm = run(
        openai_script={},                      # nothing answers on OpenAI
        gemini_script={bare(FLOOR): GOOD_JSON},
        ids=["gpt-4o", "gpt-4o-mini"])         # none of sol/terra/luna exist
    assert out == WANT, f"no briefing was produced: {out!r}"
    assert any("no model matching" in a for a in llm.alerts()), llm.alerts()
    assert gm.calls == [bare(FLOOR)], gm.calls


# ---- API-surface adaptation -------------------------------------------------
def test_unsupported_parameter_is_corrected_once():
    out, log, oa, gm = run(openai_script={bare(SOL): [
        OpenAIError(400, "Unsupported parameter: 'max_output_tokens' is not "
                         "supported with this model.", "invalid_request_error"),
        GOOD_JSON]})
    assert out == WANT, out
    assert len(oa.calls) == 2, oa.calls
    assert "max_output_tokens" not in oa.kwargs[-1], oa.kwargs[-1]
    # the budget carries over to the alias, headroom included
    assert oa.kwargs[-1].get("max_tokens") == 8000 + llm.OPENAI_TOKEN_HEADROOM, \
        oa.kwargs[-1]


def test_reasoning_budget_leaves_room_for_the_answer():
    out, log, oa, gm = run(openai_script={bare(SOL): GOOD_JSON})
    assert out == WANT, out
    sent = oa.kwargs[-1]
    # reasoning tokens share the output budget and are billed, so the request
    # must ask for more than the caller's answer budget, at low effort
    assert sent["reasoning"] == {"effort": llm.OPENAI_REASONING_EFFORT}, sent
    assert sent["max_output_tokens"] == 8000 + llm.OPENAI_TOKEN_HEADROOM, sent
    assert sent["text"] == {"format": {"type": "json_object"}}, sent


def test_reasoning_param_is_dropped_if_rejected():
    out, log, oa, gm = run(openai_script={bare(SOL): [
        OpenAIError(400, "Unknown parameter: 'reasoning'.", "invalid_request_error"),
        GOOD_JSON]})
    assert out == WANT, out
    assert "reasoning" not in oa.kwargs[-1], oa.kwargs[-1]


def test_responses_api_falls_back_to_chat_completions():
    out, log, oa, gm = run(openai_script={
        f"responses:{bare(SOL)}": OpenAIError(
            404, "This model is not supported by the v1/responses endpoint."),
        f"chat:{bare(SOL)}": GOOD_JSON})
    assert out == WANT, out
    assert "using chat.completions instead" in log, log


def test_sdk_without_responses_api_uses_chat_completions():
    out, log, oa, gm = run(openai_script={bare(SOL): GOOD_JSON},
                           no_responses_api=True)
    assert out == WANT, out
    assert "messages" in oa.kwargs[-1], oa.kwargs[-1]


# ---- output problems are not outages ---------------------------------------
def test_unusable_output_is_not_reported_as_an_outage():
    out, log, oa, gm = run(
        openai_script={bare(SOL): "not json", bare(TERRA): "still not json",
                       bare(LUNA): "nope"},
        gemini_script={bare(FLOOR): "also not json"})
    assert not isinstance(out, llm.ModelsBusyError), out
    assert isinstance(out, ValueError), f"got {type(out).__name__}: {out}"
    assert "returned unusable output" in log, log


def test_soft_failure_then_fallback_succeeds():
    out, log, oa, gm = run(openai_script={bare(SOL): "garbage",
                                          bare(TERRA): GOOD_JSON})
    assert out == WANT, out
    assert llm.stage_report(SOL)["effective"] == TERRA


# ---- routing state is session-only -----------------------------------------
def test_busy_skip_is_session_only():
    llm.LLM_BUSY_COOLDOWN = 300
    try:
        oa, gm = scripted(openai_script={bare(SOL): busy_openai(),
                                         bare(TERRA): GOOD_JSON})
        with redirect_stdout(io.StringIO()):
            llm.complete_json("sys", "p", SOL, fallback_models=[TERRA])
        assert bare(SOL) in oa.calls

        # Second call in the SAME session: the busy model is skipped, not
        # re-hammered for another six retries.
        oa.calls.clear()
        with redirect_stdout(io.StringIO()) as buf:
            llm.complete_json("sys", "p", SOL, fallback_models=[TERRA])
        assert bare(SOL) not in oa.calls, oa.calls
        assert "cooling down" in buf.getvalue()

        # A NEW process (reset_state) tries the preferred model FIRST again:
        # the skip is routing state and is never persisted.
        oa, gm = scripted(openai_script={bare(SOL): GOOD_JSON,
                                         bare(TERRA): GOOD_JSON})
        with redirect_stdout(io.StringIO()):
            llm.complete_json("sys", "p", SOL, fallback_models=[TERRA])
        assert oa.calls[0] == bare(SOL), oa.calls
        assert llm.stage_report(SOL)["effective"] == SOL
    finally:
        llm.LLM_BUSY_COOLDOWN = 120


def test_cooldown_never_empties_the_candidate_list():
    llm.LLM_BUSY_COOLDOWN = 300
    try:
        oa, gm = scripted(openai_script={bare(SOL): busy_openai(),
                                         bare(TERRA): busy_openai()})
        with redirect_stdout(io.StringIO()):
            try:
                llm.complete_json("sys", "p", SOL, fallback_models=[TERRA])
            except llm.ModelsBusyError:
                pass
        # Both are cooling now; the next call must still try something.
        oa.script = {bare(SOL): GOOD_JSON, bare(TERRA): GOOD_JSON}
        oa.calls.clear()
        with redirect_stdout(io.StringIO()):
            out = llm.complete_json("sys", "p", SOL, fallback_models=[TERRA])
        assert out == WANT, out
        assert oa.calls, "no call was made while every model was cooling"
    finally:
        llm.LLM_BUSY_COOLDOWN = 120


# ---- attribution is per STAGE, not per model id -----------------------------
def test_stages_sharing_a_model_do_not_borrow_each_others_credit():
    # REGRESSION (2026-08-02): both stages were configured to gpt-5.6-sol, the
    # gatekeeper made 5 calls, the editor made none — and the dashboard labelled
    # the empty briefing "via openai:gpt-5.6-sol" because the record was keyed by
    # model id. A stage that never called anything must report nothing.
    scripted(openai_script={bare(SOL): GOOD_JSON})
    with redirect_stdout(io.StringIO()):
        llm.complete_json("sys", "p", SOL, fallback_models=[], stage="gatekeeper")

    gate = llm.stage_report("gatekeeper", SOL)
    editor = llm.stage_report("editor", SOL)
    assert gate["effective"] == SOL, gate
    assert gate["counts"] == {SOL: 1}, gate
    assert editor["effective"] is None, f"editor claimed credit it never earned: {editor}"
    assert editor["counts"] == {}, editor


def test_each_stage_reports_its_own_model():
    # Cooldown off so the second call re-tries the first choice: this test is
    # about attribution keys, not routing.
    llm.LLM_BUSY_COOLDOWN = 0
    try:
        oa, _ = scripted(openai_script={bare(SOL): busy_openai(),
                                        bare(TERRA): GOOD_JSON})
        with redirect_stdout(io.StringIO()):
            llm.complete_json("sys", "p", SOL, fallback_models=[TERRA],
                              stage="gatekeeper")
        oa.script = {bare(SOL): GOOD_JSON, bare(TERRA): GOOD_JSON}
        with redirect_stdout(io.StringIO()):
            llm.complete_json("sys", "p", SOL, fallback_models=[TERRA],
                              stage="editor")
        assert llm.stage_report("gatekeeper", SOL)["effective"] == TERRA
        assert llm.stage_report("editor", SOL)["effective"] == SOL
    finally:
        llm.LLM_BUSY_COOLDOWN = 120


# ---- token accounting -------------------------------------------------------
def test_token_usage_accumulates_across_calls():
    # Both stages call the chain once per batch, so the reported figure has to be
    # the run total, not whatever the last request happened to cost.
    scripted(openai_script={bare(SOL): [GOOD_JSON, GOOD_JSON, GOOD_JSON]},
             usage=(100, 40, 10))
    with redirect_stdout(io.StringIO()):
        for _ in range(3):
            llm.complete_json("sys", "p", SOL, fallback_models=[], stage="editor")

    u = llm.stage_report("editor", SOL)["usage"]
    assert u["calls"] == 3, u
    assert u["prompt"] == 300 and u["completion"] == 120, u
    assert u["total"] == 420, u
    assert u["reasoning"] == 30, u


def test_token_usage_is_attributed_per_stage():
    # Same bug class as the model label: the editor must not inherit tokens the
    # gatekeeper spent, even when both are configured to the same model.
    scripted(openai_script={bare(SOL): [GOOD_JSON, GOOD_JSON]}, usage=(50, 25, 0))
    with redirect_stdout(io.StringIO()):
        llm.complete_json("sys", "p", SOL, fallback_models=[], stage="gatekeeper")
        llm.complete_json("sys", "p", SOL, fallback_models=[], stage="editor")

    assert llm.stage_report("gatekeeper", SOL)["usage"]["total"] == 75
    assert llm.stage_report("editor", SOL)["usage"]["total"] == 75
    assert llm.stage_report("nobody", SOL)["usage"] is None


def test_gemini_usage_is_read_from_its_own_field_name():
    # Gemini reports usage_metadata.*_token_count, not usage.*_tokens. Reading
    # only the OpenAI shape would silently report zero on every failover night.
    scripted(openai_script={bare(SOL): busy_openai()},
             gemini_script={bare(FLOOR): GOOD_JSON}, usage=(200, 60, 20))
    with redirect_stdout(io.StringIO()):
        llm.complete_json("sys", "p", SOL, fallback_models=[FLOOR], stage="editor")

    u = llm.stage_report("editor", SOL)["usage"]
    assert u["total"] == 260, u
    assert u["reasoning"] == 20, u


def test_a_response_without_usage_reports_none_not_zero():
    # "The provider told us nothing" and "this run cost nothing" are different
    # claims; the dashboard shows the count only for the first.
    scripted(openai_script={bare(SOL): GOOD_JSON})       # no usage on the fake
    with redirect_stdout(io.StringIO()):
        llm.complete_json("sys", "p", SOL, fallback_models=[], stage="editor")

    rep = llm.stage_report("editor", SOL)
    assert rep["effective"] == SOL, "the call still has to be credited"
    assert rep["usage"] is None, rep["usage"]


# ---- runner -----------------------------------------------------------------
# ---- 5. same-provider failover: one model's cap is not the provider's -------
# Quotas are metered PER MODEL. The single most likely overnight failure is one
# model exhausting its own bucket, and the fix that costs nothing is the next
# model on the SAME key. These cases exist because that used not to happen: a
# Gemini quota error repeats OpenAI's out-of-credit prose word for word ("check
# your plan and billing details"), the substring "billing" marked the whole
# provider dead, and every remaining Gemini model was skipped for the run.
GEM_MAIN = "gemini:gemini-3.5-flash"

_QUOTA_DETAILS = (
    "[{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': "
    "[{'quotaMetric': 'generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests', 'quotaId': '%s'}]}]")


def gemini_quota(per="Day"):
    """A real-shaped Gemini 429 for a per-model free-tier cap."""
    return GeminiError(
        429, "RESOURCE_EXHAUSTED",
        "You exceeded your current quota, please check your plan and billing "
        "details. For more information on this error, head on to: "
        "https://ai.google.dev/gemini-api/docs/rate-limits. Details: "
        + _QUOTA_DETAILS % f"GenerateRequestsPer{per}PerProjectPerModel-FreeTier")


def test_gemini_model_quota_falls_over_to_the_next_gemini_model():
    """THE headline case: same key, next model, briefing still ships."""
    out, log, oa, gm = run(
        model=GEM_MAIN, fallbacks=(FLOOR,),
        gemini_script={bare(GEM_MAIN): gemini_quota(), bare(FLOOR): GOOD_JSON})
    assert out == WANT, f"no briefing was produced: {out!r}"
    assert bare(FLOOR) in gm.calls, gm.calls
    assert not oa.calls, "left the provider entirely when a sibling could answer"
    assert llm.stage_report(GEM_MAIN)["effective"] == FLOOR


def test_a_model_quota_does_not_kill_the_provider():
    err = gemini_quota()
    assert llm._provider_wide(err) is False, \
        "a per-model cap was treated as a dead account — the rest of the " \
        "provider's chain would be skipped"
    assert llm.classify(err) == llm.BUSY, llm.classify(err)


def test_daily_quota_is_not_retried():
    """A cap that resets tomorrow must cost ONE request, not six.

    The retry ladder would burn ~2 minutes of a 60-minute budget to collect six
    more refusals it already knows are coming.
    """
    out, log, oa, gm = run(
        model=GEM_MAIN, fallbacks=(FLOOR,),
        gemini_script={bare(GEM_MAIN): gemini_quota("Day"), bare(FLOOR): GOOD_JSON})
    assert out == WANT, out
    assert gm.calls.count(bare(GEM_MAIN)) == 1, \
        f"retried a daily cap {gm.calls.count(bare(GEM_MAIN))} times: {gm.calls}"
    assert "daily quota" in log, log


def test_per_minute_quota_is_retried_before_moving_on():
    """The mirror image: a per-minute cap DOES clear, so waiting is correct."""
    out, log, oa, gm = run(
        model=GEM_MAIN, fallbacks=(FLOOR,),
        gemini_script={bare(GEM_MAIN): gemini_quota("Minute"),
                       bare(FLOOR): GOOD_JSON})
    assert out == WANT, out
    assert gm.calls.count(bare(GEM_MAIN)) == llm.MAX_RETRIES, gm.calls


def test_out_of_credit_still_kills_the_whole_provider():
    """Guard against over-correcting: a dry ACCOUNT must still skip its models.

    Retrying sibling models on a key that cannot pay just spends the run's time
    budget collecting the same refusal.
    """
    err = OpenAIError(429, "You exceeded your current quota, please check your "
                           "plan and billing details.", "insufficient_quota")
    assert llm._provider_wide(err) is True, \
        "an unpayable account was treated as a per-model cap"
    out, log, oa, gm = run(
        openai_script={bare(SOL): err}, gemini_script={bare(FLOOR): GOOD_JSON})
    assert out == WANT, out
    assert oa.calls == [bare(SOL)], f"tried more OpenAI models on a dry key: {oa.calls}"


def test_a_bad_key_still_kills_the_whole_provider():
    err = OpenAIError(401, "Incorrect API key provided.")
    assert llm._provider_wide(err) is True, err


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for t in tests:
        llm.reset_state()
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed.append(t.__name__)
            print(f"  FAIL  {t.__name__}\n        {e}")
        except Exception as e:  # noqa: BLE001
            failed.append(t.__name__)
            print(f"  ERROR {t.__name__}\n        {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed"
          + (f" — FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
