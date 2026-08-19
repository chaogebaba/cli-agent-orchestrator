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
    IDLE_PLACEHOLDER_PATTERN,
    IDLE_PROMPT_PATTERN,
    PROCESSING_PATTERN,
    WAITING_USER_ANSWER_PATTERN,
)


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
    """Tests for get_status terminal state detection."""

    def _make_provider(self) -> ClineCliProvider:
        return ClineCliProvider("t1234567", "sess", "win0")

    def test_empty_output_unknown(self):
        provider = self._make_provider()
        assert provider.get_status("") == TerminalStatus.UNKNOWN

    def test_whitespace_only_unknown(self):
        provider = self._make_provider()
        assert provider.get_status("   \n\n  ") == TerminalStatus.UNKNOWN

    def test_idle_prompt_detected(self):
        provider = self._make_provider()
        provider._initialized = True
        output = "Welcome to Cline\n\n❯ "
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_idle_with_prior_input_is_completed(self):
        provider = self._make_provider()
        provider._initialized = True
        provider._input_received = True
        output = "Some response text\n\n❯ "
        assert provider.get_status(output) == TerminalStatus.COMPLETED

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
        output = "The error was:\nError: model not found\nThat's the issue.\n\n❯ "
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_error_suppressed_when_idle_prompt_completed(self):
        """S1: quoted error lines after input don't block COMPLETED verdict."""
        provider = self._make_provider()
        provider._initialized = True
        provider._input_received = True
        output = "I found this error:\nError: connection refused\nFixed it.\n\n❯ "
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_waiting_user_answer(self):
        provider = self._make_provider()
        provider._initialized = True
        output = "Do you want to proceed? [y/n]"
        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    def test_idle_via_ask_anything_placeholder(self):
        provider = self._make_provider()
        provider._initialized = True
        output = "Welcome to Cline\n\nAsk anything..."
        assert provider.get_status(output) == TerminalStatus.IDLE


# ─── Idle prompt detection ────────────────────────────────────────────────────


class TestClineCliIdlePrompt:
    """Tests for _has_idle_prompt helper."""

    def test_bare_prompt(self):
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
        lines = ["❯ "] + ["filler line"] * 20
        assert ClineCliProvider._has_idle_prompt(lines) is False

    def test_ask_anything_placeholder(self):
        """Cline TUI 'Ask anything...' placeholder is detected as idle."""
        lines = ["some response", "", "Ask anything..."]
        assert ClineCliProvider._has_idle_prompt(lines) is True

    def test_ask_anything_with_prefix(self):
        """Placeholder with leading whitespace."""
        lines = ["output", "  Ask anything or type / for commands"]
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
