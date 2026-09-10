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
4. CHAIN the user's own statusline (F894 #746): read ``statusLine.command``
   from ``~/.claude/settings.json`` and, when it is a ``type: "command"``
   entry, run it with the SAME stdin JSON and print its stdout verbatim. The
   per-terminal ``--settings`` overlay makes CAO's ``statusLine`` win over the
   user's, so without this chain the user's bar is silently REPLACED in every
   CAO pane rather than augmented.
5. Otherwise print exactly ONE line ``<model> · <effort>`` — regardless of the
   write's outcome (NIT-1: a failed sidecar write degrades the fleet marker, it
   must never blank the user's pane). The constant line is also the fallback
   whenever the user command is absent, unreadable, times out, exits non-zero
   or prints nothing.

Imports are stdlib + ``json`` ONLY (AC5): CAO's own steady-state budget is one
subprocess per Claude terminal per ``refreshInterval``, runtime ≤ 50 ms, no
network. The chained user command is an ADDITIONAL subprocess whose cost is the
user's own (capped at ``_USER_TIMEOUT_S`` = 1 s, after which the constant line
is printed instead). It deliberately does NOT import
``cli_agent_orchestrator.constants`` (that would drag the package import chain
into the 50 ms budget); the home dir is resolved from the same env var /
default that ``constants`` uses, inline.

SHOULD-5 stability, with the chain: CAO's own line is constant by construction,
and a user statusline is *expected* to be stable while the session is idle. A
user script that prints a clock, a counter or any per-refresh-changing token
makes the pane bytes change on every ``refreshInterval`` and so breaks Claude's
``wait_until_input_ready`` byte-identical stability check — that is the user's
script to fix. ``observe.claude_statusline = false`` in
``CAO_HOME_DIR/settings.json`` remains the escape hatch: it drops the overlay
entirely, restoring the user's bar (and dropping the fleet model/effort marker).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# The sidecar model line joiner. A middle dot, matching the D3 spec text.
_SEP = " \u00b7 "
# What the printed line shows for an absent field (N2: non-thinking model has no
# effort.level). Kept a single glyph so the line stays short and constant.
_UNKNOWN = "-"
# Wall-clock cap on the chained user statusline command (F894). The emitter is
# re-run every ``refreshInterval`` (1500 ms), so a slower script must not stack.
_USER_TIMEOUT_S = 1.0
# Recursion guard: a user ``statusLine.command`` naming this very module would
# re-enter the emitter on every refresh. Treated as "no user command".
_SELF_MODULE = "cli_agent_orchestrator.hooks.status_emit"


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


def _user_settings_path() -> Path:
    """``~/.claude/settings.json`` — the user's own Claude settings file."""
    return Path.home() / ".claude" / "settings.json"


def _user_statusline_command() -> str | None:
    """The user's own ``statusLine.command``, or None (F894 #746).

    None whenever the file is missing/unreadable/not JSON, ``statusLine`` is
    absent or not an object, ``type`` is not ``"command"``, ``command`` is not a
    non-empty string, or the command names this module (recursion guard).
    """
    try:
        raw = _user_settings_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        settings = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(settings, dict):
        return None
    entry = settings.get("statusLine")
    if not isinstance(entry, dict) or entry.get("type") != "command":
        return None
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    if _SELF_MODULE in command:
        return None
    return command


def _run_user_statusline(command: str, raw_stdin: str) -> str | None:
    """Run the user's statusline command on the same stdin JSON.

    Returns its stdout with ONE trailing newline stripped (multi-line output is
    preserved as-is), or None on timeout, non-zero exit, empty output or a spawn
    failure — the caller then prints the constant line. stderr is captured and
    discarded: it must never reach stdout, which is the pane's status text.
    The pane environment is passed through untouched (no ``env=`` override), so
    the user's script sees exactly what a plain Claude terminal gives it.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            input=raw_stdin,
            capture_output=True,
            text=True,
            timeout=_USER_TIMEOUT_S,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout
    if not out.strip():
        return None
    if out.endswith("\n"):
        out = out[:-1]
    return out


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

    # Then CHAIN the user's own statusline (F894 #746) and print ITS output,
    # falling back to CAO's constant line when there is none or it fails. Either
    # way exactly one write happens, unconditionally: the pane is never blanked.
    rendered: str | None = None
    command = _user_statusline_command()
    if command is not None:
        rendered = _run_user_statusline(command, raw)
    if rendered is None:
        rendered = _line(model, effort)
    sys.stdout.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
