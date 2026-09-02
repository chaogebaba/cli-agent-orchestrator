"""Server-side creation of the per-session ``fleet`` TUI window (F702 J4, #473).

Before this module the fleet TUI was launched by two cli-subagents-local
triggers — ``doctrine/compose/orchestrator-md.sh`` and a ``.claude/settings.json``
hook running ``scripts/fleet-tui-ensure.sh``. Both resolve through the seat's
project directory, so a supervisor whose cwd is any other repo never got a
fleet window (#473). The window is created here instead, at the one server-side
choke point every launch path passes through (``session_service.create_session``,
reached from ``cao session start``, the deprecated ``cao launch`` and the
``POST /sessions/start`` handler), so it is repo-agnostic.

Contract, in order of importance:

1. **This never fails session creation.** Every entry point swallows all
   exceptions and returns ``False``. A missing ``cao-fleet`` binary, a tmux
   that will not answer, a backend that is not tmux at all — each is a logged
   no-op, never a raised error.
2. **Opt-out is the existing ``CAO_FLEET_TUI`` flag**, read with exactly the
   semantics the two shell guards use (``scripts/fleet-tui-ensure.sh`` and
   ``doctrine/compose/orchestrator-md.sh``): the literal string ``"0"``
   disables, anything else — including an absent key — enables. It reaches the
   server as ``cao session start --env CAO_FLEET_TUI=0`` → the request's
   ``env_vars`` → ``canonical_session_env``; ``cao-server`` is a systemd user
   unit and reads no shell environment, so that request field is the only
   channel that gets here.
3. **Idempotent.** A session that already has a window named ``fleet`` is left
   alone; this call creates a window or does nothing.

``cao-fleet`` is the console script of the optional ``cli-agent-orchestrator[fleet]``
extra. A server-only install carries no TUI library and therefore no such
binary, which is why step 2 below probes ``PATH`` before touching tmux.
"""

import importlib.util
import logging
import shutil
import subprocess
from typing import List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

FLEET_WINDOW_NAME = "fleet"
"""tmux window name, matching the name the shell launcher used."""

FLEET_WINDOW_INDEX = 1
"""Preferred index — immediately after the supervisor at index 0."""

FLEET_TUI_ENV = "CAO_FLEET_TUI"
"""Opt-out variable, shared verbatim with the two repo-local shell guards."""

FLEET_CONSOLE_SCRIPT = "cao-fleet"
"""Console script — present on PATH even without the ``[fleet]`` extra."""

FLEET_TUI_MODULE = "textual"
"""The library the ``[fleet]`` extra actually adds; its presence is the test."""

TMUX_TIMEOUT_SECONDS = 5.0


def fleet_tui_enabled(env: Optional[Mapping[str, str]]) -> bool:
    """Return False only when ``CAO_FLEET_TUI`` is exactly ``"0"``.

    Mirrors ``[ "${CAO_FLEET_TUI:-1}" = 0 ]`` from ``fleet-tui-ensure.sh:12``:
    an absent key, an empty value and every other value all enable the window.
    Keeping the comparison this literal is deliberate — a server that disabled
    on ``"false"``/``"no"`` while the shell guards did not would make the two
    halves of the same flag disagree.
    """
    if env is None:
        return True
    return str(env.get(FLEET_TUI_ENV, "1")) != "0"


def _run_tmux(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=TMUX_TIMEOUT_SECONDS,
    )


def _list_windows(session_name: str) -> Optional[List[Tuple[str, str]]]:
    """Return ``(index, name)`` per window, or None if tmux could not answer.

    None and ``[]`` are different answers and callers must not conflate them:
    an empty list would mean "a session with no windows", which cannot happen,
    while None means the inventory is unknown and no window should be created
    on a guess.
    """
    result = _run_tmux(["list-windows", "-t", session_name, "-F", "#{window_index} #{window_name}"])
    if result.returncode != 0:
        logger.debug(
            "fleet window: tmux list-windows failed for %s (rc=%s): %s",
            session_name,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return None
    windows: List[Tuple[str, str]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            windows.append((parts[0], parts[1]))
    return windows


def ensure_fleet_window(
    session_name: str,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Create the session's ``fleet`` window if it is wanted and not there yet.

    Returns True only when this call created the window. Never raises.
    """
    try:
        if not fleet_tui_enabled(env):
            logger.debug("fleet window: %s=0, skipping for %s", FLEET_TUI_ENV, session_name)
            return False

        executable = shutil.which(FLEET_CONSOLE_SCRIPT)
        if executable is None:
            logger.info(
                "fleet window: %s is not on PATH, skipping for %s "
                "(install the 'cli-agent-orchestrator[fleet]' extra to enable it)",
                FLEET_CONSOLE_SCRIPT,
                session_name,
            )
            return False
        # The console script is declared unconditionally in pyproject, so it is
        # on PATH even for a server-only install; the extra is what adds
        # textual. Probing the library rather than the script is what keeps a
        # server without the extra from getting a window that opens only to
        # print an install hint and die.
        if importlib.util.find_spec(FLEET_TUI_MODULE) is None:
            logger.info(
                "fleet window: the 'cli-agent-orchestrator[fleet]' extra is not installed "
                "(no %s), skipping for %s",
                FLEET_TUI_MODULE,
                session_name,
            )
            return False

        windows = _list_windows(session_name)
        if windows is None:
            return False
        if any(name == FLEET_WINDOW_NAME for _, name in windows):
            logger.debug("fleet window: %s already has one, leaving it alone", session_name)
            return False

        occupied = {index for index, _ in windows}
        if str(FLEET_WINDOW_INDEX) in occupied:
            # Index 1 is taken (a worker landed there first). Append rather than
            # renumber: shuffling a live worker's window index to seat the TUI
            # would break every `session:index` reference already handed out.
            target = session_name
        else:
            target = f"{session_name}:{FLEET_WINDOW_INDEX}"

        result = _run_tmux(
            [
                "new-window",
                "-d",
                "-t",
                target,
                "-n",
                FLEET_WINDOW_NAME,
                f"{executable} --session {session_name}",
            ]
        )
        if result.returncode != 0:
            logger.warning(
                "fleet window: tmux new-window failed for %s (rc=%s): %s",
                session_name,
                result.returncode,
                (result.stderr or "").strip(),
            )
            return False
        logger.info("fleet window: created for session %s at %s", session_name, target)
        return True
    except Exception:
        # Deliberately total: this is a convenience surface hanging off session
        # creation, and #473's fix must not turn a TUI problem into a failed
        # session start. exc_info so the cause is still diagnosable from the log.
        logger.warning(
            "fleet window: could not be ensured for session %s", session_name, exc_info=True
        )
        return False
