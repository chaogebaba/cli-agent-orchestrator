"""Tests for the shared agent-step substrate (issue #312, unit N0).

Mocks the terminal layer (create/send/wait/extract/delete) and asserts the
canonical sequence + the reliability contract: run_agent_step returns ONLY on
success and RAISES on every failure mode (RD-2.1) — it never returns a falsy
success.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.services.agent_step import (
    StepExecutionError,
    _CompletionOutcome,
    _wait_for_completion,
    run_agent_step,
)
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.terminal_service import OutputMode, TerminalInputBlockedError

_MODULE = "cli_agent_orchestrator.services.agent_step"


def _fake_terminal(terminal_id="abc12345"):
    t = MagicMock()
    t.id = terminal_id
    return t


def _patch_terminal_layer(
    *,
    created_id="abc12345",
    wait_results=(True, True),
    final_status=TerminalStatus.COMPLETED,
    output="the answer",
):
    """Context-manager bundle patching the terminal layer for run_agent_step.

    wait_results: side_effect list for wait_until_status calls (ready, complete).
    """
    create = patch(
        f"{_MODULE}.terminal_service.create_terminal",
        new=AsyncMock(return_value=_fake_terminal(created_id)),
    )
    send = patch(f"{_MODULE}.terminal_service.send_input", return_value=True)
    delete = patch(f"{_MODULE}.terminal_service.delete_terminal", return_value=True)
    get_output = patch(f"{_MODULE}.terminal_service.get_output", return_value=output)
    exit_cli = patch(f"{_MODULE}.terminal_service.exit_terminal_cli", return_value=None)
    wait = patch(
        f"{_MODULE}.wait_until_status",
        new=AsyncMock(side_effect=list(wait_results)),
    )
    status = patch(f"{_MODULE}.status_monitor.get_status", return_value=final_status)
    return create, send, delete, get_output, exit_cli, wait, status


class TestHappyPath:
    def test_create_per_call_runs_full_sequence_and_tears_down(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with (
            create as m_create,
            send as m_send,
            delete as m_delete,
            get_output as m_out,
            exit_cli as m_exit,
            wait,
            status,
        ):
            result = asyncio.run(run_agent_step("kiro_cli", "developer", "do the task"))

        assert isinstance(result, AgentStepResult)
        assert result.terminal_id == "abc12345"
        assert result.last_message == "the answer"
        assert result.status == TerminalStatus.COMPLETED
        # Canonical sequence: created, prompt sent, output extracted in LAST mode.
        m_create.assert_awaited_once()
        m_send.assert_called_once_with(
            "abc12345", "do the task", orchestration_type=OrchestrationType.HANDOFF
        )
        m_out.assert_called_once_with("abc12345", OutputMode.LAST)
        # Created-here + teardown default -> graceful exit THEN delete.
        m_exit.assert_called_once_with("abc12345")
        m_delete.assert_called_once_with("abc12345", registry=None)

    def test_teardown_false_skips_delete(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create, send, delete as m_delete, get_output, exit_cli as m_exit, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x", teardown=False))
        m_delete.assert_not_called()
        m_exit.assert_not_called()

    def test_teardown_false_cancelled_skips_delete(self):
        signal = asyncio.Event()
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()

        async def _cancel(*args, **kwargs):
            signal.set()
            return _CompletionOutcome.CANCELLED

        with (
            create,
            send,
            delete as m_delete,
            get_output,
            exit_cli as m_exit,
            wait,
            status,
            patch(f"{_MODULE}._wait_for_completion", new=AsyncMock(side_effect=_cancel)),
        ):
            with pytest.raises(StepExecutionError) as exc_info:
                asyncio.run(
                    run_agent_step(
                        "kiro_cli", "dev", "x", teardown=False, cancel_signal=signal
                    )
                )
        assert exc_info.value.kind == "cancelled"
        m_delete.assert_not_called()
        m_exit.assert_not_called()

    @pytest.mark.parametrize("cancelled", [False, True])
    def test_reuse_terminal_skips_create_and_delete(self, cancelled):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True,)
        )
        signal = asyncio.Event()
        outcome = _CompletionOutcome.CANCELLED if cancelled else _CompletionOutcome.COMPLETED

        async def _completion(*args, **kwargs):
            if cancelled:
                signal.set()
            return outcome

        with (
            create as m_create,
            send as m_send,
            delete as m_delete,
            get_output,
            exit_cli as m_exit,
            wait,
            status,
            patch(
                f"{_MODULE}._wait_for_completion",
                new=AsyncMock(side_effect=_completion),
            ),
        ):
            if cancelled:
                with pytest.raises(StepExecutionError) as exc_info:
                    asyncio.run(
                        run_agent_step(
                            "kiro_cli",
                            "dev",
                            "x",
                            reuse_terminal_id="reuse99",
                            cancel_signal=signal,
                        )
                    )
                assert exc_info.value.kind == "cancelled"
            else:
                result = asyncio.run(
                    run_agent_step("kiro_cli", "dev", "x", reuse_terminal_id="reuse99")
                )
                assert result.terminal_id == "reuse99"
        m_create.assert_not_awaited()
        m_delete.assert_not_called()
        # A reused terminal is owned by the caller — no graceful exit either.
        m_exit.assert_not_called()
        m_send.assert_called_once_with(
            "reuse99", "x", orchestration_type=OrchestrationType.HANDOFF
        )

    def test_dialog_block_surfaces_structured_non_retryable_error(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True,)
        )
        with (
            create,
            send as m_send,
            delete as m_delete,
            get_output,
            exit_cli,
            wait,
            status as m_status,
        ):
            m_send.side_effect = TerminalInputBlockedError("dialog")
            m_status.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
            with pytest.raises(StepExecutionError) as exc_info:
                asyncio.run(
                    run_agent_step("codex", "dev", "x", reuse_terminal_id="reuse99")
                )

        assert exc_info.value.kind == "input_blocked"
        assert exc_info.value.terminal_id == "reuse99"
        assert "waiting on a dialog" in str(exc_info.value)
        m_delete.assert_not_called()

    def test_draft_guard_deferral_surfaces_structured_retryable_error(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True,)
        )
        with create, send as m_send, delete, get_output, exit_cli, wait, status:
            m_send.side_effect = DeliveryDeferredError("composer unstable")
            with pytest.raises(StepExecutionError) as exc_info:
                asyncio.run(
                    run_agent_step("claude_code", "dev", "x", reuse_terminal_id="reuse99")
                )
        assert exc_info.value.kind == "delivery_deferred"
        assert exc_info.value.terminal_id == "reuse99"

    def test_reads_generation_after_send_and_admits_fresh_idle(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True,), final_status=TerminalStatus.IDLE
        )
        events = []
        with (
            create,
            send as m_send,
            delete,
            get_output,
            exit_cli,
            wait,
            status as m_status,
            patch(f"{_MODULE}.status_monitor.get_input_gen", return_value=7) as m_gen,
            patch(f"{_MODULE}.status_monitor.get_status_gen", return_value=7),
            patch(f"{_MODULE}.asyncio.sleep", new=AsyncMock()),
        ):
            m_send.side_effect = lambda *_args, **_kwargs: events.append("send") or True
            m_gen.side_effect = lambda *_args: events.append("gen") or 7
            m_status.side_effect = [
                TerminalStatus.PROCESSING,
                TerminalStatus.IDLE,
                TerminalStatus.IDLE,
            ]
            asyncio.run(run_agent_step("kiro_cli", "dev", "x", reuse_terminal_id="reuse99"))
        assert events == ["send", "gen"]

    def test_working_directory_forwarded_to_create(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create as m_create, send, delete, get_output, exit_cli, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x", working_directory="/tmp/wd"))
        assert m_create.await_args.kwargs["working_directory"] == "/tmp/wd"

    def test_no_session_name_creates_new_session(self):
        """Regression: session_name=None must create a NEW tmux session
        (new_session=True). Otherwise create_terminal auto-generates a name and
        then fails with 'Session not found' because it tries to add a window to
        a session that does not exist yet."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create as m_create, send, delete, get_output, exit_cli, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        assert m_create.await_args.kwargs["new_session"] is True
        assert m_create.await_args.kwargs["session_name"] is None

    def test_session_name_adds_to_existing_session(self):
        """A supplied session_name adds a window to that EXISTING session
        (new_session=False) and preserves the per-step env overlay."""
        env_vars = {"CAO_WORKFLOW_RUN_ID": "run-123", "CAO_WORKFLOW_GENERATION": "1"}
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create as m_create, send, delete, get_output, exit_cli, wait, status:
            asyncio.run(
                run_agent_step(
                    "kiro_cli", "dev", "x", session_name="cao-sup", env_vars=env_vars
                )
            )
        assert m_create.await_args.kwargs["new_session"] is False
        assert m_create.await_args.kwargs["session_name"] == "cao-sup"
        assert m_create.await_args.kwargs["env_vars"] == env_vars

    def test_caller_id_and_allowed_tools_forwarded_to_create(self):
        """caller_id (#284 callback routing) and inherited allowed_tools must
        reach create_terminal for handoff workers."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create as m_create, send, delete, get_output, exit_cli, wait, status:
            asyncio.run(
                run_agent_step(
                    "kiro_cli",
                    "dev",
                    "x",
                    session_name="cao-sup",
                    caller_id="sup-123",
                    allowed_tools=["fs_read", "fs_write"],
                )
            )
        assert m_create.await_args.kwargs["caller_id"] == "sup-123"
        assert m_create.await_args.kwargs["allowed_tools"] == ["fs_read", "fs_write"]

    def test_registry_threaded_to_delete_on_teardown(self):
        """The plugin registry passed to run_agent_step must reach delete_terminal
        so post_kill_terminal hooks dispatch (parity with the DELETE endpoint)."""
        from cli_agent_orchestrator.plugins import PluginRegistry

        sentinel = PluginRegistry()
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with create, send, delete as m_delete, get_output, exit_cli, wait, status:
            asyncio.run(run_agent_step("kiro_cli", "dev", "x", registry=sentinel))
        m_delete.assert_called_once_with("abc12345", registry=sentinel)


class TestFailureRaises:
    def test_completion_timeout_raises(self):
        """A typed completion timeout must raise, never return a falsy success."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True,),
            final_status=TerminalStatus.PROCESSING,
        )
        with (
            create,
            send,
            delete as m_delete,
            get_output,
            exit_cli as m_exit,
            wait,
            status,
        ):
            with pytest.raises(StepExecutionError, match="did not complete") as exc_info:
                asyncio.run(run_agent_step("kiro_cli", "dev", "x", timeout=0))
        # Timeout (ran long), with the live terminal carried structurally.
        assert exc_info.value.kind == "timeout"
        assert exc_info.value.terminal_id == "abc12345"
        m_exit.assert_not_called()
        m_delete.assert_not_called()

    def test_readiness_timeout_raises(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(False,),  # readiness times out before any input
        )
        with (
            create,
            send as m_send,
            delete as m_delete,
            get_output,
            exit_cli as m_exit,
            wait,
            status,
        ):
            with pytest.raises(StepExecutionError, match="ready status") as exc_info:
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        # Fail-fast: no prompt sent if the terminal never became ready.
        m_send.assert_not_called()
        assert exc_info.value.kind == "timeout"
        assert exc_info.value.terminal_id == "abc12345"
        m_exit.assert_not_called()
        m_delete.assert_not_called()

    def test_error_fails_fast_with_error_kind(self):
        """ERROR fails immediately rather than burning the nominal timeout."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True,),
            final_status=TerminalStatus.ERROR,
        )
        with (
            create,
            send,
            delete,
            get_output as m_out,
            exit_cli,
            wait,
            status,
            patch(f"{_MODULE}.asyncio.sleep", new=AsyncMock()) as m_sleep,
        ):
            with pytest.raises(StepExecutionError, match="ERROR status") as exc_info:
                asyncio.run(run_agent_step("kiro_cli", "dev", "x", timeout=3600))
        assert exc_info.value.kind == "error"
        assert exc_info.value.terminal_id == "abc12345"
        m_sleep.assert_not_awaited()
        m_out.assert_not_called()

    def test_error_wins_final_recheck_after_completed_outcome(self):
        """A completion-to-ERROR race must not be reported as success."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True,), final_status=TerminalStatus.COMPLETED
        )
        with (
            create,
            send,
            delete,
            get_output as m_out,
            exit_cli,
            wait,
            status as m_status,
            patch(f"{_MODULE}.status_monitor.get_status_gen", return_value=None),
        ):
            m_status.side_effect = [TerminalStatus.COMPLETED, TerminalStatus.ERROR]
            with pytest.raises(StepExecutionError, match="ERROR status") as exc_info:
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        # No output extraction once ERROR is detected.
        m_out.assert_not_called()
        assert exc_info.value.kind == "error"

    def test_create_failure_propagates(self):
        """A terminal-create failure is surfaced (ValueError), never swallowed."""
        create = patch(
            f"{_MODULE}.terminal_service.create_terminal",
            new=AsyncMock(side_effect=ValueError("session not found")),
        )
        with create:
            with pytest.raises(ValueError, match="session not found"):
                asyncio.run(run_agent_step("kiro_cli", "dev", "x"))


