"""Best-effort Claude interaction-marker transport (F507 layer 1).

A ``transcript_binding.py`` sibling: same dead-letter / flock / rotation /
redaction machinery, same ``get_local_bearer()`` credential (D8), same
``return 0`` always (Do-NOT #4). It differs in three deliberate ways the
blueprint calls out:

1. **Explicit containment gate (Do-NOT #14, AC7).** ``transcript_binding``
   reads ``os.environ["CAO_TERMINAL_ID"]`` as a bare subscript whose
   ``KeyError`` is swallowed by the outer handler and dead-lettered under
   ``terminal_id="unknown"``. For a hook that fires as often as
   ``Notification`` that would turn every non-CAO claude session into
   dead-letter spam. This module hard-returns 0 BEFORE anything else — no
   HTTP, no dead-letter — when ``CAO_TERMINAL_ID`` is unset.

2. **Edge classification (D7).** CC fires this module on several hook events;
   the module maps each to a ``question_open`` / ``question_clear`` marker
   ``kind`` and POSTs it to ``/terminals/{id}/interaction-marker``. Events it
   does not recognise as an open/clear edge are dropped with ``return 0`` and
   NO POST (they are not errors).

3. **Idempotence + storm control (AC9).** ``Notification`` fires broadly, so a
   per-terminal+kind cooldown in ``cao_tmp_dir()`` collapses a burst of
   identical edges into at most one POST per cooldown window. The cooldown
   file is the ONLY state this module writes, and it lives under
   ``cao_tmp_dir()`` — never inside a repo tree (Do-NOT #5).

Nothing in this module is claude-specific on the wire (Do-NOT #8): the marker
body carries only ``kind``/``source``/``event``/``tool_name``/``ts``/``nonce``
so codex and kiro can adopt the same channel later.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.utils.http import CAOHttpClient, resolve_endpoint
from cli_agent_orchestrator.utils.temp_path import cao_tmp_dir

cao_http = CAOHttpClient(lambda: requests)
_DEADLETTER_MAX_BYTES = 512 * 1024
_SENSITIVE = re.compile(r"authorization|bearer|token|secret|api[_-]?key", re.IGNORECASE)

# AC9: storm control. A burst of identical edges within this window produces at
# most one POST. Deliberately short — a genuine state change (open->clear) uses
# a different cooldown key, so it is never suppressed by a prior open.
_COOLDOWN_S = 2.0

# D7 open matcher set of record (Fork B pick iii + gate r3/r4 additions):
#   Notification notification_type in {permission_prompt, elicitation_dialog,
#     elicitation_url_dialog}
#   PreToolUse matcher AskUserQuestion
_OPEN_NOTIFICATION_TYPES = frozenset(
    {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog"}
)
# D7 clear matcher set of record:
#   PostToolUse / PostToolUseFailure matcher AskUserQuestion
#   Stop (no matcher)
#   Notification notification_type in {elicitation_complete, elicitation_response}
_CLEAR_NOTIFICATION_TYPES = frozenset({"elicitation_complete", "elicitation_response"})
_OPEN_PRETOOL_MATCHER = "AskUserQuestion"


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
            f"WARNING: CAO interaction marker dead-letter failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    finally:
        if data_fd is not None:
            os.close(data_fd)
        if lock_fd is not None:
            os.close(lock_fd)


def _classify(event: dict[str, Any]) -> tuple[str, str | None] | None:
    """Map one CC hook event to ``(kind, tool_name)`` or ``None`` to drop it.

    ``kind`` is ``"question_open"`` or ``"question_clear"``. Returns ``None``
    (drop, no POST, not an error) for any event that is not a recognised
    open/clear edge — e.g. a ``Notification`` with ``notification_type`` in
    ``{idle_prompt, auth_success, ...}``, which Fork B pick (iii) deliberately
    excludes.
    """
    event_name = str(event.get("hook_event_name") or event.get("hookEventName") or "")
    tool_name = event.get("tool_name") or event.get("toolName")
    tool_name = str(tool_name) if tool_name is not None else None

    if event_name == "Notification":
        ntype = str(event.get("notification_type") or event.get("notificationType") or "")
        if ntype in _OPEN_NOTIFICATION_TYPES:
            return "question_open", None
        if ntype in _CLEAR_NOTIFICATION_TYPES:
            return "question_clear", None
        return None
    if event_name == "PreToolUse":
        if tool_name == _OPEN_PRETOOL_MATCHER:
            return "question_open", tool_name
        return None
    if event_name in ("PostToolUse", "PostToolUseFailure"):
        if tool_name == _OPEN_PRETOOL_MATCHER:
            return "question_clear", tool_name
        return None
    if event_name == "Stop":
        # Stop takes no matcher (D7): the turn ended, so any open question this
        # terminal held is resolved.
        return "question_clear", None
    return None


def _cooldown_gate(terminal_id: str, kind: str, now: float) -> bool:
    """Return True to PROCEED, False to suppress (AC9 storm control).

    Keyed on terminal_id+kind so an open->clear transition is never suppressed
    by a preceding open. Best-effort: any filesystem error proceeds (fail-open,
    the endpoint is idempotent anyway).
    """
    try:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{terminal_id}.{kind}")
        marker = cao_tmp_dir() / f"qmarker-cooldown.{safe}"
        if marker.exists():
            last = marker.stat().st_mtime
            if now - last < _COOLDOWN_S:
                return False
        marker.write_text(str(now), encoding="utf-8")
        try:
            marker.chmod(0o600)
        except OSError:
            pass
        return True
    except Exception:
        return True


def main() -> int:
    # Do-NOT #14 / AC7: explicit containment BEFORE anything else. No HTTP, no
    # dead-letter, zero side effects when this fires outside a CAO terminal.
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
        kind, tool_name = classified

        # AC9: storm control uses a wall clock (mtime), so gate on time.time()
        # — time.monotonic() has no relation to a file's mtime.
        if not _cooldown_gate(terminal_id, kind, time.time()):
            return 0

        base_url = (
            os.environ.get("CAO_ENDPOINT")
            or os.environ.get("CAO_API_BASE_URL")
            or resolve_endpoint()
        ).rstrip("/")
        payload: dict[str, Any] = {
            "terminal_id": terminal_id,
            "kind": kind,
            "source": event.get("source", "") or event_source,
            "event": event_source,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "nonce": uuid.uuid4().hex,
        }
        if tool_name is not None:
            payload["tool_name"] = tool_name
        headers = {}
        token = get_local_bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = cao_http.post(
            f"/terminals/{terminal_id}/interaction-marker",
            base_url=base_url,
            json=payload,
            headers=headers,
            timeout=5,
        )
        response.raise_for_status()
    except Exception as exc:
        _deadletter(terminal_id, event_source, type(exc).__name__, exc)
        print(
            f"WARNING: CAO interaction marker failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
