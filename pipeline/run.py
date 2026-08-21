"""
Orchestrator — runs the four stages in sequence. Any stage that raises aborts the
run (so a broken gatekeeper never dispatches a stale briefing).

One failure mode gets its own exit path: EVERY model being busy at once. That is
an upstream outage, not a misconfiguration, so it is reported as temporary and
exits 75 (EX_TEMPFAIL) instead of looking like a broken pipeline. dispatch never
runs, so the published briefing is untouched.
"""
import sys

import ingest, gatekeeper, editor, dispatch
from gatekeeper import EmptyScoringError
from llm import ModelsBusyError

if __name__ == "__main__":
    try:
        ingest.main()
        gatekeeper.main()
        editor.main()
        dispatch.main()
    except EmptyScoringError as exc:
        print(f"\n[run] NOTHING TO PUBLISH — {exc}", flush=True)
        print("[run] Supabase still holds the previous briefing, unchanged.",
              flush=True)
        sys.exit(75)          # EX_TEMPFAIL — same class as an outage
    except ModelsBusyError as exc:
        print(f"\n[run] TEMPORARY OUTAGE — {exc}", flush=True)
        print("[run] Nothing was published: Supabase still holds the previous "
              "briefing, unchanged. No key, model id or setting needs fixing — "
              "re-run the workflow when the models free up.", flush=True)
        sys.exit(75)          # EX_TEMPFAIL — distinct from a real failure
    print("[run] briefing pipeline complete.", flush=True)
