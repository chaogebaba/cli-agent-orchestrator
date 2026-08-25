"""F254 D10 — FakeBackend: single canonical fake for the TerminalBackend ABC.

Implements the complete TerminalBackend ABC (15 abstract methods) plus
overrides all 9 non-abstract hooks so no base-class default can silently
answer for the fake. Backed by a scriptable per-window screen buffer.

Usage in tests:
    from test.fixtures.fake_backend import FakeBackend
    from cli_agent_orchestrator.backends import registry

    backend = FakeBackend()
    registry.set_backend(backend)
    # ... test code ...
    # The autouse _reset_backend_registry fixture restores on teardown.

Assertion surfaces:
    backend.sent_keys(session, window) -> list[str]
    backend.sent_special_keys(session, window) -> list[str]
    backend.kill_out_of_band(session, window) -> None  # marks window as dead

Scripting:
    backend.script_screen(session, window, frames: list[str]) -> None
    backend.script_cwd(session, window, cwd: str) -> None
    backend.script_command(session, window, cmd: str) -> None

The 9 non-abstract hooks are explicitly overridden with documented safe
no-op defaults (D10 amendment #3). Each override is annotated so AC-B1's
introspection can verify they are NOT inherited from the base class.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Dict, List, Literal, Optional

from cli_agent_orchestrator.backends.base import (
    NativeIdentityResult,
    PaneIdentityReadResult,
    ScopeProbe,
    TerminalBackend,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus


class FakeBackend(TerminalBackend):
    """Complete TerminalBackend fake with scriptable screen buffers.

    All non-abstract hooks return safe no-op values matching the base class's
    documented defaults — but are explicitly overridden here, not inherited,
    so ABC completeness introspection passes (AC-B1).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Per-window state: key = (session_name, window_name)
        self._screens: Dict[tuple, list[str]] = defaultdict(list)
        self._screen_index: Dict[tuple, int] = defaultdict(int)
        self._sent_keys: Dict[tuple, list[str]] = defaultdict(list)
        self._sent_special: Dict[tuple, list[str]] = defaultdict(list)
        self._sessions: Dict[str, set[str]] = {}  # session -> set of windows
        self._dead_windows: set[tuple] = set()
        self._cwds: Dict[tuple, str] = {}
        self._commands: Dict[tuple, str] = {}
        self._pipe_panes: Dict[tuple, Optional[str]] = {}

    # ------------------------------------------------------------------
    # Scripting API (test-facing)
    # ------------------------------------------------------------------

    def script_screen(self, session: str, window: str, frames: list[str]) -> None:
        """Set screen frames for get_history to cycle through."""
        key = (session, window)
        with self._lock:
            self._screens[key] = list(frames)
            self._screen_index[key] = 0

    def script_cwd(self, session: str, window: str, cwd: str) -> None:
        """Set the working directory returned by get_pane_working_directory."""
        self._cwds[(session, window)] = cwd

    def script_command(self, session: str, window: str, cmd: str) -> None:
        """Set the command returned by get_pane_current_command."""
        self._commands[(session, window)] = cmd

    def sent_keys(self, session: str, window: str) -> list[str]:
        """Return all text sent via send_keys (assertion surface)."""
        return list(self._sent_keys[(session, window)])

    def sent_special_keys(self, session: str, window: str) -> list[str]:
        """Return all special keys sent via send_special_key."""
        return list(self._sent_special[(session, window)])

    def kill_out_of_band(self, session: str, window: str) -> None:
        """Mark a window as dead (simulates out-of-band kill for UX-6)."""
        self._dead_windows.add((session, window))

    # ------------------------------------------------------------------
    # Abstract methods (15 total — all implemented)
    # ------------------------------------------------------------------

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        terminal_token: Optional[str] = None,
        allowed_blocked_values: Optional[Dict[str, str]] = None,
    ) -> str:
        with self._lock:
            self._sessions[session_name] = {window_name}
        if working_directory:
            self._cwds[(session_name, window_name)] = working_directory
        return window_name

    def session_exists(self, session_name: str) -> bool:
        return session_name in self._sessions

    def list_sessions(self) -> List[Dict[str, str]]:
        return [
            {"id": name, "name": name, "status": "running"}
            for name in self._sessions
        ]

    def kill_session(self, session_name: str) -> bool:
        if session_name in self._sessions:
            del self._sessions[session_name]
            return True
        return False

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        window_shell: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        terminal_token: Optional[str] = None,
        allowed_blocked_values: Optional[Dict[str, str]] = None,
    ) -> str:
        with self._lock:
            if session_name not in self._sessions:
                self._sessions[session_name] = set()
            self._sessions[session_name].add(window_name)
        if working_directory:
            self._cwds[(session_name, window_name)] = working_directory
        return window_name

    def kill_window(self, session_name: str, window_name: str) -> bool:
        if session_name in self._sessions and window_name in self._sessions[session_name]:
            self._sessions[session_name].discard(window_name)
            return True
        return False

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
    ) -> None:
        self._sent_keys[(session_name, window_name)].append(keys)

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        self._sent_special[(session_name, window_name)].append(key)

    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
    ) -> str:
        key = (session_name, window_name)
        with self._lock:
            frames = self._screens.get(key, [])
            if not frames:
                return ""
            idx = self._screen_index[key]
            result = frames[min(idx, len(frames) - 1)]
            if idx < len(frames) - 1:
                self._screen_index[key] = idx + 1
            return result

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        return self._cwds.get((session_name, window_name), "/tmp")

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        return self._commands.get((session_name, window_name), "mock_cli")

    def attach_session(self, session_name: str) -> None:
        pass  # No-op in fake

    def prepare_web_attach(self, session_name: str, window_name: str) -> List[str]:
        return ["echo", "fake-web-attach"]

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        self._pipe_panes[(session_name, window_name)] = file_path

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        self._pipe_panes.pop((session_name, window_name), None)

    # ------------------------------------------------------------------
    # Non-abstract hooks (9 total — explicitly overridden, never inherited)
    # ------------------------------------------------------------------

    def read_native_identity(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        expected_provider: str,
    ) -> NativeIdentityResult:
        """Override: always unavailable (no real agent process)."""
        return NativeIdentityResult(None, None, "unavailable")

    def window_liveness(self, session_name: str, window_name: str) -> str:
        """Override: returns 'gone' for killed windows, 'live' otherwise."""
        if (session_name, window_name) in self._dead_windows:
            return "gone"
        return "live"

    def enumerate_windows(
        self, session_name: str
    ) -> tuple[Literal["ok", "error"], List[Dict[str, object]] | None]:
        """Override: returns window list from internal state."""
        if session_name not in self._sessions:
            return ("ok", [])
        windows = [
            {"name": w, "id": f"{session_name}:{w}"}
            for w in self._sessions[session_name]
        ]
        return ("ok", windows)

    def session_scope_probe(
        self,
        session_name: str,
        *,
        window_name: str,
        samples: int = 2,
        timeout_s: float = 5.0,
    ) -> ScopeProbe:
        """Override: deterministic scope probe based on internal state."""
        session_present = session_name in self._sessions
        siblings = (
            list(self._sessions[session_name] - {window_name})
            if session_present
            else None
        )
        scope: Literal["window", "session", "unknown"]
        if not session_present:
            scope = "session"
        elif (session_name, window_name) in self._dead_windows:
            scope = "window"
        else:
            scope = "unknown"
        return ScopeProbe(
            scope=scope,
            session_present=session_present,
            sibling_windows=siblings,
            samples=samples,
            evidence=("fake_backend_probe",),
        )

    def get_session_windows(self, session_name: str) -> List[Dict[str, object]]:
        """Override: returns windows from internal state."""
        if session_name not in self._sessions:
            return []
        return [{"name": w} for w in self._sessions[session_name]]

    def capture_viewport(self, session_name: str, window_name: str) -> str:
        """Override: returns current screen frame (same as get_history)."""
        return self.get_history(session_name, window_name)

    def read_pane_identity(self, session_name: str, window_name: str) -> PaneIdentityReadResult:
        """Override: always returns read_error (no real pane)."""
        return PaneIdentityReadResult(reason="fake_backend")

    def get_pane_size(self, session_name: str, window_name: str) -> Optional[tuple]:
        """Override: returns standard 80x24 terminal size."""
        return (80, 24)

    def supports_event_inbox(self) -> bool:
        """Override: no event-based inbox in fake."""
        return False

    def get_pane_id(self, terminal_id: str, session_name: str = "", window_name: str = "") -> str:
        """Override: returns a deterministic pane ID."""
        return f"fake-pane-{terminal_id}"

    def get_native_status(self, session_name: str, window_name: str) -> Optional[TerminalStatus]:
        """Override: no native agent status in fake."""
        return None
