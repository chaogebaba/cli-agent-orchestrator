"""Grok CLI provider implementation.

This provider drives the interactive Grok Build TUI in tmux. Probe P1 showed
that ``--minimal`` is the most scrapeable rendering mode: completed turns are
printed into normal scrollback and the pinned prompt/footer remains compact.

Profile prompt delivery uses ``--system-prompt-override`` rather than
``--agent`` because a live headless probe did not confirm that ad-hoc agent
definition bodies are applied consistently. The launch still passes ``-m`` when
the CAO profile pins a model.
"""

import logging
import re
import shlex
from pathlib import Path
from typing import List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_server_settings,
)
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.grok_config import ensure_grok_mcp_servers
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

GROK_BINARY = str(Path.home() / ".grok" / "bin" / "grok")

IDLE_PROMPT_PATTERN = r"^\s*❯\s*$|^\s*❯\s+\S.*$"
USER_PROMPT_PATTERN = r"^\s*❯\s+(.+)$"
PROCESSING_PATTERN = (
    r"Waiting for response…"
    r"|Waiting for response\.\.\."
    r"|^\s*(?:⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏)?\s*(?:Thinking|Responding)\b"
    r"| - (?:Waiting for response|Thinking|Responding) - "
)
COMPLETION_PATTERN = r"Turn completed in [\d.]+s\."
WAITING_USER_ANSWER_PATTERN = (
    r"Run Grok Build in a project directory\?"
    r"|↑/↓ navigate"
    r"|Enter:submit"
)
ERROR_PATTERN = (
    r"^\s*(?:"
    r"Error:\s+.+"
    r"|ERROR:\s+.+"
    r"|Grok(?: Build)? (?:error|failed):\s+.+"
    r"|Authentication required\b.*"
    r"|Rate limit(?:ed| exceeded)?\b.*"
    r"|Failed to (?:authenticate|load|connect|initialize|start)\b.*"
    r")$"
)

FOOTER_HINT_PATTERN = (
    r"(?:\balways-approve\b|ctrl\+o transcript|Shift\+Tab:mode|Ctrl\+x:shortcuts)"
)
IDLE_FOOTER_PATTERN = FOOTER_HINT_PATTERN
EMPTY_DRAFT_PLACEHOLDERS = {
    "",
}


class ProviderError(Exception):
    """Exception raised for Grok provider-specific errors."""


