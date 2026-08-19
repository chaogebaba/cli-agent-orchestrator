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
    IDLE_PROMPT_PATTERN,
    PROCESSING_PATTERN,
    WAITING_USER_ANSWER_PATTERN,
)


# ─── Command construction ────────────────────────────────────────────────────


class TestClineCliProviderCommand:
    """Tests for launch command construction."""

    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_build_command_default(self, mock_load):
        """Default command includes --tui and --auto-approve true."""
        mock_load.side_effect = FileNotFoundError("no profile")

        provider = ClineCliProvider("t1234567", "sess", "win0")
        cmd = provider._build_command()
        parts = shlex.split(cmd)

        assert parts[0] == CLINE_BINARY
        assert "--tui" in parts
        assert "--auto-approve" in parts
        assert "true" in parts
        # No model flag without profile or providers.toml
        assert "-m" not in parts
        assert "-s" not in parts

    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_build_command_with_model_override(self, mock_load):
        """Explicit model kwarg is forwarded as -m."""
        mock_load.side_effect = FileNotFoundError("no profile")

        provider = ClineCliProvider(
            "t1234567", "sess", "win0", model="deepseek/deepseek-chat"
        )
        cmd = provider._build_command()
        parts = shlex.split(cmd)

        assert "-m" in parts
        assert parts[parts.index("-m") + 1] == "deepseek/deepseek-chat"

    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_build_command_with_system_prompt(self, mock_load):
        """Profile system prompt is forwarded as -s."""
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

    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_build_command_skill_prompt_appended(self, mock_load):
        """Skill prompt is appended to system prompt."""
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

    @patch("cli_agent_orchestrator.providers.cline_cli.load_agent_profile")
    def test_resolved_model_property(self, mock_load):
        """resolved_model is set after _build_command."""
        mock_load.side_effect = FileNotFoundError("no profile")

        provider = ClineCliProvider(
            "t1234567", "sess", "win0", model="deepseek/deepseek-chat"
        )
        provider._build_command()

        assert provider.resolved_model == "deepseek/deepseek-chat"


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

    def test_waiting_user_answer(self):
        provider = self._make_provider()
        provider._initialized = True
        output = "Do you want to proceed? [y/n]"
        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER


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
            model="deepseek/deepseek-chat",
        )
        assert isinstance(provider, ClineCliProvider)
        assert provider._model == "deepseek/deepseek-chat"


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
