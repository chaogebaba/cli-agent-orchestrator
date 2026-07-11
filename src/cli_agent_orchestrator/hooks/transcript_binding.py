"""Best-effort Claude SessionStart transcript binding transport."""

from __future__ import annotations

import json
import os
import sys

import requests

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.security.auth import get_local_bearer


def main() -> int:
    try:
        event = json.load(sys.stdin)
        terminal_id = os.environ["CAO_TERMINAL_ID"]
        base_url = os.environ.get("CAO_API_BASE_URL", API_BASE_URL).rstrip("/")
        payload = {
            "terminal_id": terminal_id,
            "session_id": event["session_id"],
            "transcript_path": event["transcript_path"],
            "cwd": event.get("cwd", ""),
            "source": event.get("source", ""),
        }
        headers = {}
        token = get_local_bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = requests.post(
            f"{base_url}/terminals/{terminal_id}/transcript-binding",
            json=payload,
            headers=headers,
            timeout=5,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"WARNING: CAO transcript binding failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