class TestTypedCompletionWait:
    @pytest.mark.asyncio
    async def test_cancel_interrupts_completion_wait_within_two_polls(self):
        signal = asyncio.Event()
        polls = 0

        def _status(_terminal_id):
            nonlocal polls
            polls += 1
            if polls == 1:
                signal.set()
            return TerminalStatus.PROCESSING

        with patch(f"{_MODULE}.status_monitor.get_status", side_effect=_status):
            outcome = await _wait_for_completion(
                "t1",
                input_gen=1,
                timeout=60,
                polling_interval=0,
                cancel_signal=signal,
            )
        assert outcome == _CompletionOutcome.CANCELLED
        assert polls <= 2

    def test_stale_idle_redraw_is_not_admitted(self):
        with (
            patch(f"{_MODULE}.status_monitor.get_status", return_value=TerminalStatus.IDLE),
            patch(f"{_MODULE}.status_monitor.get_status_gen", return_value=6),
            patch(f"{_MODULE}.time.time", side_effect=[0.0, 0.0, 2.0]),
            patch(f"{_MODULE}.asyncio.sleep", new=AsyncMock()),
        ):
            outcome = asyncio.run(_wait_for_completion("t1", input_gen=7, timeout=1.0))
        assert outcome == _CompletionOutcome.TIMEOUT

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (TerminalStatus.IDLE, _CompletionOutcome.TIMEOUT),
            (TerminalStatus.COMPLETED, _CompletionOutcome.COMPLETED),
        ],
    )
    def test_none_generation_is_fail_closed_only_for_idle(self, status, expected):
        with (
            patch(f"{_MODULE}.status_monitor.get_status", return_value=status),
            patch(f"{_MODULE}.status_monitor.get_status_gen", return_value=None),
            patch(f"{_MODULE}.time.time", side_effect=[0.0, 0.0, 2.0]),
            patch(f"{_MODULE}.asyncio.sleep", new=AsyncMock()),
        ):
            outcome = asyncio.run(_wait_for_completion("t1", input_gen=7, timeout=1.0))
        assert outcome == expected

    @pytest.mark.parametrize("gate", ["wait_rule", "retry_exhausted", "unknown_dialog"])
    def test_nonempty_waiting_gate_requires_user_input(self, gate):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True,), final_status=TerminalStatus.WAITING_USER_ANSWER
        )
        with (
            create,
            send,
            delete,
            get_output as m_out,
            exit_cli,
            wait,
            status,
            patch(
                "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
                return_value=gate,
            ),
            patch(f"{_MODULE}.asyncio.sleep", new=AsyncMock()) as m_sleep,
        ):
            with pytest.raises(StepExecutionError) as exc_info:
                asyncio.run(
                    run_agent_step(
                        "kiro_cli", "dev", "x", reuse_terminal_id="reuse99", timeout=3600
                    )
                )
        assert exc_info.value.kind == "waiting_user_input"
        assert exc_info.value.terminal_id == "reuse99"
        m_sleep.assert_not_awaited()
        m_out.assert_not_called()

    def test_empty_waiting_gate_keeps_waiting_until_completed(self):
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(True,), final_status=TerminalStatus.WAITING_USER_ANSWER
        )
        with (
            create,
            send,
            delete,
            get_output,
            exit_cli,
            wait,
            status as m_status,
            patch(f"{_MODULE}.status_monitor.get_status_gen", return_value=None),
            patch(
                "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
                return_value=None,
            ),
            patch(f"{_MODULE}.asyncio.sleep", new=AsyncMock()) as m_sleep,
        ):
            m_status.side_effect = [
                TerminalStatus.WAITING_USER_ANSWER,
                TerminalStatus.COMPLETED,
                TerminalStatus.COMPLETED,
            ]
            result = asyncio.run(
                run_agent_step(
                    "kiro_cli", "dev", "x", reuse_terminal_id="reuse99", timeout=3600
                )
            )
        assert result.status == TerminalStatus.COMPLETED
        m_sleep.assert_awaited_once()


