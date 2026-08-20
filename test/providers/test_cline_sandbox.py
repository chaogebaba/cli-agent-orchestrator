"""F329' AC7, AC8, AC15: Cline sandbox isolation unit tests.

AC7: Both subprocess.run argv lists contain --data-dir <dd>.
AC8: Materialized MCP settings are correct and resolved.
AC15: Cleanup deletes sandbox dir and nothing outside it.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def cline_provider():
    """Create a ClineCliProvider instance for testing."""
    from cli_agent_orchestrator.providers.cline_cli import ClineCliProvider

    provider = ClineCliProvider(
        terminal_id="abc12345",
        session_name="test-session",
        window_name="test-window",
        agent_profile="cline_dev",
    )
    return provider


class TestAC7_DataDirInSubprocessCalls:
    """AC7: Both subprocess.run argv lists contain --data-dir <dd>."""

    def test_build_base_args_includes_data_dir(self, cline_provider):
        """_build_base_args includes --data-dir pointing to the worker's sandbox."""
        with patch(
            "cli_agent_orchestrator.providers.cline_cli.load_agent_profile",
            side_effect=FileNotFoundError,
        ):
            args = cline_provider._build_base_args()

        assert "--data-dir" in args
        expected_dir = str(cline_provider._data_dir())
        assert expected_dir in args

    def test_snapshot_history_includes_data_dir(self, cline_provider):
        """_snapshot_history_ids passes --data-dir to subprocess."""
        import subprocess

        captured_args = []

        def fake_run(cmd, **kwargs):
            captured_args.extend(cmd)
            result = MagicMock()
            result.returncode = 1
            result.stderr = "not found"
            return result

        with patch("cli_agent_orchestrator.providers.cline_cli.subprocess.run", side_effect=fake_run):
            cline_provider._snapshot_history_ids()

        assert "--data-dir" in captured_args
        expected_dir = str(cline_provider._data_dir())
        assert expected_dir in captured_args


class TestAC8_MaterializedMCPSettings:
    """AC8: The materialized MCP settings entry is correct and resolved."""

    def test_mcp_settings_content(self, cline_provider, tmp_path):
        """cline_mcp_settings.json has correct command, env with terminal ID + token."""
        from cli_agent_orchestrator.providers.cline_cli import CLINE_SANDBOX_ROOT

        # Override the sandbox root to use tmp_path
        with patch(
            "cli_agent_orchestrator.providers.cline_cli.CLINE_SANDBOX_ROOT", tmp_path
        ):
            with patch(
                "cli_agent_orchestrator.providers.cline_cli._CLINE_USER_DATA",
                tmp_path / "fake_user_data",
            ):
                dd = tmp_path / cline_provider.terminal_id
                dd.mkdir(parents=True)
                (dd / "settings").mkdir(parents=True)

                with patch.dict(os.environ, {
                    "CAO_TERMINAL_TOKEN": "test_token_xyz",
                    "CAO_INSTANCE_ID": "inst_1",
                }):
                    with patch(
                        "cli_agent_orchestrator.utils.http.resolve_endpoint",
                        return_value="http://localhost:9889",
                    ):
                        with patch(
                            "cli_agent_orchestrator.providers.cline_cli.load_agent_profile",
                            side_effect=FileNotFoundError,
                        ):
                            cline_provider._materialize_mcp_settings(dd)

        settings_file = dd / "settings" / "cline_mcp_settings.json"
        assert settings_file.exists()

        settings = json.loads(settings_file.read_text())
        servers = settings["mcpServers"]
        assert "cao-mcp-server" in servers

        entry = servers["cao-mcp-server"]
        assert entry["disabled"] is False
        assert entry["command"]  # resolved, not empty
        assert entry["env"]["CAO_TERMINAL_ID"] == "abc12345"
        assert entry["env"]["CAO_TERMINAL_TOKEN"] == "test_token_xyz"
        assert entry["env"]["CAO_ENDPOINT"] == "http://localhost:9889"


class TestAC15_CleanupSandboxDir:
    """AC15: cleanup() deletes sandbox dir and nothing outside it."""

    def test_cleanup_removes_sandbox_dir(self, cline_provider, tmp_path):
        """cleanup() removes the data dir when guard passes."""
        from cli_agent_orchestrator.providers.cline_cli import CLINE_SANDBOX_ROOT

        sandbox_root = tmp_path / "cline-home"
        sandbox_root.mkdir()
        dd = sandbox_root / cline_provider.terminal_id
        dd.mkdir()
        (dd / "settings").mkdir()
        (dd / "settings" / "cline_mcp_settings.json").write_text("{}")

        # Create symlink targets (simulating user's real files)
        user_secrets = tmp_path / "user_secrets.json"
        user_secrets.write_text('{"key": "value"}')
        (dd / "secrets.json").symlink_to(user_secrets)

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.CLINE_SANDBOX_ROOT", sandbox_root
        ):
            with patch(
                "cli_agent_orchestrator.providers.cline_cli.SCRATCH_DIR", tmp_path
            ):
                cline_provider.cleanup()

        # Sandbox dir is gone
        assert not dd.exists()
        # But the symlink TARGET (user's secrets) still exists
        assert user_secrets.exists()

    def test_cleanup_refuses_wrong_parent(self, cline_provider, tmp_path):
        """cleanup() refuses if parent is not CLINE_SANDBOX_ROOT."""
        wrong_root = tmp_path / "wrong-root"
        wrong_root.mkdir()
        dd = wrong_root / cline_provider.terminal_id
        dd.mkdir()

        # Set CLINE_SANDBOX_ROOT to something different
        correct_root = tmp_path / "cline-home"
        correct_root.mkdir()

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.CLINE_SANDBOX_ROOT", correct_root
        ):
            with patch(
                "cli_agent_orchestrator.providers.cline_cli.SCRATCH_DIR", tmp_path
            ):
                # This should NOT delete dd because parent doesn't match
                cline_provider.cleanup()

        # Dir is still there — guard refused
        assert dd.exists()
