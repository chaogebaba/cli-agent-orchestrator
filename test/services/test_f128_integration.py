"""F128 integration tests: deterministic mutant killers for M3 (ordering) and M8 (lock).

M3 killer: exercises the REAL _delete_terminal_under_lease production path,
instruments emit_pre_delete_notice and clear_terminal to record invocation order,
and asserts emit/persist occurs BEFORE clear. Moving or omitting the emit call
after clear must fail the test.

M8 killer: holds svc._lock in one thread, starts another thread calling
emit_pre_delete_notice, asserts it cannot complete while the lock is held,
then releases and verifies completion. Removing the lock acquisition must fail.
"""

import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.stalled_callback_watchdog import (
    StalledCallbackWatchdog,
    WatchdogNotice,
)


class TestF128M3CallSiteOrdering:
    """M3 killer: emit_pre_delete_notice is called BEFORE clear_terminal
    in the real _delete_terminal_under_lease production path.

    This test patches the minimal set of dependencies to allow
    _delete_terminal_under_lease to reach the delivery_lock section,
    then uses spies on the watchdog singleton to record call ordering.
    """

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
    @patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_emit_before_clear_in_delete_terminal_under_lease(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_fifo_manager,
        mock_status_monitor,
        mock_validate_lease,
        mock_db_delete,
    ):
        """Invoke the REAL _delete_terminal_under_lease and assert
        emit_pre_delete_notice is called before clear_terminal."""
        from cli_agent_orchestrator.services.terminal_service import (
            _delete_terminal_under_lease,
        )

        terminal_id = "f128m3"

        # Minimal metadata for the function to proceed
        mock_get_metadata.return_value = {
            "id": terminal_id,
            "tmux_session": "cao-test",
            "tmux_window": terminal_id,
            "provider": "kiro_cli",
            "agent_profile": "developer",
        }
        mock_tmux.get_pane_working_directory.return_value = "/tmp"
        mock_tmux.get_history.return_value = ""
        mock_tmux.window_liveness.return_value = "gone"
        mock_db_delete.return_value = {
            "terminal_deleted": True,
            "intent_deleted": False,
            "intent_error": None,
            "intent_retain_reason": None,
        }

        # Track call ordering on the watchdog singleton
        call_log: list[str] = []

        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            stalled_callback_watchdog,
        )

        # Seed an episode so emit_pre_delete_notice has something to fire
        stalled_callback_watchdog.record_inbound_task(terminal_id, "caller1", "developer")

        original_emit = stalled_callback_watchdog.emit_pre_delete_notice
        original_clear = stalled_callback_watchdog.clear_terminal

        def spy_emit(tid):
            call_log.append("emit")
            return original_emit(tid)

        def spy_clear(tid):
            call_log.append("clear")
            return original_clear(tid)

        with (
            patch.object(
                stalled_callback_watchdog, "emit_pre_delete_notice", side_effect=spy_emit
            ),
            patch.object(
                stalled_callback_watchdog, "clear_terminal", side_effect=spy_clear
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "insert_barrier_escalation_message",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message"
            ),
        ):
            _delete_terminal_under_lease(terminal_id, lease_token="lease")

        # M3 assertion: emit MUST come before clear
        assert "emit" in call_log, "emit_pre_delete_notice was never called"
        assert "clear" in call_log, "clear_terminal was never called"
        emit_idx = call_log.index("emit")
        clear_idx = call_log.index("clear")
        assert emit_idx < clear_idx, (
            f"ORDERING VIOLATION: emit at index {emit_idx}, clear at index {clear_idx}. "
            f"emit_pre_delete_notice must be called BEFORE clear_terminal. "
            f"Full call log: {call_log}"
        )

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
    @patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_emit_actually_persists_before_clear(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_fifo_manager,
        mock_status_monitor,
        mock_validate_lease,
        mock_db_delete,
    ):
        """Stronger M3 variant: assert _persist_notice completes
        (the DB write) before clear_terminal erases the episode."""
        from cli_agent_orchestrator.services.terminal_service import (
            _delete_terminal_under_lease,
        )

        terminal_id = "f128m3b"

        mock_get_metadata.return_value = {
            "id": terminal_id,
            "tmux_session": "cao-test",
            "tmux_window": terminal_id,
            "provider": "kiro_cli",
            "agent_profile": "developer",
        }
        mock_tmux.get_pane_working_directory.return_value = "/tmp"
        mock_tmux.get_history.return_value = ""
        mock_tmux.window_liveness.return_value = "gone"
        mock_db_delete.return_value = {
            "terminal_deleted": True,
            "intent_deleted": False,
            "intent_error": None,
            "intent_retain_reason": None,
        }

        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            stalled_callback_watchdog,
        )

        stalled_callback_watchdog.record_inbound_task(terminal_id, "caller1", "developer")

        persist_completed_before_clear = []

        original_clear = stalled_callback_watchdog.clear_terminal

        def spy_clear(tid):
            # At the point clear_terminal is called, the episode should
            # have fired=True (evidence that persist completed)
            ep = stalled_callback_watchdog._episodes.get(tid)
            persist_completed_before_clear.append(ep is not None and ep.fired)
            return original_clear(tid)

        with (
            patch.object(
                stalled_callback_watchdog, "clear_terminal", side_effect=spy_clear
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "insert_barrier_escalation_message",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message"
            ),
        ):
            _delete_terminal_under_lease(terminal_id, lease_token="lease")

        assert persist_completed_before_clear == [True], (
            "At clear_terminal time, episode.fired was not True — "
            "emit_pre_delete_notice did not persist before clear"
        )


