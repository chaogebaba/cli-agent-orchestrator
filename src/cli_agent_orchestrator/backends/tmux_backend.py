"""TmuxBackend — concrete TerminalBackend implementation wrapping TmuxClient.

This backend delegates all operations to the existing TmuxClient, preserving
identical behavior for all callers. It serves as the default backend when
no alternative is configured.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

from cli_agent_orchestrator.backends.base import (
    PaneIdentityReadResult,
    TerminalBackend,
    TerminalBackendError,
)
from cli_agent_orchestrator.clients.tmux import TmuxClient
from cli_agent_orchestrator.utils.tmux_command import tmux_argv

logger = logging.getLogger(__name__)


class TmuxBackend(TerminalBackend):
    """TerminalBackend implementation backed by tmux via TmuxClient."""

    supports_identity_readback = True

    def __init__(self, client: Optional[TmuxClient] = None) -> None:
        """Initialize with an optional TmuxClient (defaults to module singleton)."""
        if client is None:
            from cli_agent_orchestrator.clients.tmux import tmux_client

            client = tmux_client
        self._client = client

    # --- Session lifecycle ---

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        try:
            return self._client.create_session(
                session_name, window_name, terminal_id, working_directory, extra_env=extra_env
            )
        except Exception as e:
            raise TerminalBackendError(f"Failed to create session '{session_name}': {e}") from e

    def session_exists(self, session_name: str) -> bool:
        return self._client.session_exists(session_name)

    def list_sessions(self) -> List[Dict[str, str]]:
        return self._client.list_sessions()

    def kill_session(self, session_name: str) -> bool:
        return self._client.kill_session(session_name)

    # --- Window/tab lifecycle ---

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        window_shell: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        try:
            return self._client.create_window(
                session_name,
                window_name,
                terminal_id,
                working_directory,
                window_shell,
                extra_env=extra_env,
            )
        except Exception as e:
            raise TerminalBackendError(
                f"Failed to create window '{window_name}' in session '{session_name}': {e}"
            ) from e

    def kill_window(self, session_name: str, window_name: str) -> bool:
        return self._client.kill_window(session_name, window_name)

    def window_liveness(self, session_name: str, window_name: str) -> str:
        import subprocess

        proc = subprocess.run(
            tmux_argv("list-windows", "-t", session_name, "-F", "#{window_name}"),
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return "live" if window_name in proc.stdout.splitlines() else "gone"
        stderr = proc.stderr.lower()
        if "can't find session" in stderr or "no server running" in stderr:
            return "gone"
        return "error"

    # --- Input ---

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
    ) -> None:
        self._client.send_keys(
            session_name,
            window_name,
            keys,
            enter_count=enter_count,
            force_bracketed_paste=force_bracketed_paste,
            submit_delay=submit_delay,
        )

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        self._client.send_special_key(session_name, window_name, key)

    # --- Output ---

    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
    ) -> str:
        return self._client.get_history(
            session_name,
            window_name,
            tail_lines=tail_lines,
            strip_escapes=strip_escapes,
            full_history=full_history,
        )

    def capture_viewport(self, session_name: str, window_name: str) -> str:
        return self._client.capture_viewport(session_name, window_name)

    @staticmethod
    def _pane_pids(session_name: str, window_name: str) -> list[int]:
        import subprocess

        result = subprocess.run(
            tmux_argv("list-panes", "-t", f"{session_name}:{window_name}", "-F", "#{pane_pid}"),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or "tmux list-panes failed")
        return [int(value) for value in result.stdout.splitlines() if value.strip()]

    @staticmethod
    def _proc_starttime(pid: int) -> str:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = value[value.rfind(")") + 2 :].split()
        return tail[19]

    @staticmethod
    def _proc_identity(pid: int) -> str | None:
        data = Path(f"/proc/{pid}/environ").read_bytes()
        prefix = b"CAO_TERMINAL_ID="
        for item in data.split(b"\0"):
            if item.startswith(prefix):
                return item[len(prefix) :].decode("utf-8")
        return None

    def read_pane_identity(self, session_name: str, window_name: str) -> PaneIdentityReadResult:
        try:
            first = self._pane_pids(session_name, window_name)
        except (OSError, ValueError):
            return PaneIdentityReadResult(reason="read_error")
        if len(first) != 1:
            return PaneIdentityReadResult(reason="pane_cardinality")
        pid = first[0]
        try:
            birth = self._proc_starttime(pid)
            identity = self._proc_identity(pid)
            second = self._pane_pids(session_name, window_name)
            if len(second) != 1 or second[0] != pid or self._proc_starttime(pid) != birth:
                return PaneIdentityReadResult(reason="incarnation_changed")
        except OSError:
            return PaneIdentityReadResult(reason="read_error")
        except (IndexError, ValueError, UnicodeError):
            return PaneIdentityReadResult(reason="read_error")
        if identity is None:
            return PaneIdentityReadResult(reason="missing_env")
        return PaneIdentityReadResult(identity=identity)

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        return self._client.get_pane_working_directory(session_name, window_name)

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        return self._client.get_pane_current_command(session_name, window_name)

    def get_pane_size(self, session_name: str, window_name: str) -> Optional[tuple]:
        return self._client.get_pane_size(session_name, window_name)

    # --- Attach ---

    def attach_session(self, session_name: str) -> None:
        """Attach to tmux session via subprocess (replaces current process)."""
        import subprocess

        subprocess.run(tmux_argv("attach-session", "-t", session_name), check=True)

    # --- Pipe-pane ---

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        self._client.pipe_pane(session_name, window_name, file_path)

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        self._client.stop_pipe_pane(session_name, window_name)
