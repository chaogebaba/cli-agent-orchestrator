"""Claude SessionStart transport for fresh session inventory context."""

from __future__ import annotations

import json, os, sys
import requests
from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.services.session_manifest_service import render_session_brief

MARKER = "SESSION BRIEF UNAVAILABLE — world-model incomplete"

def main() -> int:
    mode = os.environ.get("CAO_SESSION_BRIEF_MODE", "optional")
    try:
        event = json.load(sys.stdin)
        source = event.get("source", "")
        if source == "startup":
            return 0
        base = os.environ.get("CAO_API_BASE_URL", API_BASE_URL).rstrip("/")
        terminal_id = os.environ["CAO_TERMINAL_ID"]
        headers = {}
        token = get_local_bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        terminal = requests.get(f"{base}/terminals/{terminal_id}", headers=headers, timeout=5)
        terminal.raise_for_status()
        response = requests.get(f"{base}/sessions/{terminal.json()['session_name']}/manifest", headers=headers, timeout=5)
        response.raise_for_status()
        brief = render_session_brief(response.json(), thin=source == "compact")
        print(json.dumps({"additionalContext": brief}))
    except Exception:
        if mode == "required":
            print(json.dumps({"additionalContext": MARKER}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
