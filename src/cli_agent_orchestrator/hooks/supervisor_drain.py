"""Supervisor inbox drain hook (F543 D22 — overlay-composed relocation).

D22 relocates the supervisor drain edge OUT of any ``~/.claude`` / repo-local
``.claude/hooks`` copy and INTO the per-seat settings overlay, composed exactly
like the four existing hooks (transcript_binding, session_brief, question_marker,
children_ledger): ``env CAO_API_BASE_URL=… python -m
cli_agent_orchestrator.hooks.supervisor_drain``. No install step, no absolute
path — so the F569 #426 hazard class (hardcoded ``/home/chao`` paths) cannot
arise here (parent Do-NOT 2 / WP Do-NOT 20).

Sequencing (SHOULD-5): D22 ships the hook *location* only. The drain's internal
delivered-state machinery is F476's (D1/D3/D5/D8 — one wake cursor, server-side
claim/commit choke point) and is NOT duplicated here. This hook is a thin,
idempotent transport: on the seat's SessionStart it asks the server to drain any
pending inbox rows for this terminal through the existing delivery seam. If
F476's richer wake-cursor drain has not fully landed, the server-side
``deliver_pending`` path is the fallback and the debt is recorded on #331.

Containment: fires ONLY inside a CAO terminal (``CAO_TERMINAL_ID`` set); a bare
``return 0`` with no side effects otherwise, so a non-CAO claude session is never
touched. Fail-open: any transport error is swallowed with ``return 0`` (a drain
miss is re-attempted by the daemon reconcile sweep — never a hard failure).

Idempotent ack (repo-local copy guard): the repo-local ``.claude/hooks`` copy,
if present, may run the same edge. Both the overlay hook and any repo-local copy
are safe to run twice — the server drain is idempotent (it only advances a
monotonic cursor / re-delivers PENDING rows), so a double fire delivers nothing
extra.
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
    # Containment BEFORE anything else: zero side effects outside a CAO terminal.
    if not os.environ.get("CAO_TERMINAL_ID"):
        return 0
    terminal_id = os.environ["CAO_TERMINAL_ID"]
    try:
        # Drain event bodies are read but not required; the terminal id is the
        # only authority the server needs.
        try:
            json.load(sys.stdin)
        except Exception:
            pass
        base_url = (
            os.environ.get("CAO_ENDPOINT")
            or os.environ.get("CAO_API_BASE_URL")
            or resolve_endpoint()
        ).rstrip("/")
        headers: dict[str, str] = {}
        token = get_local_bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # F707 (#562): the server binds this edge to the ROUTE terminal via the
        # existing F332 per-terminal token, so scope alone can no longer drain a
        # foreign inbox. Every CAO terminal carries its own token in the
        # environment; absent it the request is refused 403 (fail-open at the
        # hook level — the warning below is printed and the seat is unharmed).
        terminal_token = os.environ.get("CAO_TERMINAL_TOKEN", "")
        if terminal_token:
            headers["X-CAO-Terminal-Token"] = terminal_token
        payload = {
            "terminal_id": terminal_id,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        response = cao_http.post(
            f"/terminals/{terminal_id}/inbox/drain",
            base_url=base_url,
            json=payload,
            headers=headers,
            timeout=5,
        )
        response.raise_for_status()
    except Exception as exc:
        print(
            f"WARNING: CAO supervisor-drain edge failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