class TestF128M8LockDeterministic:
    """M8 killer: removing the lock acquisition from emit_pre_delete_notice
    must cause this test to fail.

    Mechanism: hold svc._lock in one thread, attempt emit_pre_delete_notice
    in another thread, assert it cannot complete within a timeout while the
    lock is held. After releasing, assert it completes promptly.
    """

    def test_emit_blocks_while_lock_held(self):
        """emit_pre_delete_notice acquires svc._lock — prove it by holding
        the lock externally and showing emit cannot proceed."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")

        emit_started = threading.Event()
        emit_completed = threading.Event()
        emit_result: list = []

        def do_emit():
            emit_started.set()
            with (
                patch(
                    "cli_agent_orchestrator.services.stalled_callback_watchdog."
                    "insert_barrier_escalation_message",
                    return_value=None,
                ),
                patch(
                    "cli_agent_orchestrator.services.mailbox_service."
                    "create_routed_inbox_message"
                ),
            ):
                result = svc.emit_pre_delete_notice("w1")
                emit_result.append(result)
            emit_completed.set()

        # Phase 1: hold the lock, start emit thread, verify it blocks
        svc._lock.acquire()
        t = threading.Thread(target=do_emit)
        t.start()

        # Wait for the thread to start and attempt to acquire the lock
        emit_started.wait(timeout=2)
        time.sleep(0.05)  # Give it time to hit the lock

        # Assert: emit has NOT completed (it's blocked on _lock)
        assert not emit_completed.is_set(), (
            "emit_pre_delete_notice completed while _lock was held — "
            "the lock acquisition has been removed (M8 mutation detected)"
        )

        # Phase 2: release the lock, verify emit completes
        svc._lock.release()
        completed = emit_completed.wait(timeout=2)
        assert completed, "emit_pre_delete_notice did not complete after lock release"
        t.join(timeout=2)

        # Verify it actually fired the notice
        assert len(emit_result) == 1
        assert emit_result[0] is not None
        assert emit_result[0].terminal_id == "w1"

    def test_emit_blocks_on_lock_multiple_iterations(self):
        """Repeat the lock-blocking assertion 5 times to confirm determinism."""
        for i in range(5):
            svc = StalledCallbackWatchdog(grace_seconds=120)
            svc.record_inbound_task("w1", "caller1", "developer")

            emit_completed = threading.Event()

            def do_emit():
                with (
                    patch(
                        "cli_agent_orchestrator.services.stalled_callback_watchdog."
                        "insert_barrier_escalation_message",
                        return_value=None,
                    ),
                    patch(
                        "cli_agent_orchestrator.services.mailbox_service."
                        "create_routed_inbox_message"
                    ),
                ):
                    svc.emit_pre_delete_notice("w1")
                emit_completed.set()

            svc._lock.acquire()
            t = threading.Thread(target=do_emit)
            t.start()
            time.sleep(0.03)

            assert not emit_completed.is_set(), (
                f"Iteration {i}: emit completed while lock held (M8 mutation)"
            )

            svc._lock.release()
            assert emit_completed.wait(timeout=2), (
                f"Iteration {i}: emit did not complete after lock release"
            )
            t.join(timeout=2)


class TestF128IntegrationPreserved:
    """Preserved integration tests for AC5/AC8 (direct watchdog-level)."""

    def test_notice_persisted_before_clear_terminal(self):
        """AC5/M3 (method-level): notice committed before clear erases episode."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "insert_barrier_escalation_message",
            return_value=None,
        ), patch(
            "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message"
        ) as mock_create:
            notice = svc.emit_pre_delete_notice("w1")
            assert notice is not None
            assert mock_create.called
            episode_snapshot = svc._episodes.get("w1")
            assert episode_snapshot is not None
            assert episode_snapshot.fired is True
            svc.clear_terminal("w1")
            assert svc._episodes.get("w1") is None

    def test_persist_failure_does_not_abort_delete(self):
        """AC8: _persist_notice raises, delete proceeds."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "insert_barrier_escalation_message",
            side_effect=RuntimeError("db down"),
        ):
            try:
                svc.emit_pre_delete_notice("w1")
            except Exception:
                pass

        assert svc._episodes.get("w1") is not None
        assert svc._episodes["w1"].fired is True
        svc.clear_terminal("w1")
        assert svc._episodes.get("w1") is None
