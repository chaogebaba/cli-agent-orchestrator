"""F343: Cline provider busy-gate and abort-status detection.

Defect 1 (busy-gate): _after_dispatch_commit_locked must NOT send Ctrl-D (EOF)
when the pane is busy (cline running). Doing so closes stdin on the running
child process (e.g. ssh), corrupting the tool call.

Defect 2 (abort-status): get_status must detect ABORT_LINE in the pane tail
and return ERROR instead of COMPLETED, so dead lanes are not misreported.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.cline_cli import (
    _ABORT_SCAN_LINES,
    ABORT_LINE,
    DISPATCHER_IDLE_CMD,
    ClineCliProvider,
)


@pytest.fixture
def provider() -> ClineCliProvider:
    """Minimal ClineCliProvider with backend mocks stubbed out."""
    p = ClineCliProvider(
        terminal_id="f343test",
        session_name="test-session",
        window_name="test-window",
        agent_profile="cline_dev",
    )
    p._initialized = True
    p.shell_baseline = "zsh"
    # Suppress native status (tmux backend always returns None).
    p._resolve_native_status = lambda: None  # type: ignore[method-assign]
    return p


# ─── Defect 1: Busy-gate (EOF suppression) ────────────────────────────────────


class TestEofSuppression:
    """_after_dispatch_commit_locked must suppress Ctrl-D when pane is busy."""

    def test_eof_sent_when_pane_idle(self, provider: ClineCliProvider) -> None:
        """EOF is sent normally when pane command is the dispatcher's cat."""
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = DISPATCHER_IDLE_CMD

        patcher_be = patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        )
        patcher_sub = patch(
            "cli_agent_orchestrator.providers.cline_cli.subprocess.run",
            return_value=MagicMock(returncode=1, stderr=""),
        )
        patcher_be.start()
        patcher_sub.start()
        try:
            provider._after_dispatch_commit_locked()
            # Give the daemon thread time to execute.
            time.sleep(0.5)
            mock_backend.send_special_key.assert_called_once_with(
                "test-session", "test-window", "C-d"
            )
        finally:
            patcher_sub.stop()
            patcher_be.stop()

    def test_eof_suppressed_when_pane_busy(self, provider: ClineCliProvider) -> None:
        """EOF is NOT sent when pane is running cline (busy)."""
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = "cline"

        patcher_be = patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        )
        patcher_sub = patch(
            "cli_agent_orchestrator.providers.cline_cli.subprocess.run",
            return_value=MagicMock(returncode=1, stderr=""),
        )
        patcher_be.start()
        patcher_sub.start()
        try:
            provider._after_dispatch_commit_locked()
            time.sleep(0.5)
            mock_backend.send_special_key.assert_not_called()
        finally:
            patcher_sub.stop()
            patcher_be.stop()

    def test_eof_suppressed_when_pane_ssh(self, provider: ClineCliProvider) -> None:
        """EOF is NOT sent when pane shows an ssh child running."""
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = "ssh"

        patcher_be = patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        )
        patcher_sub = patch(
            "cli_agent_orchestrator.providers.cline_cli.subprocess.run",
            return_value=MagicMock(returncode=1, stderr=""),
        )
        patcher_be.start()
        patcher_sub.start()
        try:
            provider._after_dispatch_commit_locked()
            time.sleep(0.5)
            mock_backend.send_special_key.assert_not_called()
        finally:
            patcher_sub.stop()
            patcher_be.stop()


# ─── Defect 1 (continued): wait_until_input_ready ─────────────────────────────


class TestWaitUntilInputReady:
    """wait_until_input_ready gates paste on the dispatcher being at cat."""

    @pytest.mark.asyncio
    async def test_returns_true_immediately_when_idle(self, provider: ClineCliProvider) -> None:
        """Returns True immediately if pane is already at cat."""
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = DISPATCHER_IDLE_CMD

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        ):
            result = await provider.wait_until_input_ready(timeout=1.0)

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self, provider: ClineCliProvider) -> None:
        """Returns False when pane stays busy beyond timeout."""
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = "cline"

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        ):
            start = time.time()
            result = await provider.wait_until_input_ready(timeout=0.5)
            elapsed = time.time() - start

        assert result is False
        assert elapsed >= 0.4  # Waited close to the timeout

    @pytest.mark.asyncio
    async def test_returns_true_when_pane_becomes_idle(self, provider: ClineCliProvider) -> None:
        """Returns True once pane transitions from busy to idle."""
        mock_backend = MagicMock()
        # First 2 calls: busy, then idle.
        mock_backend.get_pane_current_command.side_effect = [
            "cline",
            "cline",
            DISPATCHER_IDLE_CMD,
            DISPATCHER_IDLE_CMD,
        ]

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        ):
            result = await provider.wait_until_input_ready(timeout=5.0)

        assert result is True


