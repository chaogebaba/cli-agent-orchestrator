"""Unit tests for F114-proper: per-terminal agent config identity injection."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider


@pytest.fixture
def tmp_agents_dir(tmp_path):
    """Create a temporary KIRO_AGENTS_DIR with a base agent JSON."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    base_config = {
        "name": "developer",
        "description": "Dev agent",
        "mcpServers": {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "/usr/local/bin/cao-mcp-server",
                "args": [],
            }
        },
    }
    (agents_dir / "developer.json").write_text(json.dumps(base_config), encoding="utf-8")
    return agents_dir


@pytest.fixture
def provider():
    """Create a KiroCliProvider instance for testing."""
    return KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")


class TestKiroCliWritesPerTerminalAgentJson:
    """test_kiro_cli_writes_per_terminal_agent_json — AC1."""

    def test_produces_json_with_cao_terminal_id(self, provider, tmp_agents_dir):
        """Verify _write_per_terminal_agent_config produces JSON with env.CAO_TERMINAL_ID
        set to self.terminal_id for each MCP server."""
        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", tmp_agents_dir
            ),
            patch.dict(os.environ, {"CAO_INSTANCE_ID": "inst-001", "CAO_ENDPOINT": "http://127.0.0.1:9890"}),
        ):
            result_path = provider._write_per_terminal_agent_config()

        assert result_path.exists()
        config = json.loads(result_path.read_text(encoding="utf-8"))
        assert config["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "ab12cd34"
        assert config["mcpServers"]["cao-mcp-server"]["env"]["CAO_INSTANCE_ID"] == "inst-001"
        assert config["name"] == "cao-ab12cd34"
        assert result_path.name == "ab12cd34.kiro-agent.json"

    def test_slash_profile_uses_double_underscore(self, tmp_agents_dir):
        """Profile 'team/dev' reads from team__dev.json."""
        base_config = {
            "name": "team/dev",
            "mcpServers": {
                "cao-mcp-server": {
                    "type": "stdio",
                    "command": "/usr/local/bin/cao-mcp-server",
                    "args": [],
                }
            },
        }
        (tmp_agents_dir / "team__dev.json").write_text(json.dumps(base_config), encoding="utf-8")
        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "team/dev")

        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", tmp_agents_dir
            ),
            patch.dict(os.environ, {"CAO_INSTANCE_ID": "inst-001", "CAO_ENDPOINT": "http://127.0.0.1:9890"}),
        ):
            result_path = provider._write_per_terminal_agent_config()

        config = json.loads(result_path.read_text(encoding="utf-8"))
        assert config["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "ab12cd34"


class TestKiroCliLaunchUsesPerTerminalPath:
    """test_kiro_cli_launch_uses_per_terminal_path — AC1/AC9 prep."""

    def test_apply_per_terminal_agent_rewrites_argv(self, provider, tmp_path):
        """Verify _apply_per_terminal_agent replaces --agent value with per-terminal name."""
        agent_path = tmp_path / "ab12cd34.kiro-agent.json"
        command = ["kiro-cli", "chat", "--agent-engine", "v2", "--trust-all-tools", "--agent", "developer"]
        provider._apply_per_terminal_agent(command, agent_path)
        assert command[command.index("--agent") + 1] == "cao-ab12cd34"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.kiro_cli.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.kiro_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.kiro_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.kiro_cli.get_backend")
    async def test_initialize_sends_per_terminal_name(
        self,
        mock_backend,
        mock_wait_status,
        mock_wait_shell,
        mock_load_profile,
        provider,
        tmp_agents_dir,
    ):
        """Verify the tmux send-keys command contains --agent cao-<tid>."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load_profile.side_effect = FileNotFoundError("no profile")

        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", tmp_agents_dir
            ),
            patch.dict(os.environ, {"CAO_INSTANCE_ID": "inst-001", "CAO_ENDPOINT": "http://127.0.0.1:9890"}),
        ):
            await provider.initialize()

        send_keys_call = mock_backend.return_value.send_keys.call_args_list[0]
        command_sent = send_keys_call[0][2]
        assert "--agent cao-ab12cd34" in command_sent
        assert "--agent developer" not in command_sent


class TestTerminalDeleteCleansPerTerminalJson:
    """test_terminal_delete_cleans_per_terminal_json — AC6."""

    def test_cleanup_removes_per_terminal_json(self, provider, tmp_agents_dir):
        """Verify cleanup() removes <tid>.kiro-agent.json."""
        per_terminal = tmp_agents_dir / "ab12cd34.kiro-agent.json"
        per_terminal.write_text("{}", encoding="utf-8")

        with patch("cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", tmp_agents_dir):
            provider.cleanup()

        assert not per_terminal.exists()

    def test_cleanup_missing_ok(self, provider, tmp_agents_dir):
        """cleanup() does not raise if file already gone."""
        with patch("cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", tmp_agents_dir):
            provider.cleanup()  # Should not raise


class TestNoF114FallbackSites:
    """test_no_f114_fallback_sites — AC3/AC4."""

    def test_grep_hotfix_f114_returns_empty(self):
        """grep '# HOTFIX F114' must return no matches in src/."""
        src_dir = Path(__file__).parent.parent.parent / "src"
        result = subprocess.run(
            ["grep", "-r", "--include=*.py", "# HOTFIX F114", str(src_dir)],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "", f"HOTFIX F114 sites still exist:\n{result.stdout}"

    def test_terminal_id_fallback_deleted(self):
        """utils/terminal_id_fallback.py must not exist."""
        fallback_path = (
            Path(__file__).parent.parent.parent
            / "src"
            / "cli_agent_orchestrator"
            / "utils"
            / "terminal_id_fallback.py"
        )
        assert not fallback_path.exists(), f"File still exists: {fallback_path}"
