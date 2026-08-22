"""F127: Unit tests for interrupt_terminal MCP tool.

Covers AC7-AC12. Mutant kills: M6, M7, M8, M9, M11.
"""
import asyncio
from unittest.mock import patch, MagicMock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus


def _run(coro):
    """Run async in sync test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def interrupt_fn():
    """Import interrupt_terminal with env set."""
    with patch.dict("os.environ", {"CAO_TERMINAL_ID": "aaaaaaaa"}):
        from cli_agent_orchestrator.mcp_server.server import interrupt_terminal
        return interrupt_terminal


class TestInterruptGuards:
    def test_rejects_not_found(self, interrupt_fn):
        """Terminal not found -> failure."""
        with patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value=None,
        ):
            result = _run(interrupt_fn("deadbeef"))
        assert result["success"] is False
        assert "not found" in result["message"].lower()

    def test_rejects_idle_terminal(self, interrupt_fn):
        """AC8: IDLE terminal -> {success: false}, kills M6."""
        mock_status = MagicMock()
        mock_status.get_status.return_value = TerminalStatus.IDLE
        with patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"id": "aabbccdd", "provider": "kiro_cli", "init_state": "ready"},
        ), patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor", mock_status
        ):
            result = _run(interrupt_fn("aabbccdd"))
        assert result["success"] is False
        assert "not processing" in result["message"].lower()

    def test_rejects_init_pending(self, interrupt_fn):
        """AC9: init_pending -> {success: false}, kills M7/M11."""
        with patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"id": "aabbccdd", "provider": "kiro_cli", "init_state": "init_pending"},
        ):
            result = _run(interrupt_fn("aabbccdd"))
        assert result["success"] is False
        assert "initialization" in result["message"].lower()

    def test_rejects_waiting_user_answer(self, interrupt_fn):
        """WAITING_USER_ANSWER -> refusal."""
        mock_status = MagicMock()
        mock_status.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        with patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"id": "aabbccdd", "provider": "kiro_cli", "init_state": "ready"},
        ), patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor", mock_status
        ):
            result = _run(interrupt_fn("aabbccdd"))
        assert result["success"] is False


class TestInterruptSuccess:
    def test_sends_keys_and_transitions(self, interrupt_fn):
        """AC7: PROCESSING terminal -> sends keys, transitions away."""
        call_count = [0]

        def status_side_effect(tid):
            call_count[0] += 1
            if call_count[0] >= 2:
                return TerminalStatus.IDLE
            return TerminalStatus.PROCESSING

        mock_status = MagicMock()
        mock_status.get_status.side_effect = status_side_effect
        with patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"id": "aabbccdd", "provider": "kiro_cli", "init_state": "ready"},
        ), patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor", mock_status
        ), patch(
            "cli_agent_orchestrator.mcp_server.server._send_terminal_key"
        ) as mock_send:
            result = _run(interrupt_fn("aabbccdd"))

        assert result["success"] is True
        assert result["final_status"] != "processing"
        mock_send.assert_called_with("aabbccdd", "C-c")

    def test_claude_code_sends_escape(self, interrupt_fn):
        """AC11: claude_code terminal sends Escape, not C-c. Kills M8."""
        call_count = [0]

        def status_side_effect(tid):
            call_count[0] += 1
            if call_count[0] >= 2:
                return TerminalStatus.IDLE
            return TerminalStatus.PROCESSING

        mock_status = MagicMock()
        mock_status.get_status.side_effect = status_side_effect
        with patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"id": "aabbccdd", "provider": "claude_code", "init_state": "ready"},
        ), patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor", mock_status
        ), patch(
            "cli_agent_orchestrator.mcp_server.server._send_terminal_key"
        ) as mock_send:
            result = _run(interrupt_fn("aabbccdd"))

        assert result["success"] is True
        mock_send.assert_called_with("aabbccdd", "Escape")


class TestInterruptTimeout:
    def test_timeout_returns_failure(self, interrupt_fn):
        """AC12: provider ignores key -> timeout, returns failure. Kills M9."""
        mock_status = MagicMock()
        mock_status.get_status.return_value = TerminalStatus.PROCESSING

        # Speed up the test by patching asyncio.sleep to be instant
        real_sleep = asyncio.sleep
        async def fast_sleep(s):
            pass  # no-op for speed

        with patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"id": "aabbccdd", "provider": "kiro_cli", "init_state": "ready"},
        ), patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor", mock_status
        ), patch(
            "cli_agent_orchestrator.mcp_server.server._send_terminal_key"
        ), patch(
            "asyncio.sleep", side_effect=fast_sleep
        ):
            result = _run(interrupt_fn("aabbccdd"))

        assert result["success"] is False
        assert result["final_status"] == "processing"
        assert "timeout" in result["message"].lower()
