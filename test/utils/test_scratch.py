"""F273: Tests for utils/scratch.py — scratch_dir() central resolver."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.utils.scratch import (
    ScratchUnavailableError,
    _is_data_mounted,
    scratch_dir,
    scratch_dir_str,
)


class TestIsDataMounted:
    """Unit tests for the mount-check helper."""

    def test_mounted_returns_true(self) -> None:
        """findmnt exits 0 with /data in stdout → True."""
        with patch("cli_agent_orchestrator.utils.scratch.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "/data\n"
            assert _is_data_mounted() is True

    def test_not_mounted_returns_false(self) -> None:
        """findmnt exits non-zero → False."""
        with patch("cli_agent_orchestrator.utils.scratch.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert _is_data_mounted() is False

    def test_findmnt_timeout_returns_false(self) -> None:
        """Timeout in findmnt → graceful False."""
        import subprocess

        with patch(
            "cli_agent_orchestrator.utils.scratch.subprocess.run",
            side_effect=subprocess.TimeoutExpired("findmnt", 5),
        ):
            assert _is_data_mounted() is False

    def test_findmnt_not_found_returns_false(self) -> None:
        """Missing findmnt binary → graceful False."""
        with patch(
            "cli_agent_orchestrator.utils.scratch.subprocess.run",
            side_effect=FileNotFoundError("findmnt"),
        ):
            assert _is_data_mounted() is False


class TestScratchDir:
    """Unit tests for scratch_dir() resolver."""

    def test_returns_data_path_when_mounted(self, tmp_path) -> None:
        """When /data is mounted, returns /data/cao-scratch/tmp."""
        with patch(
            "cli_agent_orchestrator.utils.scratch._is_data_mounted", return_value=True
        ), patch(
            "cli_agent_orchestrator.utils.scratch._SCRATCH_TMP", tmp_path / "scratch"
        ):
            result = scratch_dir()
            assert result == tmp_path / "scratch"
            assert result.exists()

    def test_raises_when_unmounted(self) -> None:
        """When /data is NOT mounted, raises ScratchUnavailableError."""
        with patch(
            "cli_agent_orchestrator.utils.scratch._is_data_mounted", return_value=False
        ):
            with pytest.raises(ScratchUnavailableError, match="F273"):
                scratch_dir()

    def test_error_message_mentions_data_mount(self) -> None:
        """Error message instructs user to plug in /data."""
        with patch(
            "cli_agent_orchestrator.utils.scratch._is_data_mounted", return_value=False
        ):
            with pytest.raises(ScratchUnavailableError, match="sudo systemctl start data.mount"):
                scratch_dir()

    def test_error_message_bans_tmp_fallback(self) -> None:
        """Error message explicitly states /tmp fallback is banned."""
        with patch(
            "cli_agent_orchestrator.utils.scratch._is_data_mounted", return_value=False
        ):
            with pytest.raises(ScratchUnavailableError, match="/tmp fallback is banned"):
                scratch_dir()


class TestScratchDirStr:
    """Convenience wrapper returns str."""

    def test_returns_string(self, tmp_path) -> None:
        with patch(
            "cli_agent_orchestrator.utils.scratch._is_data_mounted", return_value=True
        ), patch(
            "cli_agent_orchestrator.utils.scratch._SCRATCH_TMP", tmp_path / "s"
        ):
            result = scratch_dir_str()
            assert isinstance(result, str)
            assert "s" in result
