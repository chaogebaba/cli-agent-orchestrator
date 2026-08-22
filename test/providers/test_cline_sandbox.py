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

    def test_mcp_settings_uses_resolved_command(self, cline_provider, tmp_path):
        """M6 kill: materialized command comes from resolve_cao_mcp_command, not hardcoded."""
        # Mock resolve_cao_mcp_command to return a known sentinel value.
        # If the provider hardcodes the path instead of calling the resolver,
        # the settings file will NOT contain our sentinel.
        SENTINEL_CMD = "/resolved/by/mcp_resolution/cao-mcp-server"
        SENTINEL_ARGS = ["--resolved-arg"]

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.CLINE_SANDBOX_ROOT", tmp_path
        ), patch(
            "cli_agent_orchestrator.providers.cline_cli._CLINE_USER_DATA",
            tmp_path / "fake_user_data",
        ), patch.dict(os.environ, {
            "CAO_TERMINAL_TOKEN": "test_token_xyz",
            "CAO_INSTANCE_ID": "inst_1",
        }), patch(
            "cli_agent_orchestrator.utils.http.resolve_endpoint",
            return_value="http://localhost:9889",
        ), patch(
            "cli_agent_orchestrator.providers.cline_cli.load_agent_profile",
            side_effect=FileNotFoundError,
        ), patch(
            "cli_agent_orchestrator.providers.cline_cli.resolve_cao_mcp_command",
            return_value=(SENTINEL_CMD, SENTINEL_ARGS),
        ):
            dd = tmp_path / cline_provider.terminal_id
            dd.mkdir(parents=True)
            (dd / "settings").mkdir(parents=True)
            cline_provider._materialize_mcp_settings(dd)

        settings_file = dd / "settings" / "cline_mcp_settings.json"
        assert settings_file.exists()

        settings = json.loads(settings_file.read_text())
        entry = settings["mcpServers"]["cao-mcp-server"]
        assert entry["disabled"] is False

        # AC8 tightened: command MUST equal the resolver's return value
        assert entry["command"] == SENTINEL_CMD, (
            f"Materialized command {entry['command']!r} != resolver return {SENTINEL_CMD!r}. "
            f"M6 mutant (hardcoded path) would produce the hardcoded value here."
        )
        assert entry["args"] == SENTINEL_ARGS
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

    def test_cleanup_uses_shutil_rmtree(self, cline_provider, tmp_path):
        """F338 S1: cleanup() must delete the sandbox dir via shutil.rmtree,
        never a spawned `rm -rf` subprocess (M7 ledger: functionally equivalent
        for symlink safety, but D5 mandates no process-spawn in cleanup).
        """
        sandbox_root = tmp_path / "cline-home"
        sandbox_root.mkdir()
        dd = sandbox_root / cline_provider.terminal_id
        dd.mkdir()
        (dd / "marker.txt").write_text("should be removed via rmtree")

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.CLINE_SANDBOX_ROOT", sandbox_root
        ), patch(
            "cli_agent_orchestrator.providers.cline_cli.SCRATCH_DIR", tmp_path
        ), patch(
            "cli_agent_orchestrator.providers.cline_cli.shutil.rmtree"
        ) as mock_rmtree, patch(
            "cli_agent_orchestrator.providers.cline_cli.subprocess.run"
        ) as mock_run:
            cline_provider.cleanup()

        mock_rmtree.assert_called_once_with(dd)
        mock_run.assert_not_called()

    def test_cleanup_refuses_wrong_parent(self, cline_provider, tmp_path):
        """cleanup() refuses if _data_dir()'s parent doesn't match CLINE_SANDBOX_ROOT.

        Fixed for M8: _data_dir() and the test's dd must agree so the guard
        is actually exercised. We create the dir at _data_dir()'s resolved path,
        then change CLINE_SANDBOX_ROOT so the guard's comparison fails.
        """
        # Phase 1: create the sandbox dir at the "original" root
        original_root = tmp_path / "cline-home"
        original_root.mkdir()
        dd = original_root / cline_provider.terminal_id
        dd.mkdir()
        (dd / "marker.txt").write_text("should survive")

        # Phase 2: patch CLINE_SANDBOX_ROOT to a DIFFERENT path
        # so the guard sees dd.parent != CLINE_SANDBOX_ROOT and refuses.
        different_root = tmp_path / "different-root"
        different_root.mkdir()

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.CLINE_SANDBOX_ROOT", different_root
        ):
            with patch(
                "cli_agent_orchestrator.providers.cline_cli.SCRATCH_DIR", tmp_path
            ):
                # _data_dir() = different_root / terminal_id (doesn't exist)
                # But we need _data_dir() to point at dd for the guard to fire.
                # So patch it directly:
                pass

        # Better approach: override _data_dir() to return dd, but set
        # CLINE_SANDBOX_ROOT to something different from dd's parent.
        with patch.object(
            cline_provider, "_data_dir", return_value=dd
        ):
            with patch(
                "cli_agent_orchestrator.providers.cline_cli.CLINE_SANDBOX_ROOT", different_root
            ):
                with patch(
                    "cli_agent_orchestrator.providers.cline_cli.SCRATCH_DIR", tmp_path
                ):
                    cline_provider.cleanup()

        # Dir still exists — guard refused deletion because
        # dd.parent (original_root) != CLINE_SANDBOX_ROOT (different_root)
        assert dd.exists(), "Guard failed to refuse — dir was deleted despite parent mismatch"
