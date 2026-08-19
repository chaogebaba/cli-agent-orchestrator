"""Cline CLI provider implementation.

This provider drives Cline CLI (v3.0.55+) in interactive TUI mode inside tmux.
Cline is launched with ``--tui --auto-approve true`` for headless orchestration;
CAO submits messages via tmux send-keys and detects terminal state from pane
output patterns.

Key flags (verified via ``cline --help`` and live probes, 2026-08-19):
  -i / --tui           : interactive TUI mode (persistent session)
  --auto-approve true  : auto-approve all tool calls (yolo)
  -c <path>            : working directory
  -m <model-id>        : model override (format: provider/model-id)
  -P <provider>        : API provider (default: cline)
  -s <system-prompt>   : system prompt override
  --timeout <seconds>  : per-run timeout
  --retries <n>        : max consecutive retries
  --id <session-id>    : resume session
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_provider_profile_defaults,
    get_server_settings,
    resolve_provider_string_option,
)
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

CLINE_BINARY = str(Path.home() / ".bun" / "bin" / "cline")

# ─── Status detection patterns ───────────────────────────────────────────────

# Cline TUI idle prompt: the input indicator at the end of output.
# Confirmed via cline/cline source: the TUI renders a ❯ glyph as the
# brand.prompt character (themeable) plus placeholder text in the composer
# when idle.  Two observed idle screens:
#   Virgin (first launch):   ❯ What can I do for you?
#   Post-exchange:           ❯ Ask anything...
# Both are prompt-glyph + placeholder ON THE SAME LINE.  A banner line
# "What can I do for you?" can also appear ABOVE the composer but must NOT
# count as idle on its own (can co-exist with a processing state).
IDLE_PROMPT_PATTERN = r"^\s*[❯>]\s*$"

# Composer-line idle: prompt glyph followed by a known placeholder phrase.
# This is the PRIMARY idle signal — it matches the actual rendered composer.
IDLE_COMPOSER_PATTERN = r"^\s*[❯>]\s+(?:What can I do for you\??|Ask anything.*)"

# Legacy placeholder search (matches "Ask anything" anywhere in a line).
# Kept as a secondary fallback for edge-case screen truncations.
IDLE_PLACEHOLDER_PATTERN = r"Ask anything"

# Processing indicators: spinner/working text.
PROCESSING_PATTERN = (
    r"Thinking\.\.\."
    r"|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏"
    r"|Working\.\.\."
    r"|Running\.\.\."
)

# Token/cost summary (marks end of a turn).
TOKEN_COST_PATTERN = r"(?:Tokens?|Cost|Usage):\s*[\d.,]+"

# Error indicators.
ERROR_PATTERN = (
    r"^\s*(?:"
    r"[Ee]rror:\s+.+"
    r"|ERROR:\s+.+"
    r"|Failed to .+"
    r"|Rate limit(?:ed| exceeded)\b.*"
    r")$"
)

# Waiting for user confirmation (permission/trust dialogs).
WAITING_USER_ANSWER_PATTERN = (
    r"\[\s*y\s*/\s*n\s*\]"
    r"|Do you want to"
    r"|Press .+ to continue"
)


class ClineCliProvider(BaseProvider):
    """Provider for Cline CLI interactive TUI.

    Launches cline in TUI mode (``--tui``) with auto-approve and detects
    terminal state by pattern-matching pane output.
    """

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._input_received = False
        self._agent_profile = agent_profile
        self._model = model

    @property
    def resolved_model(self) -> Optional[str]:
        """Return the effective model resolved during command build."""
        return getattr(self, "_resolved_model", None)

    @property
    def paste_enter_count(self) -> int:
        """Cline TUI submits with one Enter after paste."""
        return 1

    @property
    def paste_submit_delay(self) -> float:
        """Delay after paste before Enter."""
        return 0.3

    def _after_dispatch_commit_locked(self) -> None:
        self._input_received = True

    def _resolve_model(self) -> Optional[str]:
        """Resolve model: spawn override > providers.toml > profile field.

        Follows the same resolution chain as other providers: explicit model
        kwarg (from assign/handoff) > [cline_cli.profiles.<name>] model >
        [cline_cli] model > profile.model field.
        """
        if self._model:
            return self._model
        profile = None
        try:
            if self._agent_profile:
                profile = load_agent_profile(self._agent_profile)
        except (FileNotFoundError, RuntimeError) as exc:
            logger.debug(
                "Profile '%s' not loadable; falling back to providers.toml: %s",
                self._agent_profile,
                exc,
            )
        provider_defaults = get_provider_defaults("cline_cli")
        profile_name = getattr(profile, "name", None) or self._agent_profile
        profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)
        return resolve_provider_string_option(
            profile_defaults,
            provider_defaults,
            profile,
            "model",
            "model",
        )

    def _resolve_provider_id(self) -> Optional[str]:
        """Resolve the Cline API provider id (e.g. 'cline-pass').

        Resolution: providers.toml [cline_cli] api_provider > default 'cline-pass'.
        The 'cline-pass' provider routes through ClinePass (free DeepSeek V4 Flash),
        while 'cline' routes through OpenRouter (paid per-token).
        """
        provider_defaults = get_provider_defaults("cline_cli")
        return provider_defaults.get("api_provider") or "cline-pass"

    def _resolve_thinking(self) -> Optional[str]:
        """Resolve reasoning effort level for --thinking flag.

        Resolution: [cline_cli.profiles.<name>] thinking > [cline_cli] thinking >
        profile.reasoningEffort field > default 'high'.
        Valid values: none|low|medium|high|xhigh (per cline --help).

        An explicit empty string ("") in providers.toml suppresses the flag
        entirely (falls back to Cline's own provider default).
        """
        profile = None
        try:
            if self._agent_profile:
                profile = load_agent_profile(self._agent_profile)
        except (FileNotFoundError, RuntimeError):
            pass
        provider_defaults = get_provider_defaults("cline_cli")
        profile_name = getattr(profile, "name", None) or self._agent_profile
        profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)
        resolved = resolve_provider_string_option(
            profile_defaults,
            provider_defaults,
            profile,
            "thinking",
            "reasoningEffort",
        )
        # Explicit empty string = suppress the flag (return "").
        if isinstance(resolved, str):
            return resolved
        # No key present anywhere → default to high reasoning effort.
        return "high"

    def _build_command(self) -> str:
        """Build the cline CLI launch command for interactive TUI mode."""
        command_parts = [CLINE_BINARY, "--tui", "--auto-approve", "true"]

        # API provider selection (cline-pass = ClinePass subscription, free).
        api_provider = self._resolve_provider_id()
        if api_provider:
            command_parts.extend(["-P", api_provider])

        model = self._resolve_model()
        self._resolved_model = model if (isinstance(model, str) and model) else None
        if isinstance(model, str) and model:
            command_parts.extend(["-m", model])

        # Reasoning effort (default: high).
        thinking = self._resolve_thinking()
        if isinstance(thinking, str) and thinking:
            command_parts.extend(["--thinking", thinking])

        # System prompt from agent profile.
        profile = None
        if self._agent_profile:
            try:
                profile = load_agent_profile(self._agent_profile)
            except (FileNotFoundError, RuntimeError):
                pass

        system_prompt = profile.system_prompt if profile and profile.system_prompt else ""
        system_prompt = self._apply_skill_prompt(system_prompt)
        if system_prompt:
            command_parts.extend(["-s", system_prompt])

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        """Start Cline TUI and wait for the idle prompt."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        command = self._build_command()
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=float(init_timeout),
            polling_interval=1.0,
        ):
            raise TimeoutError(f"Cline CLI initialization timed out after {init_timeout}s")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        """Detect Cline terminal state from raw output."""
        native = self._resolve_native_status()
        if native is not None:
            return native

        if not output:
            return TerminalStatus.UNKNOWN

        clean_output = strip_terminal_escapes(output)
        if not clean_output.strip():
            return TerminalStatus.UNKNOWN

        lines = clean_output.splitlines()
        tail = "\n".join(lines[-20:])
        idle_detected = self._has_idle_prompt(lines)

        # Check for waiting/permission prompts.
        if re.search(WAITING_USER_ANSWER_PATTERN, tail, re.IGNORECASE):
            return TerminalStatus.WAITING_USER_ANSWER

        # Check for errors — but NOT when the idle prompt is also visible
        # (the error text is likely a quoted line in the response, not a
        # real terminal error). Priority-inversion guard (S1).
        if not idle_detected and re.search(ERROR_PATTERN, tail, re.MULTILINE | re.IGNORECASE):
            return TerminalStatus.ERROR

        # Check for active processing.
        if re.search(PROCESSING_PATTERN, tail):
            return TerminalStatus.PROCESSING

        # Check for idle prompt at end.
        if idle_detected:
            if self._input_received:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        # Fallback: if shell baseline matches, terminal may have exited.
        if self._initialized and self.shell_baseline:
            current_cmd = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
            if current_cmd == self.shell_baseline:
                return TerminalStatus.IDLE

        return TerminalStatus.PROCESSING

    @staticmethod
    def _has_idle_prompt(lines: list[str]) -> bool:
        """Check if idle prompt is visible in the last few lines.

        Cline's TUI shows the composer line in one of these forms:
        - Virgin idle:       ❯ What can I do for you?
        - Post-exchange:     ❯ Ask anything...
        - Bare (rare/old):   ❯

        The real TUI layout has STATUS BAR lines BELOW the composer:
          ────────────────
          ❯ Ask anything...
          ────────────────
          ClinePass: DeepSeek V4 Flash (high) ...
          cli-subagents (quirks-merge-train)
          ⏵⏵ Auto-approve all enabled (Shift+Tab)

        So we must scan ALL non-empty lines in the 8-line tail window
        (not stop at the first non-matching line from the bottom).

        A bare "What can I do for you?" banner line (no ❯ glyph) does NOT
        count — it co-exists with processing state and must not short-circuit.
        """
        tail = lines[-8:] if len(lines) >= 8 else lines
        for line in tail:
            stripped = line.strip()
            if not stripped:
                continue
            # Primary: composer line with glyph + placeholder text.
            if re.match(IDLE_COMPOSER_PATTERN, stripped):
                return True
            # Fallback: bare prompt glyph only.
            if re.match(IDLE_PROMPT_PATTERN, stripped):
                return True
            # Secondary fallback: "Ask anything" anywhere in line (handles
            # rare cases where escape stripping removes the glyph).
            if re.search(IDLE_PLACEHOLDER_PATTERN, stripped, re.IGNORECASE):
                return True
        return False

    def classify_injection_hazard(self, rows: list[str]) -> str | None:
        return (
            "interactive_dialog"
            if self.get_status_from_screen(rows) == TerminalStatus.WAITING_USER_ANSWER
            else None
        )

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last Cline response from captured scrollback."""
        clean_output = strip_terminal_escapes(script_output)
        lines = clean_output.splitlines()

        # Find the last user input line (prompt followed by content).
        user_line_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            stripped = lines[idx].strip()
            if re.match(r"^[❯>]\s+\S", stripped):
                user_line_idx = idx
                break

        if user_line_idx is None:
            raise ValueError("No user input found in Cline output")

        # Extract content between user input and end (minus trailing prompts).
        content_lines = []
        for line in lines[user_line_idx + 1 :]:
            stripped = line.strip()
            if re.match(IDLE_PROMPT_PATTERN, stripped):
                break
            if stripped:
                content_lines.append(stripped)

        result = "\n".join(content_lines).strip()
        if not result:
            raise ValueError("Empty Cline response")
        return result

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        self._initialized = False
