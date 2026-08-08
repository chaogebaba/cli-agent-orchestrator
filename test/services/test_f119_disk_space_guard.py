"""F119 — disk-space pre-flight guard in create_terminal().

Tests:
  a) shutil.disk_usage reports < 3 GB free  → RuntimeError('disk_space_low: ...')
  b) shutil.disk_usage reports >= 3 GB free → no exception
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.terminal_service import (
    DISK_SPACE_FLOOR_GB,
    _preflight_disk_space,
)


def _make_usage(free_gb: float):
    """Return a namedtuple-like with .free in bytes."""
    free_bytes = int(free_gb * (1024**3))

    class _Usage:
        total = int(500 * (1024**3))
        used = total - free_bytes
        free = free_bytes

    return _Usage()


class TestPreflightDiskSpace:
    """Unit tests for _preflight_disk_space helper."""

    @patch("cli_agent_orchestrator.services.terminal_service.shutil.disk_usage")
    def test_raises_when_below_floor(self, mock_disk_usage):
        """(a) Free space below floor → RuntimeError with 'disk_space_low'."""
        mock_disk_usage.return_value = _make_usage(2.5)
        with pytest.raises(RuntimeError, match="disk_space_low"):
            _preflight_disk_space("/some/path")
        mock_disk_usage.assert_called_once_with("/some/path")

    @patch("cli_agent_orchestrator.services.terminal_service.shutil.disk_usage")
    def test_passes_when_above_floor(self, mock_disk_usage):
        """(b) Free space above floor → no exception."""
        mock_disk_usage.return_value = _make_usage(10.0)
        _preflight_disk_space("/some/path")  # should not raise
        mock_disk_usage.assert_called_once_with("/some/path")

    @patch("cli_agent_orchestrator.services.terminal_service.shutil.disk_usage")
    def test_passes_at_exact_floor(self, mock_disk_usage):
        """Boundary: exactly at floor → no exception (< not <=)."""
        mock_disk_usage.return_value = _make_usage(DISK_SPACE_FLOOR_GB)
        _preflight_disk_space("/some/path")  # should not raise

    @patch("cli_agent_orchestrator.services.terminal_service.shutil.disk_usage")
    def test_custom_floor(self, mock_disk_usage):
        """Custom floor_gb parameter is respected."""
        mock_disk_usage.return_value = _make_usage(4.5)
        with pytest.raises(RuntimeError, match="disk_space_low"):
            _preflight_disk_space("/some/path", floor_gb=5.0)
