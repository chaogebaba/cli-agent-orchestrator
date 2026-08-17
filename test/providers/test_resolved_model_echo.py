"""F127: Unit tests for each provider's resolved_model property.

Covers AC1-AC5 provider-level, M1/M2/M4 kills.
"""
import pytest
from unittest.mock import patch, MagicMock
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.models.terminal import TerminalStatus


class _Concrete(BaseProvider):
    async def initialize(self):
        return True
    def get_status(self, buffer):
        return TerminalStatus.IDLE
    def extract_last_message_from_script(self, s):
        return ""
    def exit_cli(self):
        return "/exit"
    def cleanup(self):
        pass


class TestBaseProviderDefaults:
    def test_resolved_model_default_none(self):
        """BaseProvider.resolved_model returns None by default."""
        p = _Concrete("t1", "s1", "w1")
        assert p.resolved_model is None

    def test_interrupt_keys_default_ctrl_c(self):
        """BaseProvider.interrupt_keys defaults to C-c."""
        p = _Concrete("t1", "s1", "w1")
        assert p.interrupt_keys == ["C-c"]


class TestKiroCliResolvedModel:
    def test_returns_model_when_set(self):
        """AC1: kiro returns its _model (pre-resolved by service layer)."""
        from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
        p = KiroCliProvider.__new__(KiroCliProvider)
        p._model = "opus"
        assert p.resolved_model == "opus"

    def test_returns_none_when_no_model(self):
        from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
        p = KiroCliProvider.__new__(KiroCliProvider)
        p._model = None
        assert p.resolved_model is None


class TestGrokCliResolvedModel:
    @patch("cli_agent_orchestrator.providers.grok_cli.load_agent_profile", return_value=None)
    @patch("cli_agent_orchestrator.providers.grok_cli.get_provider_defaults", return_value={})
    @patch("cli_agent_orchestrator.providers.grok_cli.get_provider_profile_defaults", return_value={})
    @patch("cli_agent_orchestrator.providers.grok_cli.resolve_provider_string_option", return_value=None)
    @patch("cli_agent_orchestrator.providers.grok_cli.get_server_settings", return_value={})
    @patch("cli_agent_orchestrator.providers.grok_cli.get_backend")
    def test_stores_model_during_build(self, mock_be, *_):
        """AC2: Grok stores resolved_model during _build_grok_command."""
        mock_be.return_value.get_pane_working_directory.return_value = "/tmp"
        from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
        p = GrokCliProvider.__new__(GrokCliProvider)
        p.terminal_id = "t1"
        p.session_name = "s1"
        p.window_name = "w1"
        p._model = "grok-3-mini"
        p._agent_profile = None
        p._fork_context = None
        p.allocated_session_uuid = "uuid-1"
        p._skill_prompt = None
        p._build_grok_command()
        assert p.resolved_model == "grok-3-mini"

    def test_none_before_build(self):
        from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
        p = GrokCliProvider.__new__(GrokCliProvider)
        assert p.resolved_model is None


class TestClaudeCodeResolvedModel:
    def test_interrupt_keys_escape(self):
        """AC11: claude_code uses Escape."""
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
        assert ClaudeCodeProvider.interrupt_keys == ["Escape"]

    def test_native_agent_returns_none(self):
        """AC4: native_agent path -> None (honest unknown). Kills M4."""
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
        p = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
        p._resolved_model = None
        assert p.resolved_model is None

    def test_full_profile_stores_model(self):
        """AC3 provider-level."""
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
        p = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
        p._resolved_model = "sonnet-4"
        assert p.resolved_model == "sonnet-4"


class TestCodexResolvedModel:
    def test_none_before_build(self):
        from cli_agent_orchestrator.providers.codex import CodexProvider
        p = CodexProvider.__new__(CodexProvider)
        assert p.resolved_model is None

    def test_after_set(self):
        from cli_agent_orchestrator.providers.codex import CodexProvider
        p = CodexProvider.__new__(CodexProvider)
        p._resolved_model = "o3"
        assert p.resolved_model == "o3"


class TestDirectModelProviders:
    def test_copilot(self):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
        p = CopilotCliProvider.__new__(CopilotCliProvider)
        p._model = "gpt-4o"
        assert p.resolved_model == "gpt-4o"

    def test_opencode(self):
        from cli_agent_orchestrator.providers.opencode_cli import OpenCodeCliProvider
        p = OpenCodeCliProvider.__new__(OpenCodeCliProvider)
        p._model = "deepseek"
        assert p.resolved_model == "deepseek"

    def test_cursor(self):
        from cli_agent_orchestrator.providers.cursor_cli import CursorCliProvider
        p = CursorCliProvider.__new__(CursorCliProvider)
        p._model = "claude-4"
        assert p.resolved_model == "claude-4"

    def test_antigravity(self):
        from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
        p = AntigravityCliProvider.__new__(AntigravityCliProvider)
        p._model = "claude-opus"
        assert p.resolved_model == "claude-opus"

    def test_none_when_unset(self):
        from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
        p = CopilotCliProvider.__new__(CopilotCliProvider)
        p._model = None
        assert p.resolved_model is None


class TestBuildChainProviders:
    def test_kimi(self):
        from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
        p = KimiCliProvider.__new__(KimiCliProvider)
        p._resolved_model = "kimi-k2"
        assert p.resolved_model == "kimi-k2"

    def test_hermes(self):
        from cli_agent_orchestrator.providers.hermes import HermesProvider
        p = HermesProvider.__new__(HermesProvider)
        p._resolved_model = "hermes-3"
        assert p.resolved_model == "hermes-3"


class TestMockCliResolvedModel:
    def test_inherits_none(self):
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider
        p = MockCliProvider.__new__(MockCliProvider)
        assert p.resolved_model is None
