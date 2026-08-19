"""Unit tests for Cline CLI provider."""

from __future__ import annotations

import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.cline_cli import (
    CLINE_BINARY,
    ClineCliProvider,
    ERROR_PATTERN,
    IDLE_COMPOSER_PATTERN,
    IDLE_PLACEHOLDER_PATTERN,
    IDLE_PROMPT_PATTERN,
    PROCESSING_PATTERN,
    WAITING_USER_ANSWER_PATTERN,
)


# ─── Real screen captures (from live cline 3.0.55, ClinePass, 2026-08-19) ───

# Virgin idle: first launch, no exchange yet.
# The "What can I do for you?" text appears both as a banner AND on the
# composer line (with ❯ glyph).
REAL_SCREEN_VIRGIN_IDLE = """\




 What can I do for you?




────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯ What can I do for you?
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 ClinePass: DeepSeek V4 Flash (high) ██████ (0)                                                                                                                                      ○ Plan ● Act (Tab)
 cli-subagents (quirks-merge-train)
 ⏵⏵ Auto-approve all enabled (Shift+Tab)
"""

# Post-response idle: after a successful exchange, composer shows "Ask anything...".
REAL_SCREEN_POST_RESPONSE_IDLE = """\
 ❯ reply with exactly: OK

 ▶ Thinking: The user just wants me to reply with exactly "OK". This is a simple request without coding context, so I should answer directly.

 * OK






































────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯ Ask anything...
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 ClinePass: DeepSeek V4 Flash (high) ██████ (6,346)                                                                                                                                  ○ Plan ● Act (Tab)
 cli-subagents (quirks-merge-train)
 ⏵⏵ Auto-approve all enabled (Shift+Tab)
"""

# Composer with typed text: user has started typing (not idle).
REAL_SCREEN_COMPOSER_WITH_TEXT = """\
 ❯ reply with exactly: OK

 ▶ Thinking: The user just wants me to reply with exactly "OK".

 * OK




────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯ hello world this is my typed text
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 ClinePass: DeepSeek V4 Flash (high) ██████ (6,346)                                                                                                                                  ○ Plan ● Act (Tab)
 cli-subagents (quirks-merge-train)
 ⏵⏵ Auto-approve all enabled (Shift+Tab)
"""


# ─── Command construction ────────────────────────────────────────────────────


class TestClineCliProviderCommand:
    """Tests for launch command construction."""

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_build_command_default(self, mock_load, mock_defaults):
        """Default command includes --tui, --auto-approve true, -P cline-pass, --thinking high."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass"}

        provider = ClineCliProvider("t1234567", "sess", "win0")
        cmd = provider._build_command()
        parts = shlex.split(cmd)

        assert parts[0] == CLINE_BINARY
        assert "--tui" in parts
        assert "--auto-approve" in parts
        assert "true" in parts
        assert "-P" in parts
        assert parts[parts.index("-P") + 1] == "cline-pass"
        assert "--thinking" in parts
        assert parts[parts.index("--thinking") + 1] == "high"
        # No model flag without profile or providers.toml model key
        assert "-m" not in parts
        assert "-s" not in parts

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_build_command_with_model_override(self, mock_load, mock_defaults):
        """Explicit model kwarg is forwarded as -m."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass"}

        provider = ClineCliProvider(
            "t1234567", "sess", "win0", model="deepseek/deepseek-chat"
        )
        cmd = provider._build_command()
        parts = shlex.split(cmd)

        assert "-m" in parts
        assert parts[parts.index("-m") + 1] == "deepseek/deepseek-chat"
        assert "-P" in parts
        assert parts[parts.index("-P") + 1] == "cline-pass"

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_build_command_with_system_prompt(self, mock_load, mock_defaults):
        """Profile system prompt is forwarded as -s."""
        mock_defaults.return_value = {"api_provider": "cline-pass"}
        profile = MagicMock()
        profile.system_prompt = "You are a test agent."
        profile.model = None
        profile.name = "test-agent"
        mock_load.return_value = profile

        provider = ClineCliProvider("t1234567", "sess", "win0", agent_profile="test-agent")
        cmd = provider._build_command()
        parts = shlex.split(cmd)

        assert "-s" in parts
        assert parts[parts.index("-s") + 1] == "You are a test agent."

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_build_command_skill_prompt_appended(self, mock_load, mock_defaults):
        """Skill prompt is appended to system prompt."""
        mock_defaults.return_value = {"api_provider": "cline-pass"}
        profile = MagicMock()
        profile.system_prompt = "Base prompt."
        profile.model = None
        profile.name = "agent"
        mock_load.return_value = profile

        provider = ClineCliProvider(
            "t1234567", "sess", "win0",
            agent_profile="agent",
            skill_prompt="Extra skill instructions.",
        )
        cmd = provider._build_command()
        parts = shlex.split(cmd)

        assert "-s" in parts
        system_arg = parts[parts.index("-s") + 1]
        assert "Base prompt." in system_arg
        assert "Extra skill instructions." in system_arg


