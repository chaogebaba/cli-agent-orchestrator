"""Best-effort Claude children-ledger transport (F568 D12a).

A ``question_marker.py`` sibling: same dead-letter / flock / rotation /
redaction machinery, same ``get_local_bearer()`` credential, same ``return 0``
always. It registers an in-harness Agent subagent when the seat DISPATCHES one
and releases it when that subagent STOPS, so the fleet can distinguish "seat
busy" from "seat idle, children busy" (the F568 #425 incident).

Edge classification (D12a):

* **register** — ``PreToolUse`` whose ``tool_name`` matches the subagent
  dispatch tool. ``Agent`` is the current name (Claude Code 2.1.63+); ``Task``
  is the historical name; the live root ``.claude/settings.json`` wires
  ``Task|Agent``. We register at the PRE edge deliberately: ``PostToolUse``
  fires only AFTER the tool completes, which is the wrong edge for a
  "child in flight" fact (the child is already gone by then).
* **release** — ``SubagentStop`` (the subagent's ``Stop``, converted to
  ``SubagentStop`` at runtime). Carries no reliable, version-stable id, so the
  release passes whatever id it can find and the server pops the oldest entry
  when none is given — count-correct because Claude Code pairs each dispatch
  with exactly one stop.

Events that are neither edge are dropped with ``return 0`` and NO POST (they
are not errors). Containment: this hook fires ONLY inside a CAO terminal
(``CAO_TERMINAL_ID`` set) — a bare ``return 0`` before any side effect
otherwise, so a non-CAO claude session is never touched.

Nothing here is claude-specific on the wire: the body carries only
``op``/``child_id``/``event``/``ts``/``nonce``, so codex/kiro can adopt the same
channel later.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.utils.http import CAOHttpClient, resolve_endpoint

cao_http = CAOHttpClient(lambda: requests)
_DEADLETTER_MAX_BYTES = 512 * 1024
_SENSITIVE = re.compile(r"authorization|bearer|token|secret|api[_-]?key", re.IGNORECASE)

# D12a dispatch-tool matcher of record: the live root .claude/settings.json
# wires ``Task|Agent`` and ``Agent`` is the new name for ``Task`` as of Claude
# Code 2.1.63. Case-sensitive exact names — the CC matcher is exact.
_DISPATCH_TOOL_NAMES = frozenset({"Agent", "Task"})


def _bounded_utf8(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _redact_error(value: object) -> str:
    text = " ".join(str(value).splitlines())
    sensitive = _SENSITIVE.search(text)
    if sensitive is not None:
        text = text[: sensitive.start()] + sensitive.group(0) + " [REDACTED]"
    home = str(Path.home())
    text = re.sub(
        re.escape(home) + r"(?:/[^\s'\";,]+)+",
        lambda match: Path(match.group(0)).name,
        text,
    )
    return text[:200]


def _deadletter(terminal_id: str, event_source: str, error_class: str, error: object) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "terminal_id": _bounded_utf8(terminal_id, 128),
        "event_source": _bounded_utf8(event_source, 64),
        "error_class": _bounded_utf8(error_class, 64),
        "error": _redact_error(error),
    }
    encoded = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    while len(encoded) > 1024 and record["error"]:
        record["error"] = record["error"][:-1]
        encoded = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    root = Path(CAO_HOME_DIR)
    lock_fd = None
    data_fd = None
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        lock_path = root / "hook-deadletter.lock"
        data_path = root / "hook-deadletter.jsonl"
        rotated_path = root / "hook-deadletter.jsonl.1"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(lock_fd, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if data_path.exists():
            data_path.chmod(0o600)
        if rotated_path.exists():
            rotated_path.chmod(0o600)
        current_size = data_path.stat().st_size if data_path.exists() else 0
        if current_size + len(encoded) > _DEADLETTER_MAX_BYTES:
            if data_path.exists():
                os.replace(data_path, rotated_path)
                rotated_path.chmod(0o600)
            current_size = 0
        data_fd = os.open(data_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(data_fd, 0o600)
        os.write(data_fd, encoded)
    except Exception as exc:
        print(
            f"WARNING: CAO children-ledger dead-letter failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    finally:
        if data_fd is not None:
            os.close(data_fd)
        if lock_fd is not None:
            os.close(lock_fd)


def _classify(event: dict[str, Any]) -> tuple[str, str | None, str | None] | None:
    """Map one CC hook event to ``(op, child_id, release_token)`` or ``None``.

    ``op`` is ``"register"`` or ``"release"``. Returns ``None`` (drop, no POST,
    not an error) for any event that is not a recognised dispatch/stop edge.

    F579/#425 D17 release-edge fix (P-B = release-lost by id mismatch): a
    ``SubagentStop`` carries ``agent_id``/``agent_type``/``stop_hook_active`` and
    **no** ``tool_use_id``/``tool_call_id``; register stores the ``PreToolUse``
    ``toolu_…`` tool_use_id. The two id namespaces never meet, so the release
    branch emits ``child_id`` ONLY when a tool-call-namespace key is actually
    present, else ``None`` — restoring the ``None → pop-oldest`` count-correct
    contract that the always-present ``agent_id`` had been suppressing. The
    subagent-lifecycle id is still carried, as ``release_token`` (an
    observability + idempotency field), NEVER as the ledger key.
    """
    event_name = str(event.get("hook_event_name") or event.get("hookEventName") or "")
    tool_name = event.get("tool_name") or event.get("toolName")
    tool_name = str(tool_name) if tool_name is not None else None

    if event_name == "PreToolUse":
        if tool_name in _DISPATCH_TOOL_NAMES:
            # tool_call_id correlates the dispatch; not required for the count
            # (release pops oldest when unmatched) but stored for observability.
            child_id = (
                event.get("tool_call_id")
                or event.get("toolCallId")
                or event.get("tool_use_id")
                or uuid.uuid4().hex
            )
            return "register", str(child_id), None
        return None
    if event_name == "SubagentStop":
        # Emit child_id ONLY for a tool-call-namespace key (never agent_id):
        # a SubagentStop never carries one, so this is None in production and the
        # server pops the oldest entry (count-correct). agent_id travels as the
        # release_token — the idempotency/observability field, not the key.
        child_id = event.get("tool_call_id") or event.get("toolCallId") or event.get("tool_use_id")
        release_token = (
            event.get("agent_id") or event.get("subagent_id") or event.get("subagentId")
        )
        return (
            "release",
            (str(child_id) if child_id is not None else None),
            (str(release_token) if release_token is not None else None),
        )
    return None


def main() -> int:
    # Containment BEFORE anything else: no HTTP, no dead-letter, zero side
    # effects when this fires outside a CAO terminal.
    if not os.environ.get("CAO_TERMINAL_ID"):
        return 0

    terminal_id = os.environ["CAO_TERMINAL_ID"]
    event_source = "unparsed"
    try:
        event = json.load(sys.stdin)
        event_source = str(event.get("hook_event_name") or event.get("hookEventName") or "")
        classified = _classify(event)
        if classified is None:
            return 0
        op, child_id, release_token = classified

        base_url = (
            os.environ.get("CAO_ENDPOINT")
            or os.environ.get("CAO_API_BASE_URL")
            or resolve_endpoint()
        ).rstrip("/")
        payload: dict[str, Any] = {
            "terminal_id": terminal_id,
            "op": op,
            "event": event_source,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "nonce": uuid.uuid4().hex,
        }
        if child_id is not None:
            payload["child_id"] = child_id
        if release_token is not None:
            payload["release_token"] = release_token
        headers = {}
        token = get_local_bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = cao_http.post(
            f"/terminals/{terminal_id}/children-ledger",
            base_url=base_url,
            json=payload,
            headers=headers,
            timeout=5,
        )
        response.raise_for_status()
    except Exception as exc:
        _deadletter(terminal_id, event_source, type(exc).__name__, exc)
        print(
            f"WARNING: CAO children-ledger edge failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
