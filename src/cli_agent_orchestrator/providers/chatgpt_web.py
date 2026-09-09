"""F862 (#718) — thin ``chatgpt_web`` provider over the deterministic runner.

D2: the provider is LIFECYCLE AND STATUS ONLY. The ``chatgpt_web_runner`` package
does framing, hashing, browser control and publication; a CAO-assigned worker
owns identity, report and callback. The provider launches the runner in its tmux
pane (like ``pi_cli`` launches Pi), injects the worker MCP identity, parses run
state from the pane, and maps typed runner errors onto the condition plane (D4).

D14 (the findings-only component): a provider-side ALLOW-LIST backstop refuses
inside :meth:`initialize` whenever the resolved position is not one of the two
cells this provider is certified for — ``design_findings`` and the constrained
``general`` — BEFORE any browser launch. This is defence in depth for callers
reaching the provider off the routing path; it is deliberately NOT keyed on
``_is_gate_position`` (D15 makes that predicate TRUE for ``design_findings``, so a
deny-list would refuse this lane's own dispatch). The dispatch guard in
``server.py`` is the primary, zero-cost control (D14).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)

#: The two cells this provider is certified for (D11/D14). The backstop is an
#: ALLOW-LIST over this set — NOT the ``_is_gate_position`` predicate (D14).
CHATGPT_WEB_ALLOWED_POSITIONS: frozenset[str] = frozenset({"design_findings", "general"})

#: Idle/completed/working chrome the runner prints to its pane. The runner is a
#: line-oriented process (not a full-screen TUI); status is parsed from its
#: structured status lines rather than from browser state.
_RUNNER_WORKING = re.compile(r"^\s*\[chatgpt_web\]\s+RUNNING\b", re.MULTILINE)
_RUNNER_DONE = re.compile(r"^\s*\[chatgpt_web\]\s+(FINDINGS-READY|DONE)\b", re.MULTILINE)
_RUNNER_ERROR = re.compile(r"^\s*\[chatgpt_web\]\s+ERROR\b", re.MULTILINE)
_RUNNER_WAIT = re.compile(r"^\s*\[chatgpt_web\]\s+WAIT_USER\b", re.MULTILINE)


class ChatGptWebProvider(BaseProvider):
    """Lifecycle/status shim for the ChatGPT-web findings lane (D2/D13/D14).

    NEVER forks or resumes: a review is one active turn on a fresh conversation
    (D9). Declares no capture/artifact_locate — an interrupted review has no
    reusable verdict and is re-dispatched cold (D4).
    """

    supports_fork_context: bool = False
    supports_resume: bool = False
    declared_capabilities = {
        "fork": False,
        "resume": False,
        "capture": False,
        "artifact_locate": False,
    }

    # F611 condition plane: this provider routes typed runner errors into the
    # shared condition classifier under its own key (D4).
    condition_provider_key: str | None = ProviderType.CHATGPT_WEB.value

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

    @property
    def resolved_model(self) -> Optional[str]:
        # The model is fixed by the account/menu (GPT-5.6 Sol / High); the
        # AUTHORITATIVE slug is read from node metadata by the runner (D6).
        return self._model

    @property
    def paste_enter_count(self) -> int:
        return 1

    # ── D14 backstop ────────────────────────────────────────────────────────

    def _assert_position_allowed(self) -> None:
        """Refuse any position outside the certified allow-list, before launch.

        The resolved position is derived from the composed profile name
        (``<position>-chatgpt_web`` or ``general-chatgpt_web``) or the bare
        position. On a name that resolves to neither certified cell, refuse with
        a typed error and NO browser launch (D14/AC-3).
        """
        position = _resolve_position_name(self._agent_profile)
        if position not in CHATGPT_WEB_ALLOWED_POSITIONS:
            raise PermissionError(
                f"chatgpt_web refuses position {position!r}: certified only for "
                f"{sorted(CHATGPT_WEB_ALLOWED_POSITIONS)} (F862 D14 backstop)"
            )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        """Backstop the position, then wait for the shell (D2/D14).

        The browser turn itself is driven by the runner process; the provider
        only prepares the pane and injects worker identity. The allow-list check
        runs FIRST so a refused position never reaches a browser launch.
        """
        self._assert_position_allowed()
        from cli_agent_orchestrator.utils.terminal import wait_for_shell

        if not await wait_for_shell(self.terminal_id, timeout=60):
            raise TimeoutError("shell initialization timed out")
        self._initialized = True
        return True

    def mark_input_received(self) -> None:
        super().mark_input_received()
        self._processing_seen = False

    def get_status(self, buffer: str) -> TerminalStatus:
        """Parse run state from the runner's structured pane lines.

        The runner prints ``[chatgpt_web] RUNNING|FINDINGS-READY|ERROR|WAIT_USER``
        markers; these drive the 6-value ``TerminalStatus``. Conditions (CAPPED,
        AUTH_EXPIRED, …) ride the separate condition plane via
        ``classify_condition`` (D4), never a ``TerminalStatus`` member.
        """
        native = self._resolve_native_status(buffer)
        if native is not None:
            return native
        if not self._initialized:
            return TerminalStatus.UNKNOWN
        if not buffer or not buffer.strip():
            return TerminalStatus.UNKNOWN
        if _RUNNER_WAIT.search(buffer):
            return TerminalStatus.WAITING_USER_ANSWER
        if _RUNNER_ERROR.search(buffer):
            return TerminalStatus.ERROR
        if _RUNNER_WORKING.search(buffer):
            self._processing_seen = True
            return TerminalStatus.PROCESSING
        if _RUNNER_DONE.search(buffer):
            if self._task_dispatched:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE
        return TerminalStatus.UNKNOWN

    def extract_last_message_from_script(self, script_output: str) -> str:
        """The report is published to disk and delivered via send_message (D2/D10);
        the pane carries only status markers, so there is no last-message body to
        extract here. Raise so callers use the inbox anchor, not pane text."""
        raise ValueError("chatgpt_web publishes via send_message; no pane message to extract")

    def exit_cli(self) -> str:
        """Ctrl-C interrupts the runner; Ctrl-D returns the shell to baseline."""
        return "C-d"

    def cleanup(self) -> None:
        self._initialized = False
        self._processing_seen = False
        return None


def _resolve_position_name(agent_profile: Optional[str]) -> str:
    """Derive the position from a composed profile name or bare position.

    Accepts ``<position>-chatgpt_web`` (the composed spawn name), ``general-<p>``,
    or a bare ``<position>``. Returns the position segment.
    """
    if not agent_profile:
        return ""
    name = agent_profile
    suffix = f"-{ProviderType.CHATGPT_WEB.value}"
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    return name
