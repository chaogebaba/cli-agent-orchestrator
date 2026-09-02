"""One-exec tmux pane lookups (perf side lane, 2026-09-02).

libtmux re-queries the tmux server on every attribute access, so the
``session -> window -> pane -> cmd`` chain in :class:`TmuxClient` costs FOUR
tmux client forks per read (``list-sessions``, ``list-windows``,
``list-panes``, then the real command). The pipe-liveness watchdog (every 4 s
per terminal), the pane-liveness sampler (every 5 s per terminal) and the
codex/cline ``pane_current_command`` status probe (every output chunk) all go
through that chain. Measured live on 2026-09-02 with 11 worker terminals,
those three lookup execs were ~70% of all tmux forks (636 of ~900 per 30 s).

This module resolves ``(session_name, window_name)`` to pane ids with ONE
``list-panes -a`` exec, caches the mapping, and runs the real command as a
tmux *command sequence* whose first command prints the target pane's
identity. A cached id that no longer denotes the expected pane (window
recreated, tmux server restarted and ids reused, active pane changed) is
therefore detected inside the same exec, the entry is re-resolved once, and
the command is retried once.

Contract — the fast path NEVER claims absence and NEVER raises:
every public method returns ``None`` whenever it cannot answer with
certainty (tmux binary missing, server down, target absent, ambiguous
window names, identity mismatch after the one refresh, unparseable output,
kill switch). Callers MUST then fall back to the legacy libtmux path, which
owns the not-found / parse-race semantics (``ValueError`` message shapes,
``TmuxLookupError``, log levels). A successful command with empty output
returns ``[]``, which is a real answer, not a fallback signal.

Kill switch: ``CAO_TMUX_FAST_LOOKUP=0`` disables the fast path entirely.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from cli_agent_orchestrator.utils.tmux_command import tmux_argv

logger = logging.getLogger(__name__)

# ASCII unit separator: never appears in tmux names/ids in practice, and a
# name that did contain it simply fails the identity check -> fallback.
_SEP = "\x1f"
_LIST_FMT = _SEP.join(
    (
        "#{session_name}",
        "#{window_id}",
        "#{window_index}",
        "#{window_name}",
        "#{pane_index}",
        "#{pane_id}",
        "#{pane_active}",
    )
)
_IDENT_FMT = _SEP.join(
    ("#{session_name}", "#{window_name}", "#{pane_id}", "#{pane_active}", "#{pane_index}")
)

# tmux client round-trips are milliseconds; anything longer means a wedged
# server, and the caller's legacy path (with its own logging) should decide.
_EXEC_TIMEOUT_S = 5.0

_ENV_KILL_SWITCH = "CAO_TMUX_FAST_LOOKUP"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def fast_lookup_enabled() -> bool:
    """``CAO_TMUX_FAST_LOOKUP`` unset / ``1`` / ``true`` -> on; ``0`` / ``false`` / ``off`` -> off."""
    raw = os.environ.get(_ENV_KILL_SWITCH, "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class WindowPanes:
    """Pane ids for one ``(session, window)``; ``first_pane`` is the lowest ``pane_index``."""

    window_id: str
    window_index: int
    first_pane: str
    active_pane: Optional[str]
    # pane_index of ``first_pane`` at resolution time. ``swap-pane`` /
    # ``rotate-window`` reorder indexes while keeping pane ids, which would
    # otherwise let a cached id pass the identity check while no longer being
    # libtmux's ``window.panes[0]`` (gate r2 blocker 3).
    first_pane_index: int = 0


class PaneLocator:
    """Cache of ``(session_name, window_name) -> WindowPanes`` with identity-checked execution."""

    def __init__(self, runner: Optional[Runner] = None) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[Tuple[str, str], WindowPanes] = {}
        # session_name -> [(window_index, window_id, window_name)], duplicates kept.
        self._windows: Dict[str, List[Tuple[int, str, str]]] = {}
        self._run: Runner = runner or subprocess.run

    # ── exec plumbing ────────────────────────────────────────────────────

    def _exec(self, args: List[str]) -> Optional["subprocess.CompletedProcess[str]"]:
        try:
            argv = tmux_argv(*args)
        except Exception:  # TmuxSocketConfigurationError and friends: not ours to judge
            return None
        try:
            completed = self._run(
                argv,
                capture_output=True,
                text=True,
                errors="backslashreplace",
                timeout=_EXEC_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if not isinstance(getattr(completed, "stdout", None), str):
            return None
        if not isinstance(getattr(completed, "returncode", None), int):
            return None
        return completed

    @staticmethod
    def _split_stdout(stdout: str) -> List[str]:
        """Mirror libtmux ``tmux_cmd``: split on newlines, drop trailing empties only."""
        lines = stdout.split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        return lines

    # ── inventory ────────────────────────────────────────────────────────

    def refresh(self) -> Optional[Dict[Tuple[str, str], WindowPanes]]:
        """Re-read every pane of every session in one exec; replaces the cache.

        Returns the new mapping, or ``None`` if tmux could not be read. Windows
        whose ``(session, name)`` is not unique are left OUT of the mapping so
        the legacy path (which raises on ambiguity) keeps owning that case.
        """
        completed = self._exec(["list-panes", "-a", "-F", _LIST_FMT])
        if completed is None or completed.returncode != 0:
            return None
        # (session, window_id) -> [index, name, [(pane_index, pane_id, active)]]
        windows: Dict[Tuple[str, str], Tuple[int, str, List[Tuple[int, str, bool]]]] = {}
        for line in self._split_stdout(completed.stdout):
            parts = line.split(_SEP)
            if len(parts) != 7:
                return None
            session, window_id, window_index, window_name, pane_index, pane_id, active = parts
            try:
                w_idx = int(window_index)
                p_idx = int(pane_index)
            except ValueError:
                return None
            if not pane_id.startswith("%") or not window_id.startswith("@"):
                return None
            entry = windows.setdefault((session, window_id), (w_idx, window_name, []))
            entry[2].append((p_idx, pane_id, active == "1"))

        by_name: Dict[Tuple[str, str], List[WindowPanes]] = {}
        by_session: Dict[str, List[Tuple[int, str, str]]] = {}
        for (session, window_id), (w_idx, window_name, panes) in windows.items():
            panes.sort()
            by_session.setdefault(session, []).append((w_idx, window_id, window_name))
            active_pane = next((pid for _idx, pid, is_active in panes if is_active), None)
            by_name.setdefault((session, window_name), []).append(
                WindowPanes(
                    window_id=window_id,
                    window_index=w_idx,
                    first_pane=panes[0][1],
                    active_pane=active_pane,
                    first_pane_index=panes[0][0],
                )
            )
        mapping = {key: entries[0] for key, entries in by_name.items() if len(entries) == 1}
        with self._lock:
            self._cache = mapping
            self._windows = by_session
        return mapping

    def resolve(self, session_name: str, window_name: str) -> Optional[WindowPanes]:
        key = (session_name, window_name)
        with self._lock:
            entry = self._cache.get(key)
        if entry is not None:
            return entry
        mapping = self.refresh()
        return mapping.get(key) if mapping else None

    def forget(self, session_name: str, window_name: Optional[str] = None) -> None:
        """Drop cached ids for one window, or every window of a session."""
        with self._lock:
            if window_name is not None:
                self._cache.pop((session_name, window_name), None)
            else:
                for key in [k for k in self._cache if k[0] == session_name]:
                    self._cache.pop(key, None)

    # ── commands ─────────────────────────────────────────────────────────

    def run_pane_command(
        self,
        session_name: str,
        window_name: str,
        command: str,
        *args: str,
        pane: str = "first",
    ) -> Optional[List[str]]:
        """Run ``command -t <pane> *args`` in ONE exec, identity-checked.

        ``pane`` is ``"first"`` (lowest pane index — libtmux ``window.panes[0]``;
        the identity line must still report that index, so a reordered window
        re-resolves) or ``"active"`` (libtmux ``window.active_pane``; the
        identity line must still report the pane as active). Returns the command's
        stdout lines (libtmux ``tmux_cmd.stdout`` semantics) or ``None`` when
        the caller must fall back.
        """
        if not fast_lookup_enabled():
            return None
        key = (session_name, window_name)
        for attempt in range(2):
            if attempt == 0:
                entry = self.resolve(session_name, window_name)
            else:
                mapping = self.refresh()
                entry = mapping.get(key) if mapping else None
            if entry is None:
                return None
            pane_id = entry.first_pane if pane == "first" else entry.active_pane
            if pane_id is None:
                return None
            completed = self._exec(
                [
                    "display-message",
                    "-t",
                    pane_id,
                    "-p",
                    _IDENT_FMT,
                    ";",
                    command,
                    "-t",
                    pane_id,
                    *args,
                ]
            )
            if completed is None:
                return None
            lines = self._split_stdout(completed.stdout)
            ident = lines[0].split(_SEP) if lines else []
            identity_ok = (
                len(ident) == 5
                and ident[0] == session_name
                and ident[1] == window_name
                and ident[2] == pane_id
                and (pane != "active" or ident[3] == "1")
                and (pane != "first" or ident[4] == str(entry.first_pane_index))
            )
            if identity_ok and completed.returncode == 0:
                return lines[1:]
            # Stale id (window recreated / server restarted / focus moved) or the
            # command itself failed: drop the entry and retry once from a fresh
            # inventory. A second miss hands the case to the legacy path.
            self.forget(session_name, window_name)
        return None

    def session_windows(self, session_name: str) -> Optional[List[Dict[str, str]]]:
        """``[{"name", "index"}]`` for a session from one fresh inventory read.

        Returns ``None`` when tmux could not be read OR the session is absent —
        absence is the legacy path's call to make.
        """
        if not fast_lookup_enabled():
            return None
        if self.refresh() is None:
            return None
        with self._lock:
            rows = sorted(self._windows.get(session_name, ()))
        if not rows:
            return None
        return [{"name": name, "index": str(index)} for index, _wid, name in rows]


# Module-level singleton shared by every TmuxClient instance (the cache is
# keyed on names, which are global to the one tmux server we talk to).
pane_locator = PaneLocator()
