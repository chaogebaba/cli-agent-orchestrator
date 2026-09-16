"""A mock ACP agent: a real subprocess speaking real newline-delimited JSON-RPC.

A subprocess rather than an in-process double, deliberately.  What S1's transport
arms are about is FRAMING and TIMING across a pipe — a second prompt never
leaving CAO, a cancel settling with a ``cancelled`` stop reason, a write receipt
that survives a settle window — and an in-process double would satisfy every one
of those assertions while the shipped client wrote nothing to a pipe at all.

It is scripted rather than clever.  Behaviour is chosen by environment variable
at spawn, because the arms that need it are exactly the ones where an agent
behaves BADLY: one that never settles a cancel, one that ignores SIGTERM, one
that answers a permission request with no denial option.  A mock that only did
the right thing could not support any of them.

Run as ``python -m test.helpers.acp_mock_agent`` (or by path).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from typing import Any

#: How long the agent's "tool call" runs before it finishes on its own.  Long
#: enough that a cancel arm reliably lands mid-turn.
TURN_SECONDS = float(os.environ.get("MOCK_ACP_TURN_SECONDS", "5"))

#: ``settle`` (answer the cancel), ``ignore`` (never settle — the FAIL cell four
#: adapters showed), ``end_turn`` (settle with the WRONG stop reason, which is
#: cline's measured queued-arm behaviour).
CANCEL_MODE = os.environ.get("MOCK_ACP_CANCEL", "settle")

#: ``none`` (never ask), ``full`` (offer allow/reject), ``no_denial`` (offer only
#: allow options, so the client must cancel first and answer ``cancelled``).
PERMISSION_MODE = os.environ.get("MOCK_ACP_PERMISSION", "none")

#: ``1`` advertises ``_session/steering`` in ``initialize``'s ``_meta``.
STEERING = os.environ.get("MOCK_ACP_STEERING", "0") == "1"

#: ``1`` installs a SIGTERM handler that does nothing, reproducing the adapter
#: that only dies to SIGKILL.
IGNORE_SIGTERM = os.environ.get("MOCK_ACP_IGNORE_SIGTERM", "0") == "1"

_lock = threading.Lock()
_state: dict[str, Any] = {"cancelled": False, "turn": None, "session": None, "prompts": []}


def _send(frame: dict[str, Any]) -> None:
    with _lock:
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()


def _update(session_id: str, update: dict[str, Any]) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


def _run_turn(request_id: int, session_id: str, text: str) -> None:
    """One turn: a chunk, a long tool call, then a stop reason.

    The tool call is what a cancel has to interrupt, and it is named in the
    frames so AC-S1.21's "the frame log names the cancelled tool call" is
    satisfiable against this agent as well as against a real one.
    """
    _state["prompts"].append(text)
    _update(
        session_id,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "working"}},
    )
    tool_call_id = f"tool-{request_id}"
    _update(
        session_id,
        {"sessionUpdate": "tool_call", "toolCallId": tool_call_id, "title": "long-running"},
    )
    if PERMISSION_MODE != "none":
        options = [
            {"optionId": "vendor-allow-once", "kind": "allow_once", "name": "Allow"},
            {"optionId": "vendor-allow-always", "kind": "allow_always", "name": "Always"},
        ]
        if PERMISSION_MODE == "full":
            options.append(
                {"optionId": "vendor-reject-once", "kind": "reject_once", "name": "Reject"}
            )
        _send(
            {
                "jsonrpc": "2.0",
                "id": 9000 + request_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": tool_call_id},
                    "options": options,
                },
            }
        )

    deadline = time.monotonic() + TURN_SECONDS
    while time.monotonic() < deadline:
        if _state["cancelled"]:
            if CANCEL_MODE == "ignore":
                break  # never settles: the arm that must quarantine
            stop = "cancelled" if CANCEL_MODE == "settle" else "end_turn"
            _update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "status": "cancelled",
                },
            )
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": stop}})
            _state["cancelled"] = False
            _state["turn"] = None
            return
        time.sleep(0.02)
    if CANCEL_MODE == "ignore" and _state["cancelled"]:
        _state["turn"] = None
        return
    _send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}})
    _state["turn"] = None


def _handle(frame: dict[str, Any]) -> None:
    method = frame.get("method")
    params = frame.get("params") or {}
    if method == "initialize":
        meta: dict[str, Any] = {}
        if STEERING:
            meta["steering"] = {"shape": "_session/steering"}
        _send(
            {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {"promptCapabilities": {"image": False}, "_meta": meta},
                    "_meta": meta,
                },
            }
        )
    elif method == "session/new":
        session_id = "mock-session-1"
        _state["session"] = session_id
        _send({"jsonrpc": "2.0", "id": frame["id"], "result": {"sessionId": session_id}})
    elif method == "session/prompt":
        text = ""
        for block in params.get("prompt") or []:
            text += block.get("text", "")
        thread = threading.Thread(
            target=_run_turn, args=(int(frame["id"]), params.get("sessionId"), text), daemon=True
        )
        _state["turn"] = thread
        thread.start()
    elif method == "session/cancel":
        _state["cancelled"] = True
    elif method == "_session/steering":
        # Steering is a NOTIFICATION with no reply.  The text is appended to the
        # open turn's record so a test can assert it was seen without the agent
        # inventing a response frame the spec does not define.
        for block in params.get("content") or []:
            _state["prompts"].append("[steer]" + block.get("text", ""))
    elif "id" in frame:
        _send({"jsonrpc": "2.0", "id": frame["id"], "result": {}})


def main() -> None:
    if IGNORE_SIGTERM:
        signal.signal(signal.SIGTERM, lambda *_: None)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle(frame)


if __name__ == "__main__":
    main()
