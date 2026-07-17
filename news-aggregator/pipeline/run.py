"""
Orchestrator — runs the four stages in sequence. Any stage that raises aborts the
run (so a broken gatekeeper never dispatches a stale briefing).
"""
import ingest, gatekeeper, editor, dispatch

if __name__ == "__main__":
    ingest.main()
    gatekeeper.main()
    editor.main()
    dispatch.main()
    print("[run] briefing pipeline complete.", flush=True)
