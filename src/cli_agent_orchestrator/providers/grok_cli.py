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
import os
import re
import shlex
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import ForkContext, TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider, RetryableArtifactValidation
from cli_agent_orchestrator.providers.screen_classification import (
    ScreenClassificationResult,
    ScreenSignal,
    screen_classification_result,
)
from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_provider_profile_defaults,
    get_server_settings,
    resolve_provider_string_option,
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
    r"|^[^\S\r\n]*(?:⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏)[^\S\r\n]+\S"
    r"| - (?:Waiting for response|Thinking|Responding) - "
)
COMPLETION_PATTERN = r"^\s*(?:Turn completed in [\d.]+s\.|Worked for [\d.]+s\.)\s*$"
RUNNING_PATTERN = r"^\s*Worked for [\d.]+s\.\s+\d+ commands? still running\.\s*$"
WAITING_USER_ANSWER_PATTERN = (
    r"Run Grok Build in a project directory\?" r"|↑/↓ navigate" r"|Enter:submit"
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

FOOTER_HINT_PATTERN = r"(?:\balways-approve\b|ctrl\+o transcript|Shift\+Tab:mode|Ctrl\+x:shortcuts)"
IDLE_FOOTER_PATTERN = FOOTER_HINT_PATTERN
COMPOSER_PROMPT_PATTERN = r"^\s*(?:│\s*)?❯(?:\s|$)"
EMPTY_DRAFT_PLACEHOLDERS = {
    "",
}


class ProviderError(Exception):
    """Exception raised for Grok provider-specific errors."""


class GrokCliProvider(BaseProvider):
    supports_fork_context = True
    supports_reauth_rebind = True
    """Provider for Grok Build's interactive CLI."""

    supports_screen_detection = True
    supports_draft_preservation = True
    composer_clear_keys = ["C-a", "C-k"]
    clear_immune_ghosts = False

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
        fork_context: Optional[ForkContext] = None,
    ):
        super().__init__(
            terminal_id, session_name, window_name, allowed_tools, skill_prompt, fork_context
        )
        self.allocated_session_uuid = (
            None
            if fork_context and fork_context.mode == "resume"
            else self._allocate_session_uuid()
        )
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
            ensure_grok_mcp_servers(profile.mcpServers, terminal_id=self.terminal_id)

        provider_defaults = get_provider_defaults("grok_cli")
        profile_name = getattr(profile, "name", None) or self._agent_profile
        profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)
        model = resolve_provider_string_option(
            profile_defaults,
            provider_defaults,
            profile,
            "model",
            "model",
        )
        if isinstance(model, str) and model:
            command_parts.extend(["-m", model])

        reasoning_effort = resolve_provider_string_option(
            profile_defaults,
            provider_defaults,
            profile,
            "reasoning_effort",
            "reasoningEffort",
        )
        if isinstance(reasoning_effort, str) and reasoning_effort:
            command_parts.extend(["--reasoning-effort", reasoning_effort])

        system_prompt = profile.system_prompt if profile and profile.system_prompt else ""
        system_prompt = self._apply_skill_prompt(system_prompt)
        if system_prompt:
            command_parts.extend(["--system-prompt-override", system_prompt])

        if self._fork_context:
            if self._fork_context.mode == "resume":
                command_parts.extend(["--resume", self._fork_context.session_uuid])
            else:
                command_parts.extend(
                    [
                        "--resume",
                        self._fork_context.session_uuid,
                        "--fork-session",
                        "--session-id",
                        self.allocated_session_uuid,
                    ]
                )
        else:
            command_parts.extend(["--session-id", self.allocated_session_uuid])

        return shlex.join(command_parts)

    def _allocate_session_uuid(self) -> str:
        try:
            cwd = (
                get_backend().get_pane_working_directory(self.session_name, self.window_name)
                or os.getcwd()
            )
        except Exception:
            cwd = os.getcwd()
        root = Path.home() / ".grok" / "sessions" / quote(cwd, safe="")
        for _ in range(2):
            value = str(uuid.uuid4())
            if not (root / value).exists():
                return value
        raise ProviderError("session_uuid_collision")

    def build_fork_command(self, session_uuid: str, new_session_uuid: Optional[str]) -> list[str]:
        old_context, old_uuid = self._fork_context, self.allocated_session_uuid
        self._fork_context = ForkContext(
            mode="fork",
            session_uuid=session_uuid,
            base_name="base",
            provider="grok_cli",
            initial_preamble="",
        )
        self.allocated_session_uuid = new_session_uuid or self._allocate_session_uuid()
        try:
            return shlex.split(self._build_grok_command())
        finally:
            self._fork_context, self.allocated_session_uuid = old_context, old_uuid

    def build_resume_command(self, session_uuid: str) -> list[str]:
        old_context = self._fork_context
        self._fork_context = ForkContext(
            mode="resume",
            session_uuid=session_uuid,
            base_name="base",
            provider="grok_cli",
            initial_preamble="",
        )
        try:
            return shlex.split(self._build_grok_command())
        finally:
            self._fork_context = old_context

    def capture_session_uuid(self, pane_pid: int, launch_time: float, cwd: str) -> str:
        if not self.allocated_session_uuid:
            raise ProviderError("base_session_unset")
        return self.allocated_session_uuid

    def resume_session_uuid(self) -> str | None:
        if self._fork_context and self._fork_context.mode == "resume":
            return self._fork_context.session_uuid
        return None

    def validate_session_artifact(self, session_uuid: str, cwd: str) -> None:
        path = (
            Path.home()
            / ".grok"
            / "sessions"
            / quote(cwd, safe="")
            / session_uuid
            / "chat_history.jsonl"
        )
        if not path.is_file() or path.stat().st_size == 0:
            raise RetryableArtifactValidation("session_artifact_missing_or_inert")

    def provider_process_started_at(self, pane_pid: int) -> float | None:
        from cli_agent_orchestrator.services.fork_context_service import _descendants

        matches = []
        for pid in _descendants(pane_pid):
            try:
                if b"grok" in Path(f"/proc/{pid}/cmdline").read_bytes():
                    matches.append(pid)
            except OSError:
                pass
        if len(matches) != 1:
            return None
        stat = Path(f"/proc/{matches[0]}/stat").read_text().split()
        btime = next(
            float(x.split()[1])
            for x in Path("/proc/stat").read_text().splitlines()
            if x.startswith("btime ")
        )
        return btime + float(stat[21]) / os.sysconf(os.sysconf_names["SC_CLK_TCK"])

    async def initialize(
        self,
        *,
        coordinates: tuple[str, str] | None = None,
        provider_override=None,
        raw_status: bool = False,
    ) -> bool:
        """Start Grok and wait for the prompt/footer to become interactive."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = get_server_settings()["provider_init_timeout"]
        shell_kwargs = {"timeout": init_timeout}
        if coordinates is not None:
            shell_kwargs["coordinates"] = coordinates
        if not await wait_for_shell(self.terminal_id, **shell_kwargs):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        command = self._build_grok_command()
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        status_kwargs = dict(
            timeout=float(get_server_settings()["provider_init_timeout"]),
            polling_interval=1.0,
        )
        if provider_override is not None or raw_status:
            status_kwargs.update(provider_override=provider_override, raw_status=raw_status)
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            **status_kwargs,
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
        last_running = self._last_match(RUNNING_PATTERN, clean_output)
        last_completed = self._last_match(COMPLETION_PATTERN, clean_output)
        last_idle = self._last_idle_match(clean_output)
        tail = "\n".join(clean_output.splitlines()[-12:])

        if self._has_error_after_last_completion(clean_output, last_completed):
            return TerminalStatus.ERROR

        if last_processing and (
            last_completed is None or last_completed.start() < last_processing.start()
        ):
            return TerminalStatus.PROCESSING

        if last_running and (
            last_completed is None or last_completed.start() < last_running.start()
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

    def classify_screen(self, screen_lines: List[str]) -> ScreenClassificationResult:
        rows = [line.rstrip() for line in screen_lines]
        while rows and not rows[-1].strip():
            rows.pop()
        signals: List[ScreenSignal] = []
        completion_rows = [
            index for index, row in enumerate(rows) if re.search(COMPLETION_PATTERN, row)
        ]
        newest_completion = max(completion_rows, default=-1)
        for index, row in enumerate(rows):
            if re.search(WAITING_USER_ANSWER_PATTERN, row):
                signals.append(ScreenSignal("waiting", "WAITING_USER_ANSWER_PATTERN", index))
            if re.search(PROCESSING_PATTERN, row):
                if "Waiting for response" in row:
                    signals.append(
                        ScreenSignal("progress", "PROCESSING_PATTERN", index, row, "exempt")
                    )
                else:
                    signals.append(
                        ScreenSignal("progress", "PROCESSING_PATTERN", index, row, "corroborable")
                    )
            if re.search(RUNNING_PATTERN, row):
                signals.append(
                    ScreenSignal("progress", "RUNNING_PATTERN", index, row, "exempt")
                )
            if re.search(COMPLETION_PATTERN, row):
                signals.append(ScreenSignal("completion", "COMPLETION_PATTERN", index))
            # Grok errors are effective only after the newest completion.
            if index > newest_completion and re.search(ERROR_PATTERN, row):
                signals.append(ScreenSignal("error", "ERROR_PATTERN", index))
            if re.search(IDLE_PROMPT_PATTERN, row):
                signals.append(ScreenSignal("chrome", "IDLE_PROMPT_PATTERN", index))
            if re.search(IDLE_FOOTER_PATTERN, row):
                signals.append(ScreenSignal("chrome", "IDLE_FOOTER_PATTERN", index))
            if re.search(COMPOSER_PROMPT_PATTERN, row):
                signals.append(ScreenSignal("chrome", "COMPOSER_PROMPT_PATTERN", index))
        return screen_classification_result(signals)

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        return self.classify_screen(screen_lines).status

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
            if re.match(COMPOSER_PROMPT_PATTERN, visible[idx]):
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
            if re.search(r"^\s*❯\s*$", match.group(0)) or re.search(FOOTER_HINT_PATTERN, suffix):
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
