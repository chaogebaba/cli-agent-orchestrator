"""Additional tests for ClaudeCodeProvider to cover uncovered branches.

Covers: McpServer model_dump path, bypass permissions prompt handling,
and the workspace-trust handling in _handle_startup_prompts (including the
regression where the echoed launch command false-matched the idle prompt).
"""

import re
import time
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# F254 D19: entire module exceeds unit budget (provider startup waits).
pytestmark = pytest.mark.slow


@pytest.fixture
def provider():
    """Create a ClaudeCodeProvider with mocked dependencies."""
    with patch("cli_agent_orchestrator.backends.registry._backend"):
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        p = ClaudeCodeProvider("tid1", "ses", "win", "test-agent")
        yield p


class TestBuildCommandMcpServerModelDump:
    """Test the model_dump branch in _build_claude_command (line 93)."""

    @patch("cli_agent_orchestrator.providers.claude_code.load_agent_profile")
    def test_mcp_server_with_model_dump(self, mock_load, provider):
        """When mcpServers contains a Pydantic model (not dict), model_dump is called."""
        mock_mcp = MagicMock()
        mock_mcp.model_dump.return_value = {
            "command": "node",
            "args": ["server.js"],
            "env": {},
        }
        # isinstance(mock_mcp, dict) returns False, so the model_dump branch triggers
        mock_profile = MagicMock()
        mock_profile.model = None
        mock_profile.system_prompt = "Test prompt"
        mock_profile.mcpServers = {"my-mcp": mock_mcp}
        mock_profile.allowedTools = None
        mock_profile.permissionMode = None
        mock_profile.inheritUserMcpServers = None
        mock_load.return_value = mock_profile

        cmd = provider._build_claude_command()

        assert "--mcp-config" in cmd
        mock_mcp.model_dump.assert_called_once_with(exclude_none=True)


