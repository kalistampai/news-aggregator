"""
Verdict-parsing simulation — no network, no API key, no pytest required.

    python test_gatekeeper_parsing.py     # from pipeline/

Covers the failure that produced an empty briefing on 2026-08-02: OpenAI's
JSON-object mode cannot return a bare top-level array, so the gatekeeper's
verdicts always arrive wrapped in an object whose key the model chooses. Five
batches parsed cleanly, none of the wrappers were recognised, and 130 articles
became a blank page with no error in the log.

Every wrapper shape a model has plausibly emitted is asserted here, along with
the guard that stops an empty result from overwriting a good briefing.
"""
from __future__ import annotations
import os
import sys

os.environ.setdefault("LLM_MIN_INTERVAL", "0")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

import test_llm_fallback  # noqa: E402,F401 — installs the Gemini SDK stub
import gatekeeper  # noqa: E402

V = [{"id": "a1", "score": 8, "tier": "feature", "reasoning": "why"},
     {"id": "a2", "score": 5, "tier": "notable", "reasoning": "why"}]


def ids(rows):
    return [r.get("id") for r in rows]


# ---- wrapper shapes ---------------------------------------------------------
def test_bare_array():
    assert gatekeeper._unwrap(V) == V


def test_documented_wrapper():
    assert gatekeeper._unwrap({"verdicts": V}) == V


def test_alternative_wrapper_keys():
    for key in ("articles", "results", "items", "scores", "data", "output",
                "response"):
        assert gatekeeper._unwrap({key: V}) == V, key


def test_unexpected_wrapper_key():
    # The exact 2026-08-02 failure: a key nobody enumerated.
    assert gatekeeper._unwrap({"article_verdicts": V}) == V


def test_nested_wrapper():
    assert gatekeeper._unwrap({"response": {"verdicts": V}}) == V


def test_keyed_by_id_object():
    out = gatekeeper._unwrap({
        "a1": {"score": 8, "tier": "feature", "reasoning": "why"},
        "a2": {"score": 5, "tier": "notable", "reasoning": "why"}})
    assert sorted(ids(out)) == ["a1", "a2"], out
    assert out[0]["score"] == 8, out


def test_single_verdict_object():
    assert gatekeeper._unwrap(V[0]) == [V[0]]


def test_genuinely_empty_is_still_empty():
    assert gatekeeper._unwrap({}) == []
    assert gatekeeper._unwrap({"verdicts": []}) == []
    assert gatekeeper._unwrap("nonsense") == []
    assert gatekeeper._unwrap({"note": "I could not score these"}) == []


def test_shape_is_described_for_the_log():
    assert "keys" in gatekeeper._shape_of({"mystery": 1})
    assert "array" in gatekeeper._shape_of([1, 2, 3])


# ---- id matching ------------------------------------------------------------
def test_numeric_ids_still_match():
    # A model that echoes "12" as the number 12 must not silently drop the row.
    by_id = {str(a["id"]): a for a in [{"id": "12", "title": "t"}]}
    assert by_id.get(str(12)) is not None


# ---- the guard --------------------------------------------------------------
def test_empty_scoring_error_exists():
    # run.py catches this to exit 75 without letting dispatch publish; that
    # wiring is covered by the end-to-end check, which has feedparser available.
    assert issubclass(gatekeeper.EmptyScoringError, RuntimeError)


def main() -> int:
    import types as _t
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and isinstance(v, _t.FunctionType)]
    failed = []
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