# ─── Model resolution ────────────────────────────────────────────────────────


class TestClineCliModelResolution:
    """Tests for model resolution precedence."""

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_explicit_model_wins(self, mock_load, mock_resolve, mock_prof_defaults, mock_defaults):
        """Explicit model kwarg takes precedence over everything."""
        mock_load.side_effect = FileNotFoundError("no profile")

        provider = ClineCliProvider(
            "t1234567", "sess", "win0", model="deepseek/deepseek-r1"
        )
        result = provider._resolve_model()

        assert result == "deepseek/deepseek-r1"
        mock_resolve.assert_not_called()

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_toml_resolution_fallback(
        self, mock_load, mock_resolve, mock_prof_defaults, mock_defaults
    ):
        """Without explicit model, falls back to providers.toml chain."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"model": "deepseek/deepseek-chat"}
        mock_prof_defaults.return_value = {}
        mock_resolve.return_value = "deepseek/deepseek-chat"

        provider = ClineCliProvider("t1234567", "sess", "win0")
        result = provider._resolve_model()

        assert result == "deepseek/deepseek-chat"
        mock_defaults.assert_called_once_with("cline_cli")

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_resolved_model_property(self, mock_load, mock_defaults):
        """resolved_model is set after _build_command."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass"}

        provider = ClineCliProvider(
            "t1234567", "sess", "win0", model="deepseek/deepseek-v4-flash"
        )
        provider._build_command()

        assert provider.resolved_model == "deepseek/deepseek-v4-flash"


# ─── Thinking / reasoning effort ──────────────────────────────────────────────


class TestClineCliThinking:
    """Tests for --thinking flag resolution."""

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_default_thinking_high(self, mock_load, mock_resolve, mock_prof, mock_defaults):
        """Without any override, thinking defaults to 'high'."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {}
        mock_prof.return_value = {}
        mock_resolve.return_value = None  # no toml override

        provider = ClineCliProvider("t1234567", "sess", "win0")
        result = provider._resolve_thinking()
        assert result == "high"

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_toml_thinking_override(self, mock_load, mock_resolve, mock_prof, mock_defaults):
        """providers.toml thinking value overrides the default."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"thinking": "medium"}
        mock_prof.return_value = {}
        mock_resolve.return_value = "medium"

        provider = ClineCliProvider("t1234567", "sess", "win0")
        result = provider._resolve_thinking()
        assert result == "medium"

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_thinking_in_command(self, mock_load, mock_defaults):
        """--thinking flag appears in the built command."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass", "thinking": "xhigh"}

        provider = ClineCliProvider("t1234567", "sess", "win0")
        parts = shlex.split(provider._build_command())

        assert "--thinking" in parts
        assert parts[parts.index("--thinking") + 1] == "xhigh"

    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_explicit_empty_suppresses_thinking(self, mock_load, mock_resolve, mock_prof, mock_defaults):
        """Explicit empty string in providers.toml suppresses the --thinking flag."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"thinking": ""}
        mock_prof.return_value = {}
        mock_resolve.return_value = ""  # explicit empty

        provider = ClineCliProvider("t1234567", "sess", "win0")
        assert provider._resolve_thinking() == ""

    @patch("cli_agent_orchestrator.providers.cline_cli.resolve_provider_string_option")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_profile_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.get_provider_defaults")
    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_empty_thinking_omits_flag_from_command(self, mock_load, mock_defaults, mock_prof, mock_resolve):
        """Empty thinking value results in no --thinking flag in the command."""
        mock_load.side_effect = FileNotFoundError("no profile")
        mock_defaults.return_value = {"api_provider": "cline-pass"}
        mock_prof.return_value = {}
        # First call for model (returns None), second call for thinking (returns "")
        mock_resolve.side_effect = [None, ""]

        provider = ClineCliProvider("t1234567", "sess", "win0")
        parts = shlex.split(provider._build_command())

        assert "--thinking" not in parts