class TestCancellationPhaseTable:
    @pytest.mark.parametrize(
        ("case", "should_cleanup"),
        [
            ("pre_readiness", False),
            ("success", True),
            ("cancelled", True),
            ("timeout", False),
            ("error", False),
            ("waiting_user_input", False),
            ("input_blocked", False),
            ("send_raw", False),
            ("extraction_raw", False),
        ],
    )
    def test_owned_terminal_phase_table(self, case, should_cleanup):
        ready = case != "pre_readiness"
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer(
            wait_results=(ready,)
        )
        outcome = {
            "success": _CompletionOutcome.COMPLETED,
            "cancelled": _CompletionOutcome.CANCELLED,
            "timeout": _CompletionOutcome.TIMEOUT,
            "error": _CompletionOutcome.ERROR,
            "waiting_user_input": _CompletionOutcome.WAITING_USER,
            "input_blocked": _CompletionOutcome.COMPLETED,
            "send_raw": _CompletionOutcome.COMPLETED,
            "extraction_raw": _CompletionOutcome.COMPLETED,
            "pre_readiness": _CompletionOutcome.COMPLETED,
        }[case]
        raw = RuntimeError(f"raw-{case}")
        with (
            create,
            send as m_send,
            delete as m_delete,
            get_output as m_output,
            exit_cli as m_exit,
            wait,
            status,
            patch(f"{_MODULE}._wait_for_completion", new=AsyncMock(return_value=outcome)),
        ):
            if case == "input_blocked":
                m_send.side_effect = TerminalInputBlockedError("blocked")
            elif case == "send_raw":
                m_send.side_effect = raw
            elif case == "extraction_raw":
                m_output.side_effect = raw

            if case == "success":
                result = asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
                assert result.status == TerminalStatus.COMPLETED
            else:
                expected = (
                    RuntimeError
                    if case in {"send_raw", "extraction_raw"}
                    else StepExecutionError
                )
                with pytest.raises(expected) as exc_info:
                    asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
                if case in {"send_raw", "extraction_raw"}:
                    assert exc_info.value is raw

        assert m_exit.call_count == int(should_cleanup)
        assert m_delete.call_count == int(should_cleanup)

    def test_cancel_before_send_skips_send_and_extraction(self):
        signal = asyncio.Event()
        signal.set()
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        with (
            create,
            send as m_send,
            delete as m_delete,
            get_output as m_output,
            exit_cli as m_exit,
            wait,
            status,
        ):
            with pytest.raises(StepExecutionError) as exc_info:
                asyncio.run(run_agent_step("kiro_cli", "dev", "x", cancel_signal=signal))
        assert exc_info.value.kind == "cancelled"
        m_send.assert_not_called()
        m_output.assert_not_called()
        m_exit.assert_called_once()
        m_delete.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase", ["send", "extraction"])
    async def test_cancel_during_failing_io_normalizes_and_tears_down(self, phase):
        signal = asyncio.Event()
        started = threading.Event()
        release = threading.Event()
        raw = RuntimeError(f"{phase} failed")
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()

        def _block_then_raise(*args, **kwargs):
            started.set()
            release.wait(timeout=5)
            raise raw

        with (
            create,
            send as m_send,
            delete as m_delete,
            get_output as m_output,
            exit_cli as m_exit,
            wait,
            status,
            patch(
                f"{_MODULE}._wait_for_completion",
                new=AsyncMock(return_value=_CompletionOutcome.COMPLETED),
            ),
        ):
            (m_send if phase == "send" else m_output).side_effect = _block_then_raise
            task = asyncio.create_task(
                run_agent_step("kiro_cli", "dev", "x", cancel_signal=signal)
            )
            assert await asyncio.to_thread(started.wait, 5)
            signal.set()
            release.set()
            with pytest.raises(StepExecutionError) as exc_info:
                await task

        assert exc_info.value.kind == "cancelled"
        assert exc_info.value.__cause__ is raw
        m_exit.assert_called_once()
        m_delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_during_blocked_send_normalizes_and_tears_down(self):
        signal = asyncio.Event()
        started = threading.Event()
        release = threading.Event()
        blocked = TerminalInputBlockedError("input blocked")
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()

        def _block_then_raise(*args, **kwargs):
            started.set()
            release.wait(timeout=5)
            raise blocked

        with (
            create,
            send as m_send,
            delete as m_delete,
            get_output,
            exit_cli as m_exit,
            wait,
            status,
        ):
            m_send.side_effect = _block_then_raise
            task = asyncio.create_task(
                run_agent_step("kiro_cli", "dev", "x", cancel_signal=signal)
            )
            assert await asyncio.to_thread(started.wait, 5)
            signal.set()
            release.set()
            with pytest.raises(StepExecutionError) as exc_info:
                await task

        assert exc_info.value.kind == "cancelled"
        assert exc_info.value.__cause__ is blocked
        m_exit.assert_called_once()
        m_delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_extraction_success_wins_concurrent_cancel(self):
        signal = asyncio.Event()
        started = threading.Event()
        release = threading.Event()
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()

        def _block_then_succeed(*args, **kwargs):
            started.set()
            release.wait(timeout=5)
            return "finished"

        with (
            create,
            send,
            delete as m_delete,
            get_output as m_output,
            exit_cli as m_exit,
            wait,
            status,
            patch(
                f"{_MODULE}._wait_for_completion",
                new=AsyncMock(return_value=_CompletionOutcome.COMPLETED),
            ),
        ):
            m_output.side_effect = _block_then_succeed
            task = asyncio.create_task(
                run_agent_step("kiro_cli", "dev", "x", cancel_signal=signal)
            )
            assert await asyncio.to_thread(started.wait, 5)
            signal.set()
            release.set()
            result = await task

        assert result.last_message == "finished"
        m_exit.assert_called_once()
        m_delete.assert_called_once()

