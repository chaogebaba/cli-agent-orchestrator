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
   exceptions and returns ``False``. A missing ``cao-fleet`` binary, a
   multiplexer that will not answer, a backend that cannot enumerate windows
   at all — each is a logged no-op, never a raised error.
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

4. **Every multiplexer operation goes through the backend port** (#786). This
   module used to shell out to ``["tmux", ...]`` directly, which broke three
   ways at once under the herdr backend this deployment actually runs: it
   spawned a tmux process per supervisor session start on a host with no tmux
   in the picture; it bypassed ``utils/tmux_command.tmux_argv``, the one
   socket-aware argv builder, so a sandbox with ``CAO_TMUX_SOCKET`` set silently
   escaped to the HOST tmux server instead of raising
   ``TmuxSocketConfigurationError`` (this is what
   ``test_g7a_sandbox.test_tmux_ast_guard_is_closed`` catches); and, because
   ``tmux list-windows`` cannot answer for a herdr session, the window has been
   silently dead since the herdr flip — with the tail risk that an operator's
   personal tmux session of the same name on the default socket got the window
   instead. ``enumerate_windows`` / ``create_window`` on
   ``backends.registry.get_backend()`` replace both calls. Point 1's "a backend
   that is not tmux at all is a no-op" is now true BY CONSTRUCTION rather than
   by accident: a backend that does not override ``enumerate_windows`` inherits
   ``base.py``'s ``("error", None)`` and this module bails without touching any
   multiplexer. HerdrBackend is in exactly that position today.

``cao-fleet`` is the console script of the optional ``cli-agent-orchestrator[fleet]``
extra. A server-only install carries no TUI library and therefore no such
binary, which is why the resolution step below probes ``PATH`` before asking the
backend anything.
"""

import importlib.util
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import List, Mapping, Optional

from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.backends.registry import get_backend

logger = logging.getLogger(__name__)

FLEET_WINDOW_NAME = "fleet"
"""tmux window name, matching the name the shell launcher used."""

FLEET_TERMINAL_ID = ""
"""The terminal id injected into the fleet pane — deliberately EMPTY (#786).

``create_window`` takes a ``terminal_id`` and every backend forces it into the
new pane's environment unconditionally (``clients/tmux.py`` ``window_env
["CAO_TERMINAL_ID"] = terminal_id``; herdr's ``_build_env_args`` does the same
with ``--env``). The fleet TUI is not a CAO terminal — it has no registry row —
so it must not be handed an id that any consumer could resolve, and the raw
``tmux new-window`` this replaced injected nothing at all.

Empty is the value that reproduces "nothing" for every consumer that exists:

* Every reader of the variable tests it for TRUTH, so ``""`` behaves exactly as
  absent — ``hooks/rewake.py:298`` (``if not os.environ.get(...): return``),
  ``cli/commands/barrier.py:26`` (``owner or os.environ.get(...)``),
  ``services/callback_barrier_service.py:25-27``,
  ``services/identity_verify_service.py:381`` (``... or None``). The two that
  read it positionally reject it anyway: ``utils/session_lookup.py:20`` requires
  a fullmatch of ``[a-f0-9]{8}``, and ``utils/sandbox_guard.bind_pane_identity``
  is never reached for a pane CAO did not register. The root repo's
  ``/orchestrator`` gate is the same shape one level out:
  ``doctrine/compose/orchestrator-md.sh:45`` refuses on
  ``[ -z "${CAO_TERMINAL_ID:-}" ]``, so ``""`` fails it closed while a synthetic
  marker would pass it and only be caught one step later by the fx141 liveness
  round-trip.
* The one sweep that reads pane environments back and matches them against the
  registry — ``terminal_service.purge_stale_terminal_records`` →
  ``backend.read_pane_identity`` — compares ``result.identity == terminal_id``
  over EVERY window of the session. ``""`` can never equal an 8-hex terminal id,
  so the fleet pane is neither a false rename target nor an
  ``IdentityAmbiguousError`` second match, and it is not counted "unreadable"
  either (``missing_env``/empty is not in that reason set).

A clearly-synthetic non-empty marker (``"fleet"``) was rejected: it is equally
unresolvable but it passes the truthiness gates above, so ``rewake`` and the
callback barrier would start work on a bogus principal instead of declining.
Extending the port with an opt-out flag was rejected too — ``HerdrBackend``
overrides ``create_window``, so a new keyword would have to land there in the
same change, and that file belongs to another lane.

Measured (tmux 3.7c): ``new-window -e CAO_TERMINAL_ID=`` leaves the variable
present-but-empty in the pane, ``-e CAO_TERMINAL_ID=abcd1234`` sets it, and no
``-e`` leaves it absent. Present-but-empty is the reachable option and behaves
as absent everywhere above.
"""

FLEET_TUI_ENV = "CAO_FLEET_TUI"
"""Opt-out variable, shared verbatim with the two repo-local shell guards."""

FLEET_CONSOLE_SCRIPT = "cao-fleet"
"""Console script — present on PATH even without the ``[fleet]`` extra."""

FLEET_TUI_MODULE = "textual"
"""The library the ``[fleet]`` extra actually adds; its presence is the test."""


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


def _resolve_console_script() -> Optional[str]:
    """Locate ``cao-fleet``, preferring the venv beside ``sys.executable``.

    ``cao-server`` runs as a systemd user unit whose PATH is the systemd
    default (``/usr/local/bin:/usr/bin``), not the venv's ``bin`` — so a
    ``shutil.which`` alone finds nothing and every server-side fleet window is
    skipped (#633). The console script is installed beside the interpreter that
    runs the server, so probe ``Path(sys.executable).parent / "cao-fleet"``
    first and only fall back to PATH when it is not an executable file there.
    """
    candidate = Path(sys.executable).parent / FLEET_CONSOLE_SCRIPT
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(FLEET_CONSOLE_SCRIPT)


def _window_names(backend: TerminalBackend, session_name: str) -> Optional[List[str]]:
    """Return the session's window names, or None when nothing may be created.

    ``backend.enumerate_windows`` answers with the same three-way distinction
    this function used to hand-roll against ``tmux list-windows``' exit status,
    so the old "never create on a guess" rule maps straight onto it:

    * ``("error", None)`` — the inventory READ failed, so the inventory is
      unknown. This is also what a backend that never implemented
      ``enumerate_windows`` returns from ``base.py``, which is why a non-tmux
      backend is a no-op without this module knowing what a backend is.
    * ``("ok", [])`` — per ``backends/base.py`` the session is genuinely
      ABSENT. It is not "an empty session to fill": a live session with zero
      windows cannot exist, which is exactly why the old code refused to treat
      an empty listing as a green light.
    * ``("ok", [...])`` — a real inventory; decide from the names.
    """
    status, windows = backend.enumerate_windows(session_name)
    if status != "ok" or windows is None:
        logger.debug("fleet window: window inventory unavailable for %s, skipping", session_name)
        return None
    if not windows:
        logger.debug("fleet window: no session named %s, skipping", session_name)
        return None
    return [str(window.get("name", "")) for window in windows]


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

        executable = _resolve_console_script()
        if executable is None:
            logger.info(
                "fleet window: %s is not beside the interpreter or on PATH, skipping for %s "
                "(install the 'cli-agent-orchestrator[fleet]' extra to enable it)",
                FLEET_CONSOLE_SCRIPT,
                session_name,
            )
            return False
        logger.info("fleet window: resolved %s to %s", FLEET_CONSOLE_SCRIPT, executable)
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

        # One resolution for both port calls, so an inventory and the
        # creation that follows it can never straddle two backends.
        backend = get_backend()
        names = _window_names(backend, session_name)
        if names is None:
            return False
        if FLEET_WINDOW_NAME in names:
            logger.debug("fleet window: %s already has one, leaving it alone", session_name)
            return False

        # No index preference is expressed, and none is lost (#786). The port's
        # `create_window` has no index parameter, and it does not need one: the
        # old code targeted `session:1` only when index 1 was free and appended
        # (bare session target) otherwise, and tmux resolves BOTH of those to
        # the lowest free index at or after base-index. Measured on tmux 3.7c
        # with libtmux's exact argv shape (`-t <session>:`, empty index): with
        # only window 0 present the new window lands at 1 — the slot the old
        # code asked for by name — and with 1 occupied it lands at 2, which is
        # what the old append branch produced. The reason the append branch
        # exists at all is unchanged and still honoured: renumbering a live
        # worker to seat the TUI would break every `session:index` reference
        # already handed out, and nothing here renumbers anything.
        #
        # working_directory=None means the backend's default (the cao-server
        # process's cwd). The fleet TUI takes its target as `--session`, so its
        # cwd carries no meaning; the old raw call expressed no preference
        # either.
        try:
            window_name = backend.create_window(
                session_name,
                FLEET_WINDOW_NAME,
                FLEET_TERMINAL_ID,
                window_shell=f"{executable} --session {session_name}",
            )
        except Exception as error:
            # Kept as its own boundary rather than falling through to the
            # module-wide one: a creation refusal is the expected, diagnosable
            # failure (no such session, backend busy) and deserves its own line,
            # not an exc_info dump under "could not be ensured".
            logger.warning(
                "fleet window: backend create_window failed for %s: %s", session_name, error
            )
            return False
        logger.info("fleet window: created for session %s as %s", session_name, window_name)
        return True
    except Exception:
        # Deliberately total: this is a convenience surface hanging off session
        # creation, and #473's fix must not turn a TUI problem into a failed
        # session start. exc_info so the cause is still diagnosable from the log.
        logger.warning(
            "fleet window: could not be ensured for session %s", session_name, exc_info=True
        )
        return False