# ─── Status detection ─────────────────────────────────────────────────────────


class TestClineCliStatusDetection:
    """Tests for get_status terminal state detection using REAL screen captures."""

    def _make_provider(self) -> ClineCliProvider:
        return ClineCliProvider("t1234567", "sess", "win0")

    def test_empty_output_unknown(self):
        provider = self._make_provider()
        assert provider.get_status("") == TerminalStatus.UNKNOWN

    def test_whitespace_only_unknown(self):
        provider = self._make_provider()
        assert provider.get_status("   \n\n  ") == TerminalStatus.UNKNOWN

    def test_virgin_idle_screen(self):
        """Real virgin idle screen (first launch, 'What can I do for you?')."""
        provider = self._make_provider()
        provider._initialized = True
        assert provider.get_status(REAL_SCREEN_VIRGIN_IDLE) == TerminalStatus.IDLE

    def test_post_response_idle_screen(self):
        """Real post-response idle screen ('Ask anything...')."""
        provider = self._make_provider()
        provider._initialized = True
        assert provider.get_status(REAL_SCREEN_POST_RESPONSE_IDLE) == TerminalStatus.IDLE

    def test_post_response_with_input_is_completed(self):
        """Post-response screen after input has been sent = COMPLETED."""
        provider = self._make_provider()
        provider._initialized = True
        provider._input_received = True
        assert provider.get_status(REAL_SCREEN_POST_RESPONSE_IDLE) == TerminalStatus.COMPLETED

    def test_composer_with_typed_text_is_not_idle(self):
        """Screen with typed text in composer is NOT idle (user is typing)."""
        provider = self._make_provider()
        provider._initialized = True
        # The composer shows "❯ hello world this is my typed text" which does
        # NOT match idle patterns — it's arbitrary user text, not a placeholder.
        assert provider.get_status(REAL_SCREEN_COMPOSER_WITH_TEXT) != TerminalStatus.IDLE

    def test_processing_spinner(self):
        provider = self._make_provider()
        provider._initialized = True
        output = "Generating...\n⠹ Thinking..."
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_error_detection(self):
        provider = self._make_provider()
        provider._initialized = True
        output = "Something happened\nError: model not found\n"
        assert provider.get_status(output) == TerminalStatus.ERROR

    def test_error_suppressed_when_idle_prompt_present(self):
        """S1: quoted error lines in response don't trigger ERROR when idle prompt visible."""
        provider = self._make_provider()
        provider._initialized = True
        output = "The error was:\nError: model not found\nThat's the issue.\n\n❯ Ask anything..."
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_error_suppressed_when_idle_prompt_completed(self):
        """S1: quoted error lines after input don't block COMPLETED verdict."""
        provider = self._make_provider()
        provider._initialized = True
        provider._input_received = True
        output = "I found this error:\nError: connection refused\nFixed it.\n\n❯ Ask anything..."
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_waiting_user_answer(self):
        provider = self._make_provider()
        provider._initialized = True
        output = "Do you want to proceed? [y/n]"
        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_banner_only_not_idle(self):
        """A banner 'What can I do for you?' without the ❯ glyph is NOT idle.

        The banner text appears above the composer and can co-exist with
        processing state.  Only the composer line (glyph + placeholder)
        should trigger idle detection.
        """
        provider = self._make_provider()
        provider._initialized = True
        # Simulate: banner present, but bottom line is a processing indicator.
        output = "What can I do for you?\n\n⠹ Thinking..."
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_processing_with_idle_composer_visible(self):
        """Real processing screen: ❯ Ask anything... is visible at bottom BUT
        '⠦ Thinking...' spinner is present — PROCESSING takes priority.

        Captured live from cline 3.0.55 during actual message processing.
        """
        provider = self._make_provider()
        provider._initialized = True
        output = (
            " ❯ write a poem\n"
            "\n"
            " ⠦ Thinking... (esc to cancel)\n"
            "\n" * 30 +
            "────────────────────────────────────────\n"
            "❯ Ask anything...\n"
            "────────────────────────────────────────\n"
            " ClinePass: DeepSeek V4 Flash (high) ██████ (6,148)\n"
            " 5046ee32 (f323-cline-idle-timeout)\n"
            " ⏵⏵ Auto-approve all enabled (Shift+Tab)\n"
        )
        assert provider.get_status(output) == TerminalStatus.PROCESSING