class TestTeardownIsBestEffort:
    def test_teardown_failure_does_not_fail_successful_step(self):
        """A delete failure after a successful step is logged, not raised — the
        work is done and captured."""
        create, send, _delete, get_output, exit_cli, wait, status = _patch_terminal_layer()
        delete = patch(
            f"{_MODULE}.terminal_service.delete_terminal",
            side_effect=Exception("kill failed"),
        )
        with create, send, delete, get_output, exit_cli, wait, status:
            result = asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        assert result.status == TerminalStatus.COMPLETED
        assert result.last_message == "the answer"

    def test_graceful_exit_failure_does_not_fail_step_and_still_deletes(self):
        """A failure sending the graceful exit must be logged, not raised, and
        must NOT prevent the subsequent delete (best-effort exit-then-delete)."""
        create, send, delete, get_output, _exit, wait, status = _patch_terminal_layer()
        exit_cli = patch(
            f"{_MODULE}.terminal_service.exit_terminal_cli",
            side_effect=Exception("exit boom"),
        )
        with create, send, delete as m_delete, get_output, exit_cli, wait, status:
            result = asyncio.run(run_agent_step("kiro_cli", "dev", "x"))
        assert result.status == TerminalStatus.COMPLETED
        # Exit failed but delete still ran.
        m_delete.assert_called_once_with("abc12345", registry=None)

    def test_on_terminal_created_callback_failure_does_not_fail_step(self):
        """F9(b): a raising ``on_terminal_created`` callback (BR-31 sweep
        bookkeeping) must never propagate into ``run_agent_step`` — it is
        best-effort, logged and swallowed, and the step still completes."""
        create, send, delete, get_output, exit_cli, wait, status = _patch_terminal_layer()

        def _boom_callback(terminal_id):
            raise RuntimeError("sweep bookkeeping boom")

        with create, send, delete, get_output, exit_cli, wait, status:
            result = asyncio.run(
                run_agent_step("kiro_cli", "dev", "x", on_terminal_created=_boom_callback)
            )
        assert result.status == TerminalStatus.COMPLETED
        assert result.last_message == "the answer"