# ─── Defect 2: Abort-status detection ─────────────────────────────────────────


class TestAbortStatusDetection:
    """get_status returns ERROR when ABORT_LINE is in the pane tail."""

    def test_abort_line_returns_error(self, provider: ClineCliProvider) -> None:
        """A dispatched task with ABORT_LINE in output → ERROR, not COMPLETED."""
        provider._task_dispatched_flag = True
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = DISPATCHER_IDLE_CMD

        # Simulated pane output with abort line.
        output_lines = [
            "[run_commands] sleep 28; ssh box@cursor-1 'pgrep ...'",
            "   ⎿ ok",
            ABORT_LINE,
            "",
        ]
        output = "\n".join(output_lines)

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        ):
            status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_no_abort_line_returns_completed(self, provider: ClineCliProvider) -> None:
        """A dispatched task with normal output → COMPLETED as before."""
        provider._task_dispatched_flag = True
        provider._message_count = 1
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = DISPATCHER_IDLE_CMD

        output_lines = [
            "Task completed successfully.",
            "All tests pass.",
            "",
        ]
        output = "\n".join(output_lines)

        with (
            patch(
                "cli_agent_orchestrator.providers.cline_cli.get_backend",
                return_value=mock_backend,
            ),
            patch(
                "cli_agent_orchestrator.providers.cline_cli.subprocess.run",
                return_value=MagicMock(returncode=1, stderr=""),
            ),
        ):
            status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_abort_line_beyond_scan_window_not_detected(self, provider: ClineCliProvider) -> None:
        """ABORT_LINE more than _ABORT_SCAN_LINES back is NOT detected."""
        provider._task_dispatched_flag = True
        provider._message_count = 1
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = DISPATCHER_IDLE_CMD

        # Put the abort line far above the scan window, then pad with good output.
        output_lines = [ABORT_LINE] + ["normal output line"] * (_ABORT_SCAN_LINES + 10)
        output = "\n".join(output_lines)

        with (
            patch(
                "cli_agent_orchestrator.providers.cline_cli.get_backend",
                return_value=mock_backend,
            ),
            patch(
                "cli_agent_orchestrator.providers.cline_cli.subprocess.run",
                return_value=MagicMock(returncode=1, stderr=""),
            ),
        ):
            status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_abort_line_with_escape_sequences(self, provider: ClineCliProvider) -> None:
        """ABORT_LINE detection works even when wrapped in ANSI escapes."""
        provider._task_dispatched_flag = True
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = DISPATCHER_IDLE_CMD

        # Simulate cline's dim styling: \x1b[2m prefix, \x1b[0m suffix.
        styled_abort = f"\x1b[2m{ABORT_LINE}\x1b[0m"
        output = f"some output\n{styled_abort}\n"

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        ):
            status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_empty_output_returns_completed(self, provider: ClineCliProvider) -> None:
        """Empty output (no abort line) still returns COMPLETED."""
        provider._task_dispatched_flag = True
        provider._message_count = 1
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = DISPATCHER_IDLE_CMD

        with (
            patch(
                "cli_agent_orchestrator.providers.cline_cli.get_backend",
                return_value=mock_backend,
            ),
            patch(
                "cli_agent_orchestrator.providers.cline_cli.subprocess.run",
                return_value=MagicMock(returncode=1, stderr=""),
            ),
        ):
            status = provider.get_status("")

        assert status == TerminalStatus.COMPLETED

    def test_idle_before_dispatch_remains_idle(self, provider: ClineCliProvider) -> None:
        """Before any task is dispatched, idle pane → IDLE (not COMPLETED)."""
        provider._task_dispatched_flag = False
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = DISPATCHER_IDLE_CMD

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        ):
            status = provider.get_status("")

        assert status == TerminalStatus.IDLE


# ─── _pane_cmd helper ─────────────────────────────────────────────────────────


class TestPaneCmd:
    """_pane_cmd wraps get_pane_current_command with error handling."""

    def test_returns_command_normally(self, provider: ClineCliProvider) -> None:
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = "cline"

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        ):
            assert provider._pane_cmd() == "cline"

    def test_returns_empty_on_none(self, provider: ClineCliProvider) -> None:
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.return_value = None

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        ):
            assert provider._pane_cmd() == ""

    def test_returns_empty_on_exception(self, provider: ClineCliProvider) -> None:
        mock_backend = MagicMock()
        mock_backend.get_pane_current_command.side_effect = RuntimeError("tmux dead")

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.get_backend",
            return_value=mock_backend,
        ):
            assert provider._pane_cmd() == ""
