"""F826 (#683) D3 — the Claude Code statusLine emitter.

Claude Code invokes this module as its ``statusLine.command`` on session
start/resume, new assistant message, /compact, permission-mode change, command
change, rate-limit change AND every ``refreshInterval`` (1500 ms). The timer is
what makes an idle ``/model`` or effort change visible without a prompt being
submitted (blueprint D3 / AC1).

Contract, in order (D3, gate SHOULD-2/SHOULD-5, NIT-1):

1. Read the statusline JSON Claude passes on **stdin** — only the allowlisted
   scalars ``model.id``, ``model.display_name``, ``output_style`` /
   ``effort.level`` and ``session_id`` are touched; no transcript body.
2. Resolve ``terminal_id`` from the pane env ``CAO_TERMINAL_ID`` (never guessed).
3. Write the sidecar ``CAO_HOME_DIR/observe/<terminal_id>.json`` **atomically
   first** — tmp file in the SAME directory, then ``os.replace`` — because the
   300 ms debounce and in-flight cancellation make write-then-exit mandatory:
   a process that printed before it wrote could be cancelled between the two.
4. Print exactly ONE line ``<model> · <effort>`` — regardless of the write's
   outcome (NIT-1: a failed sidecar write degrades the fleet marker, it must
   never blank the user's pane). The line is CONSTANT while the selection is
   constant (SHOULD-5): no clock, counter, age or spinner, so Claude's
   ``wait_until_input_ready`` byte-identical stability check still settles.

Imports are stdlib + ``json`` ONLY (AC5): the whole steady-state budget is one
subprocess per Claude terminal per ``refreshInterval``, runtime ≤ 50 ms, no
network. It deliberately does NOT import ``cli_agent_orchestrator.constants``
(that would drag the package import chain into the 50 ms budget); the home dir
is resolved from the same env var / default that ``constants`` uses, inline.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# The sidecar model line joiner. A middle dot, matching the D3 spec text.
_SEP = " \u00b7 "
# What the printed line shows for an absent field (N2: non-thinking model has no
# effort.level). Kept a single glyph so the line stays short and constant.
_UNKNOWN = "-"


def _home_dir() -> Path:
    """Resolve ``CAO_HOME_DIR`` inline (no package import — AC5 50 ms budget).

    Mirrors ``constants.CAO_HOME_DIR``: the ``CAO_HOME_DIR`` env override wins
    (empty/whitespace treated as unset, tilde expanded), else
    ``~/.aws/cli-agent-orchestrator``. This module must not import
    ``constants`` — that pulls the package ``__init__`` chain into a process
    budgeted at stdlib + json.
    """
    raw = os.environ.get("CAO_HOME_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".aws" / "cli-agent-orchestrator"


def _extract(event: object) -> tuple[str | None, str | None, str | None]:
    """Pull (model, effort, claude_session_id) from the statusline JSON.

    Per the Claude Code statusline schema (docs.anthropic.com/en/docs/
    claude-code/statusline): ``model.id`` / ``model.display_name``,
    ``effort.level`` (one of low/medium/high/xhigh/max — **absent when the
    current model does not support the effort parameter**, N2), and
    ``session_id``. Only these allowlisted scalars are read; no transcript body.
    ``model`` prefers ``display_name`` (N2: this is user-visible in every pane)
    and falls back to ``id``. Any missing field is ``None`` (never invented).
    """
    if not isinstance(event, dict):
        return None, None, None

    model_obj = event.get("model")
    model: str | None = None
    if isinstance(model_obj, dict):
        disp = model_obj.get("display_name")
        mid = model_obj.get("id")
        model = disp if isinstance(disp, str) and disp else (mid if isinstance(mid, str) else None)
    elif isinstance(model_obj, str):
        model = model_obj

    # ``effort.level`` is the reasoning effort this feature tracks. It is
    # ABSENT (N2) for a non-thinking model — then effort is None and the line
    # renders "-". A bare-string ``effort`` is tolerated for forward compat.
    effort: str | None = None
    eff_obj = event.get("effort")
    if isinstance(eff_obj, dict):
        level = eff_obj.get("level")
        if isinstance(level, str) and level:
            effort = level
    elif isinstance(eff_obj, str) and eff_obj:
        effort = eff_obj

    sid = event.get("session_id")
    session_id = sid if isinstance(sid, str) and sid else None
    return model, effort, session_id


def _write_sidecar(
    home: Path,
    terminal_id: str,
    model: str | None,
    effort: str | None,
    session_id: str | None,
) -> bool:
    """Atomically write the observe sidecar. Returns True on success.

    tmp file in the SAME directory as the target (``os.replace`` is atomic only
    within a filesystem — NIT-1), ``mkdir(parents=True, exist_ok=True)`` on
    first run, event_time = the emitter's own realtime wall clock in ns (D3: the
    reader orders by ``event_time`` and rejects a sidecar whose event_time is
    not greater than the last accepted one; no file-resident seq).
    """
    observe_dir = home / "observe"
    try:
        observe_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    record = {
        "terminal_id": terminal_id,
        "claude_session_id": session_id,
        "model": model,
        "effort": effort,
        "event_time": time.time_ns(),
    }
    target = observe_dir / f"{terminal_id}.json"
    tmp = observe_dir / f".{terminal_id}.{os.getpid()}.tmp"
    try:
        data = json.dumps(record, separators=(",", ":")).encode("utf-8")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _line(model: str | None, effort: str | None) -> str:
    """The single CONSTANT status line (SHOULD-5): ``<model> · <effort>``."""
    return f"{model or _UNKNOWN}{_SEP}{effort or _UNKNOWN}"


def main() -> int:
    model: str | None = None
    effort: str | None = None
    session_id: str | None = None
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    if raw.strip():
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            event = None
        model, effort, session_id = _extract(event)

    terminal_id = os.environ.get("CAO_TERMINAL_ID", "").strip()
    # Write the sidecar FIRST (write-then-print, D3). A missing terminal id
    # means we cannot bind the observation to a seat, so we skip the write but
    # still print — the pane must never be blanked (NIT-1).
    if terminal_id:
        _write_sidecar(_home_dir(), terminal_id, model, effort, session_id)

    # Print exactly one line, unconditionally. ``print`` adds the trailing
    # newline Claude expects for a single-line status.
    sys.stdout.write(_line(model, effort) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
