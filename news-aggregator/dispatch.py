"""
Stage 4 — Automated Gist Dispatch (The Delivery)

PATCHes briefing.json into the target Gist via the GitHub API. Auth is a PAT scoped
to `gist`, read from the GH_GIST_TOKEN env var. No SMTP, no server.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

import requests

HERE = Path(__file__).parent
BRIEFING = HERE / "briefing.json"

GIST_ID = os.environ["GIST_ID"]
TOKEN = os.environ["GH_GIST_TOKEN"]
TARGET_FILENAME = os.environ.get("GIST_FILENAME", "briefing.json")


def main() -> None:
    content = BRIEFING.read_text()
    resp = requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        data=json.dumps({"files": {TARGET_FILENAME: {"content": content}}}),
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["files"][TARGET_FILENAME]["raw_url"]
    print(f"[dispatch] Gist updated -> {raw}", flush=True)


if __name__ == "__main__":
    main()
