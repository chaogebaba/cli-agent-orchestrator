"""F862 (#718) — ``chatgpt_web`` provider: launches AND drives the runner (r3).

D2: the provider is lifecycle and status; the ``chatgpt_web_runner`` package does
framing, hashing, browser control and publication. The r2 gate found the runner
built but UNWIRED — ``initialize`` only allow-listed and waited for a shell, and a
normal ``send_input`` pasted the task into the bare shell. r3 wires the ONE
production path:

* :meth:`initialize` acquires the :class:`ProfileLock` review lease (D5,
  owner-only stop) and LAUNCHES the runner (``python -m
  cli_agent_orchestrator.chatgpt_web_runner --task-file <path>``) in the worker's
  tmux pane, with the display + profile env it needs, then waits for the runner's
  ``[chatgpt_web] READY`` marker.
* The provider OWNS its dispatch (``handles_own_dispatch``): :meth:`dispatch_task`
  writes the orchestrated task to the runner's task file, so the task is NEVER
  pasted into the shell. The pane runner consumes it and drives the D2 anchor
  sequence as the worker (its pane env carries ``CAO_TERMINAL_ID`` /
  ``CAO_TERMINAL_TOKEN``), calling back worker-scoped.
* :meth:`cleanup` releases the lease.

D14 (findings-only): a provider-side ALLOW-LIST backstop refuses any position
outside ``design_findings`` / ``general`` in :meth:`initialize`, before any lease
or launch. NOT keyed on ``_is_gate_position`` (D15 makes that true for
``design_findings``). The server dispatch guard is the primary control.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)

#: The two cells this provider is certified for (D11/D14). The backstop is an
#: ALLOW-LIST over this set — NOT the ``_is_gate_position`` predicate (D14).
CHATGPT_WEB_ALLOWED_POSITIONS: frozenset[str] = frozenset({"design_findings", "general"})

#: Per-worker runtime root (task file + logs). Under /data scratch, never /tmp.
_RUNTIME_ROOT = Path("/data/cao-scratch/worker-scratch/f862-build/runtime")

#: The runner's structured pane markers drive the 6-value TerminalStatus.
_RUNNER_READY = re.compile(r"^\s*\[chatgpt_web\]\s+READY\b", re.MULTILINE)
_RUNNER_WORKING = re.compile(r"^\s*\[chatgpt_web\]\s+RUNNING\b", re.MULTILINE)
_RUNNER_DONE = re.compile(r"^\s*\[chatgpt_web\]\s+(FINDINGS-READY|DONE)\b", re.MULTILINE)
_RUNNER_INVALID = re.compile(r"^\s*\[chatgpt_web\]\s+FINDINGS-INVALID\b", re.MULTILINE)
_RUNNER_ERROR = re.compile(r"^\s*\[chatgpt_web\]\s+ERROR\b", re.MULTILINE)
_RUNNER_WAIT = re.compile(r"^\s*\[chatgpt_web\]\s+WAIT_USER\b", re.MULTILINE)


class ChatGptWebProvider(BaseProvider):
    """Lifecycle/status provider that launches AND drives the runner (D2/D13/D14)."""

    supports_fork_context: bool = False
    supports_resume: bool = False
    declared_capabilities = {
        "fork": False,
        "resume": False,
        "capture": False,
        "artifact_locate": False,
    }
    condition_provider_key: str | None = ProviderType.CHATGPT_WEB.value

    # F862 r3: the provider owns its dispatch — send_input routes the task to
    # dispatch_task instead of pasting it into the shell (r2 Blocker 1).
    handles_own_dispatch: bool = True

    #: Display/profile env the runner needs to drive the headful browser. The
    #: worker pane inherits cao-server's env; these are set explicitly so the
    #: browser launches regardless of how cao-server itself was started.
    _BROWSER_ENV: dict[str, str] = {
        "DISPLAY": ":0",
        "WAYLAND_DISPLAY": "wayland-0",
        "XAUTHORITY": "/run/user/1000/.mutter-Xwaylandauth.K8YAV3",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "C2C_DRIVER_PROFILE": "/data/cao-scratch/chatgpt-web/profile",
        "CLOAKBROWSER_CACHE_DIR": "/data/cao-scratch/chatgpt-web/.cloakbrowser",
    }

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
        fork_context: Optional["ForkContext"] = None,
    ) -> None:
        super().__init__(
            terminal_id, session_name, window_name, allowed_tools, skill_prompt, fork_context
        )
        self._agent_profile = agent_profile
        self._model = model
        self._initialized = False
        self._processing_seen = False
        self._lock: Any = None  # ProfileLock, held for the worker's lifetime (D5)
        self._runtime_dir = _RUNTIME_ROOT / terminal_id
        self._task_file = self._runtime_dir / "task.txt"

    @property
    def resolved_model(self) -> Optional[str]:
        return self._model

    @property
    def paste_enter_count(self) -> int:
        return 1

    # ── D14 backstop ────────────────────────────────────────────────────────

    def _assert_position_allowed(self) -> None:
        position = _resolve_position_name(self._agent_profile)
        if position not in CHATGPT_WEB_ALLOWED_POSITIONS:
            raise PermissionError(
                f"chatgpt_web refuses position {position!r}: certified only for "
                f"{sorted(CHATGPT_WEB_ALLOWED_POSITIONS)} (F862 D14 backstop)"
            )

    # ── Launch command ────────────────────────────────────────────────────────

    def _build_launch_command(self) -> str:
        """The shell command that launches the pane runner (D2/D13).

        Exports the display/profile env, then runs the runner module reading its
        task from the per-worker task file. The pane already carries the worker
        identity (CAO_TERMINAL_ID/TOKEN via create_terminal), so the runner acts
        as the worker.
        """
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in self._BROWSER_ENV.items())
        py = "python3"
        module = "cli_agent_orchestrator.chatgpt_web_runner"
        return f"{env_prefix} {py} -m {module} --task-file {shlex.quote(str(self._task_file))}"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        """Backstop the position, acquire the review lease, launch the runner (D5/D2)."""
        self._assert_position_allowed()

        from cli_agent_orchestrator.chatgpt_web_runner.runtime import (
            ProfileLock,
            resolve_profile_dir,
        )
        from cli_agent_orchestrator.utils.terminal import wait_for_shell

        if not await wait_for_shell(self.terminal_id, timeout=60):
            raise TimeoutError("shell initialization timed out")

        # D5: acquire the one-browser-per-profile review lease (owner lease). The
        # lock is held for this worker's whole lifetime and released in cleanup.
        profile = resolve_profile_dir()
        self._lock = ProfileLock(profile, self.terminal_id)
        self._lock.acquire(queue_deadline_s=300.0)
        logger.info("chatgpt_web %s acquired review lease on %s", self.terminal_id, profile)

        # Clear any stale task file, then launch the runner in the pane.
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._task_file.unlink()
        except OSError:
            pass
        from cli_agent_orchestrator.backends.registry import get_backend

        command = self._build_launch_command()
        get_backend().send_keys(self.session_name, self.window_name, command)
        self._initialized = True
        return True

    def dispatch_task(self, message: str) -> None:
        """Write the orchestrated task to the runner's task file (F862 r3).

        The pane runner is watching this path; writing it (atomically) hands the
        task to the runner WITHOUT the shell ever seeing it. The message is the
        dispatch body verbatim (its ``ARTIFACT:``/``BUNDLE:`` header + prompt are
        parsed by the runner). Written under the dispatch transaction by
        ``send_input`` (F862 r3).
        """
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._task_file.with_suffix(".txt.tmp")
        tmp.write_text(message, encoding="utf-8")
        os.replace(str(tmp), str(self._task_file))
        logger.info("chatgpt_web %s wrote task file %s", self.terminal_id, self._task_file)

    def mark_input_received(self) -> None:
        super().mark_input_received()
        self._processing_seen = False

    def get_status(self, buffer: str) -> TerminalStatus:
        """Parse run state from the runner's structured pane markers (D4)."""
        native = self._resolve_native_status(buffer)
        if native is not None:
            return native
        if not self._initialized:
            return TerminalStatus.UNKNOWN
        if not buffer or not buffer.strip():
            return TerminalStatus.UNKNOWN
        if _RUNNER_WAIT.search(buffer):
            return TerminalStatus.WAITING_USER_ANSWER
        if _RUNNER_ERROR.search(buffer) or _RUNNER_INVALID.search(buffer):
            # FINDINGS-INVALID is a terminal ERROR for the seat: the model's body
            # failed the D10 schema and was never promoted (r3 item 4).
            return TerminalStatus.ERROR
        if _RUNNER_WORKING.search(buffer):
            self._processing_seen = True
            return TerminalStatus.PROCESSING
        if _RUNNER_DONE.search(buffer):
            if self._task_dispatched:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE
        if _RUNNER_READY.search(buffer):
            # Launched and waiting for a task: IDLE until a task is dispatched,
            # COMPLETED only once a task has been through.
            return TerminalStatus.IDLE
        return TerminalStatus.UNKNOWN

    def extract_last_message_from_script(self, script_output: str) -> str:
        raise ValueError("chatgpt_web publishes via send_message; no pane message to extract")

    def exit_cli(self) -> str:
        return "C-d"

    def cleanup(self, *, preserve_session: bool = False) -> None:
        """Release the review lease (owner-only stop, D5) and reset state."""
        self._initialized = False
        self._processing_seen = False
        if self._lock is not None:
            try:
                self._lock.release()
            except Exception:  # pragma: no cover - best-effort release
                logger.warning("chatgpt_web %s lease release failed", self.terminal_id)
            self._lock = None
        return None


def _resolve_position_name(agent_profile: Optional[str]) -> str:
    """Derive the position from a composed profile name or bare position."""
    if not agent_profile:
        return ""
    name = agent_profile
    suffix = f"-{ProviderType.CHATGPT_WEB.value}"
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    return name