# ─── Idle prompt detection ────────────────────────────────────────────────────


class TestClineCliIdlePrompt:
    """Tests for _has_idle_prompt helper with real screen patterns."""

    def test_virgin_composer_line(self):
        """'❯ What can I do for you?' composer line is detected as idle."""
        lines = ["", "What can I do for you?", "", "────", "❯ What can I do for you?", "────", "ClinePass: ..."]
        # The tail window includes the composer line.
        assert ClineCliProvider._has_idle_prompt(lines) is True

    def test_post_response_composer_line(self):
        """'❯ Ask anything...' composer line is detected as idle."""
        lines = ["response text", "", "────", "❯ Ask anything...", "────", "ClinePass: ..."]
        assert ClineCliProvider._has_idle_prompt(lines) is True

    def test_bare_prompt(self):
        """Bare ❯ still works (fallback for older cline versions)."""
        lines = ["some output", "", "❯ "]
        assert ClineCliProvider._has_idle_prompt(lines) is True

    def test_bare_gt_prompt(self):
        lines = ["some output", "", "> "]
        assert ClineCliProvider._has_idle_prompt(lines) is True

    def test_no_prompt(self):
        lines = ["some output", "more output"]
        assert ClineCliProvider._has_idle_prompt(lines) is False

    def test_prompt_buried_too_deep(self):
        """Prompt beyond the 8-line tail window is not detected."""
        lines = ["❯ What can I do for you?"] + ["filler line"] * 20
        assert ClineCliProvider._has_idle_prompt(lines) is False

    def test_ask_anything_without_glyph_fallback(self):
        """'Ask anything...' without glyph still detected (escape-strip edge case)."""
        lines = ["some response", "", "Ask anything..."]
        assert ClineCliProvider._has_idle_prompt(lines) is True

    def test_typed_text_not_idle(self):
        """'❯ hello world' (user text, not placeholder) is NOT idle."""
        lines = ["prev response", "────", "❯ hello world this is my typed text", "────", "ClinePass: ..."]
        assert ClineCliProvider._has_idle_prompt(lines) is False

    def test_status_bar_lines_below_composer(self):
        """Real screen has status bar lines below composer — still detect idle.

        Real layout (bottom 5 lines):
          ❯ Ask anything...
          ──────────...
          ClinePass: DeepSeek V4 Flash (high) ██████ (6,346)   ...
          cli-subagents (quirks-merge-train)
          ⏵⏵ Auto-approve all enabled (Shift+Tab)

        The status/info lines are non-empty and don't match — but the scan
        should still find the composer in the 8-line tail.
        """
        lines = [
            "response text",
            "",
            "────────────────────────────────────────",
            "❯ Ask anything...",
            "────────────────────────────────────────",
            " ClinePass: DeepSeek V4 Flash (high) ██████ (6,346)",
            " cli-subagents (quirks-merge-train)",
            " ⏵⏵ Auto-approve all enabled (Shift+Tab)",
        ]
        assert ClineCliProvider._has_idle_prompt(lines) is True

    def test_virgin_full_tail(self):
        """Real virgin screen tail — the bottom-most non-empty lines are status lines,
        but the composer '❯ What can I do for you?' is within the 8-line window."""
        lines = REAL_SCREEN_VIRGIN_IDLE.splitlines()
        assert ClineCliProvider._has_idle_prompt(lines) is True

    def test_post_response_full_tail(self):
        """Real post-response screen tail."""
        lines = REAL_SCREEN_POST_RESPONSE_IDLE.splitlines()
        assert ClineCliProvider._has_idle_prompt(lines) is True


