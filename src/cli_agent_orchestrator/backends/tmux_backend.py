"""TmuxBackend — concrete TerminalBackend implementation wrapping TmuxClient.

This backend delegates all operations to the existing TmuxClient, preserving
identical behavior for all callers. It serves as the default backend when
no alternative is configured.
"""

import logging
from pathlib import Path
from typing import Dict, List, Literal, Optional

from cli_agent_orchestrator.backends.base import (
    PaneIdentityReadResult,
    ScopeProbe,
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
        terminal_token: Optional[str] = None,
        allowed_blocked_values: Optional[Dict[str, str]] = None,
    ) -> str:
        try:
            return self._client.create_session(
                session_name, window_name, terminal_id, working_directory, extra_env=extra_env, terminal_token=terminal_token, allowed_blocked_values=allowed_blocked_values
            )
        except Exception as e:
            raise TerminalBackendError(f"Failed to create session '{session_name}': {e}") from e

    def session_exists(self, session_name: str) -> bool:
        return self._client.session_exists(session_name)

    def session_exists_strict(self, session_name: str) -> bool:
        return self._client.session_exists_strict(session_name)

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
        terminal_token: Optional[str] = None,
        allowed_blocked_values: Optional[Dict[str, str]] = None,
    ) -> str:
        try:
            return self._client.create_window(
                session_name,
                window_name,
                terminal_id,
                working_directory,
                window_shell,
                extra_env=extra_env,
                terminal_token=terminal_token,
                allowed_blocked_values=allowed_blocked_values,
            )
        except Exception as e:
            raise TerminalBackendError(
                f"Failed to create window '{window_name}' in session '{session_name}': {e}"
            ) from e

    def kill_window(self, session_name: str, window_name: str) -> bool:
        # F469: park attached clients viewing this window on a stable window
        # before killing, so tmux doesn't yank them to an arbitrary adjacent one.
        self._park_clients_off_window(session_name, window_name)
        return self._client.kill_window(session_name, window_name)

    # ── F469 client-parking helper ───────────────────────────────────────

    def _park_clients_off_window(self, session_name: str, window_name: str) -> None:
        """Move clients currently viewing *window_name* to a stable window.

        Best-effort: any tmux error in the parking step is swallowed so that
        the subsequent kill is never blocked by a parking failure.
        """
        import subprocess

        try:
            # Discover which window index/id corresponds to window_name
            target_idx = self._resolve_window_index(session_name, window_name)
            if target_idx is None:
                logger.debug("F469: _resolve_window_index returned None for %s:%s",
                             session_name, window_name)
                return  # window doesn't exist or can't be resolved — nothing to park

            # List clients attached to this session whose current window matches
            clients_on_target = self._clients_on_window(session_name, target_idx)
            if not clients_on_target:
                logger.debug("F469: no clients on window idx=%s for %s:%s",
                             target_idx, session_name, window_name)
                return  # no client viewing this window — fast path

            # Park each client on a stable window.
            logger.debug("F469: parking %d client(s) off %s:%s (idx=%s)",
                         len(clients_on_target), session_name, window_name, target_idx)
            for client_tty in clients_on_target:
                self._park_single_client(session_name, client_tty, target_idx)
        except Exception as exc:
            # Graceful degradation: parking must never block the kill.
            logger.debug(
                "F469: failed to park clients off %s:%s (non-fatal, proceeding to kill): %s",
                session_name,
                window_name,
                exc,
            )

    def _resolve_window_index(self, session_name: str, window_name: str) -> str | None:
        """Return the window_index of *window_name* within *session_name*, or None."""
        import subprocess

        proc = subprocess.run(
            tmux_argv(
                "list-windows", "-t", session_name,
                "-F", "#{window_name}\t#{window_index}",
            ),
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            logger.debug(
                "F469: list-windows failed for %s (rc=%d, stderr=%s)",
                session_name, proc.returncode, proc.stderr.strip(),
            )
            return None
        for line in proc.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0] == window_name:
                return parts[1]
        logger.debug(
            "F469: window %r not found in list-windows output for %s: %r",
            window_name, session_name, proc.stdout,
        )
        return None

    def _clients_on_window(self, session_name: str, window_index: str) -> list[str]:
        """Return client tty names whose current window matches *window_index*."""
        import subprocess

        proc = subprocess.run(
            tmux_argv(
                "list-clients", "-t", session_name,
                "-F", "#{client_tty}\t#{window_index}",
            ),
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return []
        result = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[1] == window_index:
                result.append(parts[0])
        return result

    def _park_single_client(
        self, session_name: str, client_tty: str, killed_index: str
    ) -> None:
        """Move one client to a stable window via switch-client. Best-effort."""
        import subprocess

        try:
            # Try window 0 first (supervisor seat — predictable landing)
            if killed_index != "0":
                proc = subprocess.run(
                    tmux_argv(
                        "switch-client", "-c", client_tty,
                        "-t", f"{session_name}:0",
                    ),
                    capture_output=True, text=True, timeout=3,
                )
                if proc.returncode == 0:
                    return  # parked on supervisor seat

            # Fallback: find any surviving window that isn't the target
            surviving = self._any_surviving_window(session_name, killed_index)
            if surviving is not None:
                subprocess.run(
                    tmux_argv(
                        "switch-client", "-c", client_tty,
                        "-t", f"{session_name}:{surviving}",
                    ),
                    capture_output=True, text=True, timeout=3,
                )
        except Exception:
            pass  # best-effort — never block the kill

    def _any_surviving_window(
        self, session_name: str, killed_index: str
    ) -> str | None:
        """Return the index of any window that isn't the one being killed."""
        import subprocess

        proc = subprocess.run(
            tmux_argv(
                "list-windows", "-t", session_name,
                "-F", "#{window_index}",
            ),
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            idx = line.strip()
            if idx and idx != killed_index:
                return idx
        return None

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

    def enumerate_windows(
        self, session_name: str
    ) -> tuple[Literal["ok", "error"], List[Dict[str, object]] | None]:
        """Enumerate windows via subprocess. Classifies its own failure."""
        import subprocess

        try:
            proc = subprocess.run(
                tmux_argv("list-windows", "-t", session_name, "-F", "#{window_name}"),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                windows: List[Dict[str, object]] = [
                    {"name": name} for name in proc.stdout.splitlines() if name
                ]
                return ("ok", windows)
            stderr = proc.stderr.lower()
            if "can't find session" in stderr or "no server running" in stderr:
                return ("ok", [])  # genuinely absent — not a failure
            return ("error", None)  # unclassifiable
        except subprocess.TimeoutExpired:
            return ("error", None)
        except Exception:
            return ("error", None)

    def session_scope_probe(
        self,
        session_name: str,
        *,
        window_name: str,
        samples: int = 2,
        timeout_s: float = 5.0,
    ) -> ScopeProbe:
        """F218-a D2: Classify scope via parse-free _has_session_via_cli + enumerate_windows.

        Verdict table:
        - session present AND enumeration ok → window_gone (with sibling list)
        - has_session is False on ``samples`` consecutive probes → session_gone
        - has_session is None, enumeration ("error", None), or disagreement → unknown
        """
        import time

        evidence_lines: list[str] = []
        false_count = 0

        for i in range(samples):
            if i > 0:
                time.sleep(min(timeout_s / samples, 1.0))

            has = self._client._has_session_via_cli(session_name)
            evidence_lines.append(f"has_session[{i}]={has}")

            if has is True:
                # Session present — classify as window_gone via enumeration
                status, windows = self.enumerate_windows(session_name)
                if status == "ok":
                    sibling_names = tuple(
                        w.get("name", "") for w in (windows or [])
                        if w.get("name") != window_name
                    )
                    evidence_lines.append(
                        f"enumerate[{i}]=ok siblings={len(sibling_names)}"
                    )
                    return ScopeProbe(
                        scope="window_gone",
                        session_present=True,
                        sibling_windows=sibling_names,
                        samples=i + 1,
                        evidence=tuple(evidence_lines),
                    )
                else:
                    evidence_lines.append(f"enumerate[{i}]=error")
                    # Disagreement: has_session=True but can't enumerate — unknown
                    return ScopeProbe(
                        scope="unknown",
                        session_present=True,
                        sibling_windows=None,
                        samples=i + 1,
                        evidence=tuple(evidence_lines),
                    )
            elif has is False:
                false_count += 1
            else:
                # None = could not ask — unknown
                evidence_lines.append(f"probe_unavailable[{i}]")
                return ScopeProbe(
                    scope="unknown",
                    session_present=None,
                    sibling_windows=None,
                    samples=i + 1,
                    evidence=tuple(evidence_lines),
                )

        # All samples returned False — session genuinely gone
        if false_count >= samples:
            return ScopeProbe(
                scope="session_gone",
                session_present=False,
                sibling_windows=(),
                samples=samples,
                evidence=tuple(evidence_lines),
            )

        # Should not reach here, but safety fallback
        return ScopeProbe(
            scope="unknown",
            session_present=None,
            sibling_windows=None,
            samples=samples,
            evidence=tuple(evidence_lines),
        )

    def get_session_windows(self, session_name: str) -> List[Dict[str, object]]:
        return self._client.get_session_windows(session_name)

    def set_window_parent(self, session_name: str, window_name: str, parent_id: str | None) -> None:
        self._client.set_window_parent(session_name, window_name, parent_id)

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
            if len(second) != 1:
                return PaneIdentityReadResult(reason="pane_cardinality")
            if second[0] != pid or self._proc_starttime(pid) != birth:
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

    def prepare_web_attach(self, session_name: str, window_name: str) -> List[str]:
        """Return the tmux command used by the browser PTY WebSocket."""
        return tmux_argv("-u", "attach-session", "-t", f"{session_name}:{window_name}")

    # --- Pipe-pane ---

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        self._client.pipe_pane(session_name, window_name, file_path)

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        self._client.stop_pipe_pane(session_name, window_name)
