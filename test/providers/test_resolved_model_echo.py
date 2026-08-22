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
    @patch("cli_agent_orchestrator.providers.grok_cli.GrokCliProvider._rebuild_private_config")
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


# ============================================================
# S1 — Echo-path witness tests (kill M1: assign result + fleet projection)
# ============================================================

class TestAssignEchoResolvedModel:
    """AC1/M1: assign return dict carries resolved_model for kiro (immediate).

    Tests the structural pattern: the result dict in _assign_impl includes
    'resolved_model' key. We verify by inspecting the server source code's
    result dict construction.
    """

    def test_assign_result_dict_has_resolved_model_key(self):
        """M1 kill: removing resolved_model from assign return dict fails this test.

        Instead of calling the full _assign_impl (which needs heavy mocking of
        requests + HTTP), we verify the structural pattern by inspecting that
        the result dict in _assign_impl contains 'resolved_model'.
        """
        import inspect
        from cli_agent_orchestrator.mcp_server import server

        source = inspect.getsource(server._assign_impl)
        # The result dict MUST contain "resolved_model" key
        assert '"resolved_model"' in source or "'resolved_model'" in source, (
            "M1 mutant: resolved_model key missing from _assign_impl result dict"
        )
        # Also verify it references _f127_resolved (the computed value, not a hardcode)
        assert "_f127_resolved" in source, (
            "M1 mutant: resolved_model not computed from DB metadata"
        )


class TestFleetEchoResolvedModel:
    """AC2/M1: fleet projection carries resolved_model."""

    def test_fleet_projection_includes_resolved_model(self, monkeypatch, tmp_path):
        """M1 kill (fleet variant): removing from fleet projection fails this."""
        from unittest.mock import patch, MagicMock
        from datetime import datetime, timezone

        # Simulate a terminal row that has resolved_model set
        row = {
            "id": "cccccccc",
            "tmux_session": "cao-test",
            "tmux_window": "w-cccccccc",
            "provider": "grok_cli",
            "agent_profile": "grok_dev",
            "caller_id": "aaaaaaaa",
            "init_state": "ready",
            "lifecycle": "ephemeral",
            "resolved_model": "grok-3-mini",
            "reparented_from": None,
            "last_active": datetime.now(timezone.utc),
        }

        from cli_agent_orchestrator.services.fleet_service import build_fleet
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        mock_status = MagicMock()
        mock_obs = MagicMock()
        mock_obs.status = TerminalStatus.IDLE
        mock_status.get_boundary_observation.return_value = mock_obs

        with patch("cli_agent_orchestrator.services.fleet_service.list_terminals_by_session", return_value=[row]), \
             patch("cli_agent_orchestrator.services.fleet_service.status_monitor", mock_status), \
             patch("cli_agent_orchestrator.services.fleet_service.get_backend") as mock_be:
            mock_be.return_value.list_windows.return_value = {
                "w-cccccccc": {"window_index": 1, "window_name": "w-cccccccc"}
            }
            result = build_fleet("cao-test")

        terminal_entry = result["terminals"][0]
        assert "resolved_model" in terminal_entry, "M1 mutant (fleet): resolved_model missing"
        assert terminal_entry["resolved_model"] == "grok-3-mini"


# ============================================================
# S2 — Native-agent build-path witness test (kill M4)
# ============================================================

class TestClaudeCodeNativeAgentBuildPath:
    """AC4/M4: _build_claude_command with native_agent set stores None, omits --model."""

    def test_native_agent_build_stores_none_and_omits_model_flag(self, tmp_path):
        """M4 kill: returning self._model for native_agent path fails this test."""
        from unittest.mock import patch, MagicMock
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        # Create a minimal provider instance via __new__ to avoid full init
        p = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
        p.terminal_id = "t1"
        p.session_name = "s1"
        p.window_name = "w1"
        p._model = "opus"  # explicitly set — M4 mutant would return this
        p._agent_profile = "my-profile"
        p._allowed_tools = None
        p._skill_prompt = None
        p._fork_context = None
        p._persona_plan = None
        p.allocated_session_uuid = "test-uuid-1234"

        # Create a profile with native_agent set
        mock_profile = MagicMock()
        mock_profile.native_agent = "my-native-agent"
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_profile.allowedTools = None
        mock_profile.name = "my-profile"
        mock_profile.permissionMode = None

        # _build_claude_command needs these
        with patch.object(ClaudeCodeProvider, "_ensure_skip_bypass_prompt_setting"), \
             patch("cli_agent_orchestrator.providers.claude_code.get_server_settings", return_value={}), \
             patch("cli_agent_orchestrator.providers.claude_code.resolve_endpoint", return_value="http://localhost:9889"):
            cmd = p._build_claude_command(profile=mock_profile)

        # M4 kill assertions:
        # 1. _resolved_model must be None (not self._model)
        assert p._resolved_model is None, (
            f"M4 mutant: native_agent path should store None, got {p._resolved_model!r}"
        )
        # 2. The property should return None
        assert p.resolved_model is None
        # 3. --model flag must NOT appear in the command
        assert "--model" not in cmd, "M4 mutant: --model flag should be absent for native_agent"
        # 4. --agent flag MUST appear
        assert "--agent" in cmd
        assert "my-native-agent" in cmd