# ─── Response extraction ──────────────────────────────────────────────────────


class TestClineCliExtraction:
    """Tests for extract_last_message_from_script."""

    def _make_provider(self) -> ClineCliProvider:
        return ClineCliProvider("t1234567", "sess", "win0")

    def test_basic_extraction(self):
        provider = self._make_provider()
        script = "❯ what is 2+2\nThe answer is 4.\n\n❯ "
        result = provider.extract_last_message_from_script(script)
        assert result == "The answer is 4."

    def test_multiline_extraction(self):
        provider = self._make_provider()
        script = "❯ explain\nLine one.\nLine two.\nLine three.\n\n❯ "
        result = provider.extract_last_message_from_script(script)
        assert "Line one." in result
        assert "Line three." in result

    def test_no_user_input_raises(self):
        provider = self._make_provider()
        with pytest.raises(ValueError, match="No user input"):
            provider.extract_last_message_from_script("just some text without prompt")

    def test_empty_response_raises(self):
        provider = self._make_provider()
        with pytest.raises(ValueError, match="Empty Cline response"):
            provider.extract_last_message_from_script("❯ hello\n\n❯ ")


# ─── Provider registration ────────────────────────────────────────────────────


class TestClineCliRegistration:
    """Tests that the provider is correctly registered."""

    def test_provider_type_enum_exists(self):
        assert ProviderType.CLINE_CLI.value == "cline_cli"

    def test_provider_in_classes_map(self):
        from cli_agent_orchestrator.providers.manager import PROVIDER_CLASSES

        assert "cline_cli" in PROVIDER_CLASSES
        assert PROVIDER_CLASSES["cline_cli"] is ClineCliProvider

    def test_provider_manager_creates_cline(self):
        from cli_agent_orchestrator.providers.manager import ProviderManager

        mgr = ProviderManager()
        provider = mgr.construct_provider(
            "cline_cli",
            "t1234567",
            "test-session",
            "win-0",
            agent_profile="developer",
            model="deepseek/deepseek-v4-flash",
        )
        assert isinstance(provider, ClineCliProvider)
        assert provider._model == "deepseek/deepseek-v4-flash"

    def test_cline_in_soft_enforcement_providers(self):
        """S2: cline_cli is in SOFT_ENFORCEMENT_PROVIDERS (no native tool blocking)."""
        from cli_agent_orchestrator.services.terminal_service import (
            SOFT_ENFORCEMENT_PROVIDERS,
        )

        assert "cline_cli" in SOFT_ENFORCEMENT_PROVIDERS


# ─── Miscellaneous properties ─────────────────────────────────────────────────


class TestClineCliProperties:
    """Tests for provider properties and interface compliance."""

    def test_paste_enter_count(self):
        provider = ClineCliProvider("t1234567", "sess", "win0")
        assert provider.paste_enter_count == 1

    def test_paste_submit_delay(self):
        provider = ClineCliProvider("t1234567", "sess", "win0")
        assert provider.paste_submit_delay == 0.3

    def test_exit_cli(self):
        provider = ClineCliProvider("t1234567", "sess", "win0")
        assert provider.exit_cli() == "/exit"

    def test_cleanup(self):
        provider = ClineCliProvider("t1234567", "sess", "win0")
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

    def test_classify_injection_hazard_none(self):
        provider = ClineCliProvider("t1234567", "sess", "win0")
        rows = ["some text", "more text"]
        assert provider.classify_injection_hazard(rows) is None
