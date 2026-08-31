"""
Settings-from-Supabase simulation — no network, no key, no pytest required.

    python test_settings.py          # from pipeline/

The dashboard's Settings tab is now the only place model choice lives, which
puts one question at the centre of every case below: *does what the browser
saved actually reach the model call, and does a failure to read it still leave
a briefing on the table in the morning?*

  1. a stored row overrides the environment defaults, per field
  2. a partial row overrides ONLY the fields it fills
  3. an unreachable Supabase, a missing table and an empty row all degrade to
     the environment instead of raising
  4. stored keys beat environment keys, and clear on reset
  5. the stages read the model late, so a settings change is not frozen at import
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout

# The Gemini SDK is optional and never reached here; llm.py imports it lazily.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm            # noqa: E402
import settings       # noqa: E402


class FakeStore:
    """Stands in for store.py. `row` may be a dict, None, or an Exception."""

    def __init__(self, row, configured=True):
        self.row = row
        self._configured = configured
        self.calls = 0

    def configured(self):
        return self._configured

    def select_one(self, table, params):
        self.calls += 1
        assert table == "app_settings", table
        assert params == {"id": "eq.1"}, params
        if isinstance(self.row, Exception):
            raise self.row
        return self.row


# Environment overrides are rolled back at the START of the next apply_with,
# not in its own finally: several cases assert on llm.api_key() AFTER the call
# returns, and restoring inside would put the variable back before they looked.
_ENV_APPLIED: dict[str, str | None] = {}


def _rollback_env():
    for k, v in _ENV_APPLIED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _ENV_APPLIED.clear()


def apply_with(row, configured=True, env=None):
    """Run settings.apply() against a fake store, returning its printed log."""
    _rollback_env()
    llm.reset_state()
    # Restore the module defaults each time: apply_settings mutates globals, and
    # a leaked override would make the next case pass for the wrong reason.
    llm.GATEKEEPER_MODEL = "openai:gpt-5.6-sol"
    llm.EDITOR_MODEL = "openai:gpt-5.6-sol"
    llm.GATEKEEPER_FALLBACK_MODELS = ["openai:gpt-5.6-terra"]
    llm.EDITOR_FALLBACK_MODELS = ["openai:gpt-5.6-terra"]
    llm.OPENAI_REASONING_EFFORT = "low"

    for k, v in (env or {}).items():
        _ENV_APPLIED[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    real_store = settings.store
    settings.store = FakeStore(row, configured)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            settings.apply()
    finally:
        settings.store = real_store
    return buf.getvalue()


FULL_ROW = {
    "id": 1,
    "gatekeeper_model": "anthropic:claude-sonnet-5",
    "editor_model": "gemini:gemini-3.5-flash",
    "gatekeeper_fallback_models": ["openai:gpt-5.6-luna", "gemini:gemini-3.5-flash"],
    "editor_fallback_models": ["openai:gpt-5.6-terra"],
    "openai_api_key": "sk-openai-stored",
    "anthropic_api_key": "sk-ant-stored",
    "gemini_api_key": "AIza-stored",
    "openai_reasoning_effort": "high",
}


# ---------------------------------------------------------------- the cases
def test_stored_row_overrides_every_field():
    apply_with(FULL_ROW, env={"ANTHROPIC_API_KEY": None})
    assert llm.GATEKEEPER_MODEL == "anthropic:claude-sonnet-5", llm.GATEKEEPER_MODEL
    assert llm.EDITOR_MODEL == "gemini:gemini-3.5-flash", llm.EDITOR_MODEL
    assert llm.GATEKEEPER_FALLBACK_MODELS == [
        "openai:gpt-5.6-luna", "gemini:gemini-3.5-flash"]
    assert llm.EDITOR_FALLBACK_MODELS == ["openai:gpt-5.6-terra"]
    assert llm.OPENAI_REASONING_EFFORT == "high", llm.OPENAI_REASONING_EFFORT


def test_partial_row_leaves_the_rest_alone():
    """A row that names only the editor must not blank the gatekeeper chain.

    This is the shape a half-filled Settings form produces, and silently
    emptying the fallback list would remove the failover that keeps an
    unattended run alive.
    """
    apply_with({"id": 1, "editor_model": "openai:gpt-5.6-luna"})
    assert llm.EDITOR_MODEL == "openai:gpt-5.6-luna", llm.EDITOR_MODEL
    assert llm.GATEKEEPER_MODEL == "openai:gpt-5.6-sol", llm.GATEKEEPER_MODEL
    assert llm.GATEKEEPER_FALLBACK_MODELS == ["openai:gpt-5.6-terra"]


def test_null_and_blank_are_both_not_set():
    apply_with({"id": 1, "gatekeeper_model": None, "editor_model": "   ",
                "editor_fallback_models": ["", "  "]})
    assert llm.GATEKEEPER_MODEL == "openai:gpt-5.6-sol", llm.GATEKEEPER_MODEL
    assert llm.EDITOR_MODEL == "openai:gpt-5.6-sol", llm.EDITOR_MODEL
    assert llm.EDITOR_FALLBACK_MODELS == ["openai:gpt-5.6-terra"]


def test_unreachable_supabase_falls_back_to_env():
    """The whole failure posture in one case: no raise, defaults intact."""
    log = apply_with(RuntimeError("connection refused"))
    assert llm.GATEKEEPER_MODEL == "openai:gpt-5.6-sol", llm.GATEKEEPER_MODEL
    assert "connection refused" in log, log
    assert "environment defaults" in log, log


def test_missing_table_falls_back_to_env():
    log = apply_with(None)
    assert llm.GATEKEEPER_MODEL == "openai:gpt-5.6-sol", llm.GATEKEEPER_MODEL
    assert "migration 004" in log, log


def test_unconfigured_supabase_is_not_an_error():
    log = apply_with(FULL_ROW, configured=False)
    assert llm.GATEKEEPER_MODEL == "openai:gpt-5.6-sol", llm.GATEKEEPER_MODEL
    assert "environment defaults" in log, log


def test_empty_row_reports_and_keeps_defaults():
    log = apply_with({"id": 1})
    assert llm.GATEKEEPER_MODEL == "openai:gpt-5.6-sol", llm.GATEKEEPER_MODEL
    assert "empty" in log, log


def test_stored_key_beats_environment_key():
    apply_with(FULL_ROW, env={"OPENAI_API_KEY": "sk-from-env"})
    assert llm.api_key(llm.OPENAI) == "sk-openai-stored", llm.api_key(llm.OPENAI)
    assert llm.api_key(llm.ANTHROPIC) == "sk-ant-stored"


def test_environment_key_is_the_backstop():
    """With no stored key, the workflow secret still has to work."""
    apply_with({"id": 1, "editor_model": "openai:gpt-5.6-luna"},
               env={"OPENAI_API_KEY": "sk-from-env"})
    assert llm.api_key(llm.OPENAI) == "sk-from-env", llm.api_key(llm.OPENAI)


def test_keys_do_not_leak_into_the_environment():
    """A stored key must not end up in os.environ, where any subprocess or
    crash dump would pick it up."""
    apply_with(FULL_ROW, env={"ANTHROPIC_API_KEY": None})
    assert os.environ.get("ANTHROPIC_API_KEY") is None, "key leaked to environ"


def test_configured_model_without_a_key_is_called_out():
    log = apply_with(
        {"id": 1, "gatekeeper_model": "anthropic:claude-sonnet-5"},
        env={"ANTHROPIC_API_KEY": None, "OPENAI_API_KEY": "sk-x"})
    assert "WARNING" in log and "anthropic" in log, log


def test_stages_read_the_model_late():
    """gatekeeper/editor must not freeze the pre-override value at import.

    This is the regression that would make the Settings tab look wired up while
    changing nothing at all — the exact failure the late-binding comment in
    llm.py exists to prevent.
    """
    import gatekeeper, editor           # noqa: E402 — imported before apply()
    apply_with(FULL_ROW)

    # Checked against the module NAMESPACE, not the source text: `from llm
    # import GATEKEEPER_MODEL` is exactly what puts that name into the module,
    # and a source scan would also match the comment warning against it.
    for module in (gatekeeper, editor):
        for name in ("GATEKEEPER_MODEL", "EDITOR_MODEL",
                     "GATEKEEPER_FALLBACK_MODELS", "EDITOR_FALLBACK_MODELS"):
            assert not hasattr(module, name), (
                f"{module.__name__} binds {name} at import — a settings "
                f"override would be silently ignored")

    # And the modules resolve through the llm module object, so they see it.
    assert gatekeeper.llm.GATEKEEPER_MODEL == "anthropic:claude-sonnet-5"
    assert editor.llm.EDITOR_MODEL == "gemini:gemini-3.5-flash"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    print(f"running {len(tests)} settings tests\n")
    for t in tests:
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
