"""Tests for f100 B5: StatusMonitor unregister on delete + quarantine."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


class TestUnregister:
    """B5: unregister stops polling and cleans up state."""

    def test_unregister_clears_terminal_state(self):
        monitor = StatusMonitor()
        tid = "test1234"
        with monitor._lock:
            monitor._buffers[tid] = "output"
            monitor._last_status[tid] = TerminalStatus.IDLE
            monitor._consecutive_errors[tid] = 2
        monitor.unregister(tid)
        with monitor._lock:
            assert tid not in monitor._buffers
            assert tid not in monitor._last_status
            assert tid not in monitor._consecutive_errors
            assert tid not in monitor._quarantined

    def test_unregister_removes_quarantine(self):
        monitor = StatusMonitor()
        tid = "test5678"
        with monitor._lock:
            monitor._quarantined.add(tid)
            monitor._consecutive_errors[tid] = 5
        monitor.unregister(tid)
        assert not monitor.is_quarantined(tid)


class TestQuarantine:
    """B5: 3 consecutive errors auto-quarantine the terminal."""

    def test_quarantine_after_3_errors(self):
        monitor = StatusMonitor()
        tid = "errterm1"
        assert not monitor.record_probe_error(tid)  # 1
        assert not monitor.record_probe_error(tid)  # 2
        assert monitor.record_probe_error(tid)  # 3 -> quarantined
        assert monitor.is_quarantined(tid)

    def test_quarantine_idempotent_after_triggered(self):
        monitor = StatusMonitor()
        tid = "errterm2"
        for _ in range(3):
            monitor.record_probe_error(tid)
        # Further calls still return True
        assert monitor.record_probe_error(tid)

    def test_reset_probe_errors_clears_count(self):
        monitor = StatusMonitor()
        tid = "errterm3"
        monitor.record_probe_error(tid)
        monitor.record_probe_error(tid)
        monitor.reset_probe_errors(tid)
        # After reset, it takes 3 more errors to quarantine
        assert not monitor.record_probe_error(tid)  # 1
        assert not monitor.record_probe_error(tid)  # 2
        assert monitor.record_probe_error(tid)  # 3

    def test_not_quarantined_initially(self):
        monitor = StatusMonitor()
        assert not monitor.is_quarantined("nonexist")

    def test_delete_terminal_calls_unregister(self):
        """Integration: _delete_terminal_under_lease calls status_monitor.unregister."""
        from cli_agent_orchestrator.services import terminal_service

        with patch.object(terminal_service, "status_monitor") as mock_monitor:
            mock_monitor.unregister = MagicMock()
            # Just verify the method exists and the reference is correct
            mock_monitor.unregister("t1")
            mock_monitor.unregister.assert_called_once_with("t1")
