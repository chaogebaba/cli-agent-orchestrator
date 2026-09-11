"""Supervisor inbox drain hook (F543 D22 relocation + F810 #667 digest surface).

D22 relocated the supervisor drain edge OUT of any ``~/.claude`` / repo-local
``.claude/hooks`` copy and INTO the per-seat settings overlay, composed exactly
like the sibling hooks (transcript_binding, session_brief, question_marker,
children_ledger): ``env CAO_API_BASE_URL=… python -m
cli_agent_orchestrator.hooks.supervisor_drain``. No install step, no absolute
path (parent Do-NOT 2 / WP Do-NOT 20).

F810 #667 — WHY THIS HOOK GREW A STDOUT DIGEST. Before F810 this hook only
POSTed ``/terminals/<id>/inbox/drain`` (a server-side ``deliver_pending``
trigger) and printed NOTHING to the seat. On the SessionStart edge alone that
left a foreign-repo seat with pending callback rows and no digest in its
context — the 18-minute silence in the evidence pack. F810 ports the digest
behaviour of the old repo-local ``supervisor-inbox-drain.sh`` into this module
and wires it onto PostToolUse (matcher ``.*``) and Stop as well as SessionStart:
it lists PENDING rows (claiming them via the messages API's ``claim=hook``),
prints the EXACT envelope the ``.sh`` printed (so nothing downstream that reads
the digest changes), acks up to the max id, and suppresses decision-free BUSY
pings the same way the ``.sh`` did.

Containment: fires ONLY inside a CAO terminal (``CAO_TERMINAL_ID`` set); a bare
``return 0`` with no side effects otherwise. Fail-open: any transport error is
swallowed with ``return 0`` (a drain miss is re-attempted next fire). Idempotent:
``claim=hook`` is a one-shot claim and the ack only advances a monotonic cursor,
so a double fire (overlay + any leftover repo-local copy) surfaces nothing extra.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import requests

from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.utils.http import CAOHttpClient, resolve_endpoint

cao_http = CAOHttpClient(lambda: requests)

#: Context budget for the emitted digest — mirrors the .sh MAX_CONTEXT.
_MAX_CONTEXT = 16000

#: F810 #667 (r3, B4): the SessionStart edge stays SERVER-TRIGGER-ONLY (the base
#: D22 behaviour: POST ``/inbox/drain`` and nothing else). The claim/ack digest
#: leg — ``GET /messages?…claim=hook`` + ``POST /messages/ack`` — is only run on
#: the *turn* edges (PostToolUse / Stop), which the provider overlay composes for
#: a server-authoritatively identified SUPERVISOR seat alone (the overlay gates
#: those legs on ``AgentProfile.role``, read server-side, never a client flag).
#: A worker's overlay carries drain ONLY on SessionStart, so this gate means a
#: worker never issues a ``claim=hook`` read and never acks — which is the B4
#: fix and what the worker SessionStart witness asserts.
_SESSION_START_EVENT = "SessionStart"


def _is_busy_suppressible(body: str) -> bool:
    """True for a decision-free BUSY-class ``[CONDITION]`` ping (F639 #494).

    Mirrors the ``.sh`` suppression predicate verbatim: a ``[CONDITION]`` body
    whose kind is BUSY, OR a ``PROC_EXITED`` whose subtype is
    ``command_exit_code`` (a non-zero shell exit inside a live worker, F718
    #574). Any other condition kind and any non-condition message surfaces.
    """
    stripped = body.lstrip()
    if not stripped.startswith("[CONDITION]"):
        return False
    if "kind=BUSY" in body:
        return True
    if "kind=PROC_EXITED" in body and "subtype=command_exit_code" in body:
        return True
    return False


def _age_str(created: Any) -> str:
    """Return the ``, Ns ago`` suffix the ``.sh`` appended, best-effort."""
    if not created or not isinstance(created, str):
        return ""
    try:
        ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if ct.tzinfo is None:
            ct = ct.replace(tzinfo=timezone.utc)  # DB stores naive UTC (F130)
        age_s = int((datetime.now(timezone.utc) - ct).total_seconds())
        return f", {age_s}s ago"
    except Exception:
        return ""


def _build_digest(items: list[dict[str, Any]]) -> tuple[str | None, int | None]:
    """Build the (digest, max_id) pair the ``.sh`` emitted.

    Returns ``(None, max_id)`` when every body was BUSY-suppressed (nothing to
    surface, but the rows still ack), and ``(None, None)`` when there is nothing
    to do at all.
    """
    if not items:
        return None, None
    items = sorted(items, key=lambda m: int(m.get("id", 0)))
    min_id = items[0].get("id", "?")
    max_id_val = items[-1].get("id", "?")
    count = len(items)

    body_lines: list[tuple[Any, str, str, str]] = []
    for msg in items:
        sender = msg.get("sender_id", "unknown")
        msg_id = msg.get("id", "?")
        body = str(msg.get("message", ""))
        if _is_busy_suppressible(body):
            continue
        body_lines.append((msg_id, sender, _age_str(msg.get("created_at", "")), body))

    try:
        max_id_int: int | None = int(max_id_val)
    except (TypeError, ValueError):
        max_id_int = None

    # Every body suppressed → nothing decision-bearing to inject, but still ack.
    if not body_lines:
        return None, max_id_int

    header = (
        f"[CAO INBOX] {count} message(s) auto-surfaced and acked " f"(ids {min_id}-{max_id_val}):\n"
    )
    digest = header
    remaining_budget = _MAX_CONTEXT - len(header) - 100  # reserve for safety
    for msg_id, sender, age_str, body in body_lines:
        line_header = f"\n--- From {sender} (id {msg_id}{age_str}) ---\n"
        line_footer = "\n---"
        overhead = len(line_header) + len(line_footer)
        body_budget = remaining_budget - overhead
        try:
            prev_id = int(msg_id) - 1
        except (TypeError, ValueError):
            prev_id = 0
        trunc_hint = (
            "...[truncated; full body: cao messages list --to me "
            f"--audit-browse --after-id {prev_id} --limit 1]"
        )
        if body_budget <= 0:
            digest += line_header + trunc_hint + line_footer
            remaining_budget = 0
            continue
        if len(body) > body_budget:
            truncated_body = body[:body_budget] + trunc_hint
        else:
            truncated_body = body
        entry = line_header + truncated_body + line_footer
        digest += entry
        remaining_budget -= len(entry)
    return digest, max_id_int


def _native_delivery_healthy(terminal_id: str, base_url: str, headers: dict[str, str]) -> bool:
    """F747 (#747): True when native delivery owns this seat, so skip the digest.

    Native agent-message delivery is the seat's ONE surface; this digest is the
    net for a seat whose native channel is verifiably broken. FAIL-OPEN: any
    error reports "not healthy" and the digest still surfaces, because a missed
    callback costs more than a duplicated one.
    """
    try:
        resp = cao_http.get(
            f"/terminals/{terminal_id}/native-delivery",
            base_url=base_url,
            headers=headers,
            timeout=5,
        )
        resp.raise_for_status()
        return bool(resp.json().get("healthy"))
    except Exception:
        return False


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    token = get_local_bearer()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # F707 (#562): bind the drain/ack to the ROUTE terminal via its own token.
    terminal_token = os.environ.get("CAO_TERMINAL_TOKEN", "")
    if terminal_token:
        headers["X-CAO-Terminal-Token"] = terminal_token
    return headers


def main() -> int:
    # Containment BEFORE anything else: zero side effects outside a CAO terminal.
    if not os.environ.get("CAO_TERMINAL_ID"):
        return 0
    terminal_id = os.environ["CAO_TERMINAL_ID"]
    try:
        # Drain event bodies are read but not required; the terminal id is the
        # only authority the server needs. F810 BLOCKER 3: port the reference
        # hook's fail-closed in-process-subagent gate BEFORE the first claim —
        # an in-harness child must never claim the parent seat's callbacks.
        raw = sys.stdin.read()
        if os.environ.get("CLAUDE_AGENT_ID") or '"agent_id"' in raw:
            return 0
        try:
            event = json.loads(raw) if raw else {}
        except Exception:
            event = {}
        base_url = (
            os.environ.get("CAO_ENDPOINT")
            or os.environ.get("CAO_API_BASE_URL")
            or resolve_endpoint()
        ).rstrip("/")
        headers = _headers()

        # (1) D22 server-side trigger: best-effort deliver_pending. Kept so the
        # server's own delivery seam still fires; never fatal.
        try:
            response = cao_http.post(
                f"/terminals/{terminal_id}/inbox/drain",
                base_url=base_url,
                json={
                    "terminal_id": terminal_id,
                    "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
                headers=headers,
                timeout=5,
            )
            response.raise_for_status()
        except Exception as exc:
            print(
                f"WARNING: CAO supervisor-drain edge failed: {type(exc).__name__}",
                file=sys.stderr,
            )

        # (2) F810 digest surface: claim PENDING rows, print the exact envelope
        # into the seat context, and ack up to the max id. This is the leg that
        # makes a foreign-repo seat actually SEE its callbacks.
        #
        # F810 #667 r3 (B4): SKIP this leg on the SessionStart edge. SessionStart
        # drain is server-trigger-only (leg 1 above) exactly as base D22 was — no
        # ``claim=hook`` read, no ack. The digest is surfaced instead on the turn
        # edges (PostToolUse matcher ``.*`` / Stop), which the provider overlay
        # composes for a SUPERVISOR seat only. A worker's overlay carries drain
        # ONLY on SessionStart, so a worker never claims or acks here — the B4 fix
        # asserted by the worker SessionStart witness.
        event_name = str(event.get("hook_event_name") or event.get("hookEventName") or "")
        if event_name == _SESSION_START_EVENT:
            return 0

        # F747 (#747): one surface. When native delivery is healthy the rows are
        # being pushed to the seat as agent messages, so this hook must neither
        # claim them (a claim would steal them from the native path) nor print a
        # second copy of the same callback into the seat's context.
        if _native_delivery_healthy(terminal_id, base_url, headers):
            return 0

        try:
            listing = cao_http.get(
                "/messages",
                base_url=base_url,
                params={
                    "to": terminal_id,
                    "status": "pending",
                    "limit": 100,
                    "claim": "hook",
                },
                headers=headers,
                timeout=5,
            )
            listing.raise_for_status()
            payload = listing.json()
        except Exception:
            return 0  # fail-open: the server trigger above still ran.

        items: list[Any] = []
        if isinstance(payload, dict):
            items = payload.get("items", payload.get("messages", [])) or []
        if not isinstance(items, list) or not items:
            return 0

        digest, max_id = _build_digest([i for i in items if isinstance(i, dict)])

        if max_id is not None:
            try:
                cao_http.post(
                    "/messages/ack",
                    base_url=base_url,
                    json={"terminal_id": terminal_id, "up_to_id": max_id},
                    headers=headers,
                    timeout=5,
                )
            except Exception:
                pass  # ack failure is non-fatal; rows re-surface next fire.

        if digest is not None:
            envelope = {
                "hookSpecificOutput": {
                    "hookEventName": str(event.get("hook_event_name") or "PostToolUse"),
                    "additionalContext": digest,
                }
            }
            print(json.dumps(envelope))
    except Exception:
        # Never crash the hook.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
