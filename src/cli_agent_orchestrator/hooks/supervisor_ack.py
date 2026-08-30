"""Supervisor inbox ack hook (F543 D22 — overlay-composed relocation).

The paired ack edge for :mod:`cli_agent_orchestrator.hooks.supervisor_drain`.
D22 relocates it into the per-seat settings overlay, composed exactly like the
four existing hooks: ``env CAO_API_BASE_URL=… python -m
cli_agent_orchestrator.hooks.supervisor_ack``. No install step, no absolute path
(parent Do-NOT 2 / WP Do-NOT 20).

Sequencing (SHOULD-5): location change only. The delivered-state cursor
semantics are F476's (server-side claim/commit choke point) and are NOT
duplicated here; this hook is a thin, idempotent transport that reports the
seat consumed its inbox through the drain. If F476 has not fully landed the
server ack is the existing best-effort path and the debt is recorded on #331.

Containment + fail-open + idempotency: identical contract to supervisor_drain —
fires only inside a CAO terminal, swallows transport errors, and is safe to run
twice (the server ack only advances a monotonic cursor).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import requests

from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.utils.http import CAOHttpClient, resolve_endpoint

cao_http = CAOHttpClient(lambda: requests)


def main() -> int:
    if not os.environ.get("CAO_TERMINAL_ID"):
        return 0
    terminal_id = os.environ["CAO_TERMINAL_ID"]
    try:
        try:
            json.load(sys.stdin)
        except Exception:
            pass
        base_url = (
            os.environ.get("CAO_ENDPOINT")
            or os.environ.get("CAO_API_BASE_URL")
            or resolve_endpoint()
        ).rstrip("/")
        headers = {}
        token = get_local_bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "terminal_id": terminal_id,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        response = cao_http.post(
            f"/terminals/{terminal_id}/inbox/drain-ack",
            base_url=base_url,
            json=payload,
            headers=headers,
            timeout=5,
        )
        response.raise_for_status()
    except Exception as exc:
        print(
            f"WARNING: CAO supervisor-ack edge failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
