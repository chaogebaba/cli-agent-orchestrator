"""Callback rewake hook (F810 #667 — overlay-composed port of f213 --arm).

Python port of the old repo-local ``.claude/hooks/f213-callback-rewake.sh
--arm``, relocated into the per-seat settings overlay composed by
``providers/claude_code.py::_write_terminal_settings`` and invoked as ``env
CAO_API_BASE_URL=… python -m cli_agent_orchestrator.hooks.rewake --arm
--source=<edge>``. No console_scripts entry point, no absolute install path, no
``~/.claude`` / ``.claude/hooks`` reference (Do-NOT 20 / F569 #426).

Contract (f213 A1, preserved):
  * The watcher runs inline for a BOUNDED window (the harness kills it at the
    hook timeout — 3600s on the async Stop edge, ~10s on PostToolUse). It polls
    the terminal's PENDING inbox and, on a stable new callback, wakes the seat.
  * exit 2 = wake (the ONLY non-zero exit); stdout carries one JSON line
    ``{"rewakeSummary": "CAO callback waiting (id <max>)"}`` (D5/AC16) and stderr
    carries a model-visible id/sender preview.
  * exit 0 = everything else (no CAO_TERMINAL_ID, empty/malformed stdin, server
    down, no pending rows, cooldown/streak suppression, timeout expiry).

Suppression rules ported from the ``.sh``: BUSY-class ``[CONDITION]`` pings never
wake the seat (F639 #494); two-poll stability before a wake (D22); a wake for an
id no newer than the last is gated by a cooldown and a wake-streak cap (D10).

State (last wake id / ts / streak) is kept in a per-terminal JSON file so the
cooldown/streak survive across arms within an incarnation. Fail-open throughout:
any error exits 0 (a missed wake is re-armed on the next edge; the drain hook is
the authoritative digest carrier regardless).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.utils.http import CAOHttpClient, resolve_endpoint

cao_http = CAOHttpClient(lambda: requests)

# Default tuning knobs (env-overridable at CALL time inside main(); reading them
# per-call keeps the watcher testable and lets a Stop-edge arm honour a changed
# poll interval without a re-import).
_DEFAULT_POLL_INTERVAL_S = 5.0
_DEFAULT_COOLDOWN_S = 300.0
_DEFAULT_MAX_STREAK = 3
_DEFAULT_STABILITY_POLLS = 2
#: Absolute inline lifetime ceiling when no test deadline is set. The harness
#: normally kills the hook at its timeout; this is a belt-and-braces bound so a
#: unit test (or a harness that never signals) cannot spin forever.
_DEFAULT_MAX_INLINE_S = 3600.0


def _knobs() -> tuple[float, float, int, int, float]:
    """Resolve (poll_interval, cooldown, max_streak, stability, deadline) now."""
    poll = float(os.environ.get("F213_POLL_INTERVAL_S", _DEFAULT_POLL_INTERVAL_S))
    cooldown = float(os.environ.get("F213_COOLDOWN_S", _DEFAULT_COOLDOWN_S))
    max_streak = int(os.environ.get("F213_MAX_STREAK", _DEFAULT_MAX_STREAK))
    stability = int(os.environ.get("F213_STABILITY_POLLS", _DEFAULT_STABILITY_POLLS))
    max_inline = float(os.environ.get("F213_MAX_INLINE_S", _DEFAULT_MAX_INLINE_S))
    deadline_env = os.environ.get("F213_DEADLINE_S")  # test-only hard bound
    deadline = float(deadline_env) if deadline_env is not None else max_inline
    return poll, cooldown, max_streak, stability, deadline


def _parse_args(argv: list[str]) -> tuple[str, str]:
    mode = "arm"
    source = "stop"
    for arg in argv:
        if arg == "--arm":
            mode = "arm"
        elif arg == "--prime":
            mode = "prime"
        elif arg.startswith("--source="):
            source = arg[len("--source=") :]
    return mode, source


def _state_path(terminal_id: str) -> Path:
    return Path(CAO_HOME_DIR) / f"f810-rewake-state.{terminal_id}.json"


def _read_state(terminal_id: str) -> tuple[int, float, int]:
    try:
        d = json.loads(_state_path(terminal_id).read_text(encoding="utf-8"))
        return (
            int(d.get("last_wake_max_id", 0)),
            float(d.get("last_wake_ts", 0)),
            int(d.get("wake_streak", 0)),
        )
    except Exception:
        return 0, 0.0, 0


def _save_state(terminal_id: str, mid: int, ts: float, streak: int) -> None:
    try:
        Path(CAO_HOME_DIR).mkdir(parents=True, exist_ok=True, mode=0o700)
        _state_path(terminal_id).write_text(
            json.dumps({"last_wake_max_id": mid, "last_wake_ts": ts, "wake_streak": streak}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _is_busy_suppressible(body: str) -> bool:
    """BUSY-class ``[CONDITION]`` pings never wake the seat (F639 #494)."""
    if not body.startswith("[CONDITION]"):
        return False
    if "kind=BUSY" in body:
        return True
    if "kind=PROC_EXITED" in body and "subtype=command_exit_code" in body:
        return True
    return False


def _poll_pending(terminal_id: str, base_url: str, headers: dict[str, str]) -> tuple[str, int, str]:
    """Return (status, max_id, preview). status ∈ {pending, empty, error}."""
    try:
        resp = cao_http.get(
            "/messages",
            base_url=base_url,
            params={"to": terminal_id, "status": "pending", "limit": 100},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return "error", 0, ""
    items: list[Any] = []
    if isinstance(data, dict):
        items = data.get("items", data.get("messages", [])) or []
    pending = [
        i
        for i in items
        if isinstance(i, dict)
        and i.get("status", "pending") == "pending"
        and not _is_busy_suppressible(str(i.get("message", "")))
    ]
    if not pending:
        return "empty", 0, ""
    max_id = max(int(i.get("id", 0)) for i in pending)
    preview_lines = [f"  [{i.get('id', '?')}] from={i.get('sender_id', '?')}" for i in pending[:5]]
    return "pending", max_id, "\n".join(preview_lines)


def _wake(terminal_id: str, max_id: int, preview: str, streak: int, now: float) -> int:
    _save_state(terminal_id, max_id, now, streak + 1)
    # stdout: minimal valid rewake JSON (D5/AC16).
    print(json.dumps({"rewakeSummary": f"CAO callback waiting (id {max_id})"}))
    # stderr: model-visible preview (D9).
    if preview:
        print(preview, file=sys.stderr)
    return 2


def _drain_gate(argv: list[str]) -> Any:
    """Subagent gate (D35): a subagent invocation must never arm the watcher.

    The seat's in-harness subagent inherits CAO_TERMINAL_ID; without this gate
    its Stop/PostToolUse arm would poll the SUPERVISOR's inbox. Discriminators:
    a ``CLAUDE_AGENT_ID`` env var, or an ``agent_id`` key in the hook stdin JSON.
    Returns the parsed stdin event (or None) so the caller does not re-read it.
    """
    if os.environ.get("CLAUDE_AGENT_ID"):
        return "subagent"
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw:
        return None
    if '"agent_id"' in raw:
        return "subagent"
    try:
        return json.loads(raw)
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode, _source = _parse_args(argv)

    # Containment: only inside a CAO terminal.
    if not os.environ.get("CAO_TERMINAL_ID"):
        return 0
    terminal_id = os.environ["CAO_TERMINAL_ID"]

    gate = _drain_gate(argv)
    if gate == "subagent":
        return 0

    # --prime is a no-op arm-preventer in this port (SessionStart uses the drain
    # digest as its sync carrier); only --arm runs the watcher.
    if mode != "arm":
        return 0

    try:
        base_url = (
            os.environ.get("CAO_ENDPOINT")
            or os.environ.get("CAO_API_BASE_URL")
            or resolve_endpoint()
        ).rstrip("/")
        headers: dict[str, str] = {}
        token = get_local_bearer()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        terminal_token = os.environ.get("CAO_TERMINAL_TOKEN", "")
        if terminal_token:
            headers["X-CAO-Terminal-Token"] = terminal_token

        last_wake_max_id, last_wake_ts, wake_streak = _read_state(terminal_id)
        armed_ts = time.monotonic()
        poll_interval, cooldown_s, max_streak, stability_polls, deadline_s = _knobs()

        candidate_max_id = 0
        candidate_seen = 0
        while True:
            if (time.monotonic() - armed_ts) >= deadline_s:
                _save_state(terminal_id, last_wake_max_id, last_wake_ts, wake_streak)
                return 0

            status, max_id, preview = _poll_pending(terminal_id, base_url, headers)
            if status == "error":
                time.sleep(poll_interval)
                continue
            if status == "empty":
                candidate_max_id = 0
                candidate_seen = 0
                time.sleep(poll_interval)
                continue

            # Two-poll stability (D22).
            if max_id == candidate_max_id and candidate_max_id > 0:
                candidate_seen += 1
            else:
                candidate_max_id = max_id
                candidate_seen = 1
            if candidate_seen < stability_polls:
                time.sleep(poll_interval)
                continue

            now = time.time()
            if max_id > last_wake_max_id:
                # Strictly newer id: wake immediately, reset streak.
                return _wake(terminal_id, max_id, preview, 0, now)
            # Same id (or older): cooldown + streak gating (D10).
            if (now - last_wake_ts) < cooldown_s:
                time.sleep(poll_interval)
                continue
            if wake_streak >= max_streak:
                time.sleep(poll_interval)
                continue
            return _wake(terminal_id, max_id, preview, wake_streak, now)
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