class TestHandleStartupPromptsBranches:
    """Test _handle_startup_prompts branches."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.claude_code.asyncio.sleep")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_bypass_permissions_prompt(self, mock_backend, mock_sleep, provider):
        """Detects bypass prompt and selects 'Yes, I accept' via real Down+Enter.

        F548 (#404): real special keys, not literal ESC[B. Focus defaults to
        'No, exit' (row 1); after Down the settle re-read shows ❯ on 'Yes, I
        accept', then Enter.
        """
        focus_no = "⚠ Bypass Permissions mode\n❯ 1. No, exit\n  2. Yes, I accept\n"
        focus_yes = "⚠ Bypass Permissions mode\n  1. No, exit\n❯ 2. Yes, I accept\n"
        mock_backend.get_history.side_effect = [focus_no, focus_yes, "Welcome to Claude Code v2.5"]

        await provider._handle_startup_prompts(idle_gap=1.0)

        assert mock_backend.send_special_key.call_args_list == [
            call(provider.session_name, provider.window_name, "Down"),
            call(provider.session_name, provider.window_name, "Enter"),
        ]
        mock_backend.send_keys.assert_not_called()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.claude_code.asyncio.sleep")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_echoed_prompt_does_not_short_circuit_trust(
        self, mock_backend, mock_sleep, provider
    ):
        """Regression: the injected --append-system-prompt contains a line that
        starts with "> `memory_store`". The shell echoes the launch command into
        the capture buffer ~300ms before the workspace-trust dialog renders, so
        that "> " must NOT be treated as the idle prompt and end startup handling
        early — otherwise the trust dialog is left unaccepted and initialize()
        blocks on {IDLE, COMPLETED} until it times out and the session is killed.

        First poll returns the echoed command (with the "> memory_store" marker
        but no dialog yet); second poll returns the trust dialog. The handler must
        accept the trust dialog (Enter) rather than returning on the first frame.
        """
        echoed_launch_cmd = (
            "user@host:/tmp/proj$ claude --dangerously-skip-permissions "
            "--append-system-prompt '## Memory\n"
            "> `memory_store` and `memory_recall` are CAO's memory tools'"
        )
        trust_frame = (
            "Quick safety check: Is this a project you created or one you trust?\n"
            "❯ 1. Yes, I trust this folder\n"
            "  2. No, exit\n"
        )
        mock_backend.get_history.side_effect = [
            echoed_launch_cmd,
            trust_frame,
            "Welcome to Claude Code v2.1.211",
        ]

        await provider._handle_startup_prompts(idle_gap=5.0)

        # Trust dialog accepted via Down+Enter (real special keys) — proves we did
        # not early-return on the echoed "> memory_store" marker. F548 (#404):
        # the fixture's second frame already focuses 'Yes, I trust this folder',
        # so the settle poll confirms from the current pane.
        assert mock_backend.send_special_key.call_args_list == [
            call(provider.session_name, provider.window_name, "Down"),
            call(provider.session_name, provider.window_name, "Enter"),
        ]

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.claude_code.asyncio.sleep")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_welcome_banner_detected_early_return(self, mock_backend, mock_sleep, provider):
        """When welcome banner is visible, returns immediately."""
        mock_backend.get_history.return_value = "Welcome to Claude Code v2.5.0"

        await provider._handle_startup_prompts(idle_gap=1.0)

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.claude_code.time")
    @patch("cli_agent_orchestrator.providers.claude_code.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_trust_prompt_detected(self, mock_backend, mock_sleep, mock_time, provider):
        """Trust prompt selects the affirmative row (Down+Enter) and settles at
        idle gap (bounded iterations).

        F548 (#404): bare Enter would confirm the default 'No, exit' and kill
        the seat, so trust is now Down (onto 'Yes, I trust this folder') + Enter.
        The trust branch now uses ``asyncio.sleep`` (not blocking ``time.sleep``)
        to match the async-offload doctrine, so ``time.monotonic`` is mocked here
        to drive the idle-gap exit deterministically instead of relying on a real
        blocking sleep to advance wall-clock.
        """
        # outer_deadline, last_prompt_time, iter1 (trust @ t=1), reset, iter2 (gap
        # 3-1=2 >= idle_gap 1 -> settle/return).
        mock_time.monotonic.side_effect = [0.0, 0.0, 1.0, 1.0, 3.0]
        mock_backend.get_history.return_value = (
            "Do you trust the files in this folder?\n" "❯ Yes, I trust this folder"
        )

        await provider._handle_startup_prompts(idle_gap=1.0)

        # F548 (#404): Down then Enter as real special keys; confirmed from the
        # current pane (❯ already on the affirmative row), no send_keys ESC[B.
        assert mock_backend.send_special_key.call_args_list == [
            call(provider.session_name, provider.window_name, "Down"),
            call(provider.session_name, provider.window_name, "Enter"),
        ]
        mock_backend.send_keys.assert_not_called()

    @pytest.mark.asyncio
    @patch(
        "cli_agent_orchestrator.providers.claude_code.provider_home",
        return_value=MagicMock(classification="shared-auth-read-only"),
    )
    @patch("cli_agent_orchestrator.providers.claude_code.asyncio.sleep", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_external_imports_prompt_settles_at_idle_gap(
        self, mock_backend, mock_sleep, _mock_home, provider
    ):
        """Lone external-imports prompt settles at idle gap, not outer timeout.

        F548 (#404): reject via real Down + settle-confirm ❯ on 'No, disable
        external imports' + Enter (real special keys, no literal ESC[B). Uses a
        stateful capture: loop read shows focus on row 1, the settle re-read
        shows ❯ moved onto the reject row, then the banner ends the loop.
        """
        focus_yes = (
            "Allow external CLAUDE.md file imports?\n"
            "❯ 1. Yes, allow external imports\n  2. No, disable external imports\n"
        )
        focus_no = (
            "Allow external CLAUDE.md file imports?\n"
            "  1. Yes, allow external imports\n❯ 2. No, disable external imports\n"
        )
        mock_backend.get_history.side_effect = [focus_yes, focus_no, "Welcome to Claude Code v2.5"]

        await provider._handle_startup_prompts(idle_gap=1.0)

        assert mock_backend.send_special_key.call_args_list == [
            call(provider.session_name, provider.window_name, "Down"),
            call(provider.session_name, provider.window_name, "Enter"),
        ]
        mock_backend.send_keys.assert_not_called()


class TestDatabaseListAllTerminals:
    """Test list_all_terminals database function (lines 149-151)."""

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_list_all_terminals(self, mock_session_class):
        from cli_agent_orchestrator.clients.database import list_all_terminals

        mock_terminal = MagicMock()
        mock_terminal.id = "tid1"
        mock_terminal.tmux_session = "ses"
        mock_terminal.tmux_window = "win"
        mock_terminal.provider = "kiro_cli"
        mock_terminal.agent_profile = "dev"
        mock_terminal.last_active = None

        mock_db = MagicMock()
        mock_db.query.return_value.all.return_value = [mock_terminal]
        mock_session_class.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_session_class.return_value.__exit__ = MagicMock(return_value=False)

        result = list_all_terminals()

        assert len(result) == 1
        assert result[0]["id"] == "tid1"
        assert result[0]["provider"] == "kiro_cli"