class GrokCliProvider(BaseProvider):
    """Provider for Grok Build's interactive CLI."""

    supports_screen_detection = True
    supports_draft_preservation = True
    composer_clear_keys = ["C-a", "C-k"]

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._input_received = False
        self._agent_profile = agent_profile

    @property
    def paste_enter_count(self) -> int:
        """Grok submits bracketed-pasted input with one Enter."""
        return 1

    @property
    def paste_submit_delay(self) -> float:
        """P1 verified 0.3s is enough for Grok's simple prompt editor."""
        return 0.3

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        return True

    @property
    def extraction_tail_lines(self) -> int:
        return 2000

    def mark_input_received(self) -> None:
        super().mark_input_received()
        self._input_received = True

    def _load_profile(self):
        if self._agent_profile is None:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except FileNotFoundError:
            logger.debug(
                "Grok profile '%s' not found; launching without profile",
                self._agent_profile,
            )
            return None
        except Exception as exc:
            raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {exc}")

    def _build_grok_command(self) -> str:
        profile = self._load_profile()
        command_parts = [GROK_BINARY, "--always-approve", "--minimal"]

        if profile and profile.mcpServers:
            ensure_grok_mcp_servers(profile.mcpServers)

        provider_defaults = get_provider_defaults("grok_cli")
        default_model = provider_defaults.get("model")
        if "model" in provider_defaults and isinstance(default_model, str):
            model = default_model or None
        elif profile and profile.model:
            model = profile.model
        else:
            model = None
        if isinstance(model, str) and model:
            command_parts.extend(["-m", model])

        system_prompt = profile.system_prompt if profile and profile.system_prompt else ""
        system_prompt = self._apply_skill_prompt(system_prompt)
        if system_prompt:
            command_parts.extend(["--system-prompt-override", system_prompt])

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        """Start Grok and wait for the prompt/footer to become interactive."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        command = self._build_grok_command()
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=float(get_server_settings()["provider_init_timeout"]),
            polling_interval=1.0,
        ):
            raise TimeoutError(f"Grok CLI initialization timed out after {init_timeout}s")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        """Detect Grok status from the raw tmux pipe-pane byte stream."""
        native = self._resolve_native_status()
        if native is not None:
            return native

        if not output:
            return TerminalStatus.UNKNOWN

        clean_output = strip_terminal_escapes(output)
        if not clean_output.strip():
            return TerminalStatus.UNKNOWN

        if re.search(WAITING_USER_ANSWER_PATTERN, clean_output, re.MULTILINE):
            return TerminalStatus.WAITING_USER_ANSWER

        last_processing = self._last_match(PROCESSING_PATTERN, clean_output)
        last_completed = self._last_match(COMPLETION_PATTERN, clean_output)
        last_idle = self._last_idle_match(clean_output)
        tail = "\n".join(clean_output.splitlines()[-12:])

        if self._has_error_after_last_completion(clean_output, last_completed):
            return TerminalStatus.ERROR

        if last_processing and (
            last_completed is None or last_completed.start() < last_processing.start()
        ):
            return TerminalStatus.PROCESSING

        if last_completed and last_idle and last_idle.start() > last_completed.end():
            return TerminalStatus.COMPLETED

        if self._input_received and last_completed:
            return TerminalStatus.COMPLETED

        if last_idle or re.search(IDLE_FOOTER_PATTERN, tail):
            return TerminalStatus.IDLE

        if self._initialized and self.shell_baseline:
            current_cmd = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
            if current_cmd == self.shell_baseline:
                return TerminalStatus.IDLE

        return TerminalStatus.PROCESSING

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        rows = [line.rstrip() for line in screen_lines]
        joined = "\n".join(rows)
        if not joined.strip():
            return TerminalStatus.UNKNOWN

        if re.search(WAITING_USER_ANSWER_PATTERN, joined, re.MULTILINE):
            return TerminalStatus.WAITING_USER_ANSWER

        bottom = "\n".join(rows[-12:])
        if re.search(PROCESSING_PATTERN, bottom, re.MULTILINE):
            return TerminalStatus.PROCESSING

        if self._has_error_after_last_completion(
            joined,
            self._last_match(COMPLETION_PATTERN, joined),
        ):
            return TerminalStatus.ERROR

        if re.search(COMPLETION_PATTERN, joined) and re.search(IDLE_FOOTER_PATTERN, bottom):
            return TerminalStatus.COMPLETED

        if re.search(IDLE_FOOTER_PATTERN, bottom) or self.read_composer_draft(rows) is not None:
            return TerminalStatus.IDLE

        return TerminalStatus.UNKNOWN

    def read_composer_draft(self, screen_lines: List[str]) -> Optional[str]:
        """Read Grok's visible bottom composer draft from a rendered screen."""
        visible = [line.rstrip() for line in screen_lines]
        if not visible:
            return None

        footer_idx = len(visible)
        for idx in range(len(visible) - 1, -1, -1):
            if re.search(FOOTER_HINT_PATTERN, visible[idx]):
                footer_idx = idx
                break
        if footer_idx == len(visible):
            return None

        lower_bound = max(0, footer_idx - 8)
        prompt_idx = None
        for idx in range(footer_idx - 1, lower_bound - 1, -1):
            if re.match(r"^\s*(?:│\s*)?❯(?:\s|$)", visible[idx]):
                prompt_idx = idx
                break
        if prompt_idx is None:
            return None

        line = visible[prompt_idx]
        prompt_pos = line.rfind("❯")
        draft = line[prompt_pos + 1 :].strip()
        if draft in EMPTY_DRAFT_PLACEHOLDERS:
            return ""
        return draft

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last completed Grok response from captured scrollback."""
        clean_output = strip_terminal_escapes(script_output)
        lines = clean_output.splitlines()

        completion_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            if re.search(COMPLETION_PATTERN, lines[idx]):
                completion_idx = idx
                break
        if completion_idx is None:
            raise ValueError("No Grok response found - no completion marker detected")

        prompt_idx = None
        for idx in range(completion_idx - 1, -1, -1):
            if re.match(USER_PROMPT_PATTERN, lines[idx]):
                prompt_idx = idx
                break
        if prompt_idx is None:
            raise ValueError("No Grok response found - no user prompt before completion")

        content = lines[prompt_idx + 1 : completion_idx]
        filtered = []
        for line in content:
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r"^[◆⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]?\s*(Thought|Thinking|Responding)\b", stripped):
                continue
            if re.search(r"\b\d+(?:\.\d+)?s\s+[⇣↑]\S+", stripped):
                continue
            filtered.append(stripped)

        final_answer = "\n".join(filtered).strip()
        if not final_answer:
            raise ValueError("Empty Grok response - no content found")
        return final_answer

    @staticmethod
    def _last_match(pattern: str, text: str) -> Optional[re.Match[str]]:
        last = None
        for match in re.finditer(pattern, text, re.MULTILINE):
            last = match
        return last

    @staticmethod
    def _last_idle_match(text: str) -> Optional[re.Match[str]]:
        last = None
        for match in re.finditer(IDLE_PROMPT_PATTERN, text, re.MULTILINE):
            suffix = text[match.end() : match.end() + 500]
            if re.search(r"^\s*❯\s*$", match.group(0)) or re.search(
                FOOTER_HINT_PATTERN, suffix
            ):
                last = match
        return last

    @staticmethod
    def _has_error_after_last_completion(
        text: str,
        last_completed: Optional[re.Match[str]],
    ) -> bool:
        scan_text = text[last_completed.end() :] if last_completed else text
        return re.search(ERROR_PATTERN, scan_text, re.IGNORECASE | re.MULTILINE) is not None

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        self._initialized = False
