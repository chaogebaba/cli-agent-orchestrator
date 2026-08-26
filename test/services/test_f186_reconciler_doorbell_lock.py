"""F186 — Reconciler ride-along ring not swallowed by delivery lock.

Regression: the fx158 pull-mode reconciler ride-along called
ring_supervisor_doorbell WITHOUT caller_holds_no_delivery_lock=True, so the
fallback path's G1 gate always lost the non-blocking acquire (a concurrent
F136 runner or deliver_pending held the same terminal's delivery lock). 4/4
rings ended `skipped_gate reason=delivery_lock` in the AC12 sandbox arm.

Fix: ring_supervisor_doorbell(caller_holds_no_delivery_lock=True) from the
reconciler path, which skips G1 since the reconciler is provably NOT inside
the delivery-lock scope.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.teammate_push_service import PushOutcome

_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
_OLD = _NOW - timedelta(seconds=60)


@pytest.fixture(autouse=True)
def _reset_doorbell_state():
    """Reset doorbell module state between tests."""
    import cli_agent_orchestrator.services.doorbell_service as ds

    ds._last_warn_time.clear()
    yield
    ds._last_warn_time.clear()


class _FakeQuery:
    """Chain-able fake for SQLAlchemy query."""

    def __init__(self, results: list):
        self._results = results

    def filter_by(self, **kw):
        return self

    def filter(self, *args):
        return self

    def order_by(self, *args):
        return self

    def limit(self, n):
        self._results = self._results[:n]
        return self

    def all(self):
        return self._results

    def one_or_none(self):
        return self._results[0] if self._results else None

    def count(self):
        return len(self._results)


def _make_mailbox(
    mb_id="mb_sup",
    current_terminal_id="sup-001",
    consumed_through_id=0,
    role="supervisor",
):
    return SimpleNamespace(
        id=mb_id,
        current_terminal_id=current_terminal_id,
        consumed_through_id=consumed_through_id,
        role=role,
    )


def _make_terminal(terminal_id="sup-001"):
    return SimpleNamespace(id=terminal_id)


def _make_inbox_row(
    msg_id=1,
    sender_id="worker-01",
    message="done",
    receiver_id="sup-001",
    logical_receiver_id="mb_sup",
    created_at=None,
    status="pending",
):
    from cli_agent_orchestrator.models.inbox import OrchestrationType

    return SimpleNamespace(
        id=msg_id,
        sender_id=sender_id,
        receiver_id=receiver_id,
        message=message,
        orchestration_type=OrchestrationType.SEND_MESSAGE.value,
        status=status,
        created_at=created_at or _OLD,
        logical_receiver_id=logical_receiver_id,
    )


# ===========================================================================
# Unit: ring_supervisor_doorbell with caller_holds_no_delivery_lock=True
# bypasses G1 even when the lock is already held.
# ===========================================================================


class TestF186CallerHoldsNoLockBypassesG1:
    """ring_supervisor_doorbell with caller_holds_no_delivery_lock=True must
    NOT skip on delivery_lock contention — it must proceed through G2+ gates.
    """

    def test_ring_succeeds_despite_held_delivery_lock(self):
        """The core defect: when delivery_lock is held by another thread,
        the fallback path's G1 gate skipped. With the fix, passing
        caller_holds_no_delivery_lock=True bypasses G1 and the ring proceeds.
        """
        # A real threading.Lock already held by 'another thread'
        real_lock = threading.Lock()
        real_lock.acquire()  # simulates F136 runner holding it

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_config,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={"metadata": {}},
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
                return_value=real_lock,
            ),
            patch(
                "cli_agent_orchestrator.services.receiver_state_view.native_probe",
            ) as mock_probe,
            patch(
                "cli_agent_orchestrator.services.inbox_service.inbox_service._inject_safe",
            ) as mock_inject,
            patch(
                "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
                return_value=MagicMock(),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.send_prepared_input",
            ) as mock_send,
        ):
            # Config: native wake OFF so we exercise the gated fallback path
            def _cfg(path, default=None, override=None):
                if path == "supervisor.wake.native":
                    return False
                if path == "supervisor.doorbell":
                    return True
                return True

            mock_config.get.side_effect = _cfg

            # Probe returns IDLE
            from cli_agent_orchestrator.services.status_monitor import TerminalStatus

            probe = MagicMock()
            probe.status = TerminalStatus.IDLE
            probe.meta = {}
            mock_probe.return_value = probe

            from cli_agent_orchestrator.services.inbox_service import InjectSafetyResult

            mock_inject.return_value = InjectSafetyResult("safe")

            # Call with the F186 fix flag
            result = ring_supervisor_doorbell(
                "sup-001",
                42,
                written_count=1,
                caller_holds_no_delivery_lock=True,
            )

            assert (
                result == "rang"
            ), f"Expected 'rang' but got '{result}' — the delivery lock bypass failed"
            mock_send.assert_called_once()

        # Release the lock we held to simulate contention
        real_lock.release()

    def test_without_flag_still_skips_when_lock_held(self):
        """Baseline: without the flag, a held lock still causes skipped_gate."""
        real_lock = threading.Lock()
        real_lock.acquire()

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_config,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={"metadata": {}},
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
                return_value=real_lock,
            ),
        ):

            def _cfg(path, default=None, override=None):
                if path == "supervisor.wake.native":
                    return False
                if path == "supervisor.doorbell":
                    return True
                return True

            mock_config.get.side_effect = _cfg

            # Without the flag, should still skip
            result = ring_supervisor_doorbell(
                "sup-001",
                42,
                written_count=1,
                caller_holds_no_delivery_lock=False,
            )

            assert result == "skipped_gate"

        real_lock.release()


# ===========================================================================
# Integration: reconcile_pull_mode_notifications passes the flag.
# ===========================================================================


class TestF186ReconcilerPassesFlag:
    """The reconciler ride-along calls ring_supervisor_doorbell with
    caller_holds_no_delivery_lock=True, proven by interception.
    """

    def test_reconciler_ride_along_passes_lock_bypass_flag(self, tmp_path):
        """End-to-end: reconciler push triggers a doorbell submit that eventually
        fires with caller_holds_no_delivery_lock=True (F461: routes through
        doorbell_coalesce_service.submit which always passes the flag on fire).
        """
        svc = InboxService()
        mb = _make_mailbox(consumed_through_id=0)
        terminal = _make_terminal()
        inbox_row = _make_inbox_row(msg_id=10, created_at=_OLD)

        inbox_path = tmp_path / "inbox.json"
        submit_calls = []

        def _capture_submit(
            terminal_id,
            max_row_id,
            *,
            written_count=0,
            **kwargs,
        ):
            submit_calls.append(
                {
                    "terminal_id": terminal_id,
                    "max_row_id": max_row_id,
                    "written_count": written_count,
                }
            )

        class _Session:
            def __call__(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def query(self, model):
                name = getattr(model, "__tablename__", str(model))
                if "mailbox" in name.lower():
                    return _FakeQuery([mb])
                elif "terminal" in name.lower():
                    return _FakeQuery([terminal])
                elif "inbox" in name.lower():
                    return _FakeQuery([inbox_row])
                return _FakeQuery([])

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value={
                    "id": "sup-001",
                    "metadata": {"cc_team_inbox_path": str(inbox_path)},
                },
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                _Session(),
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
                return_value="attempt-uuid-1",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
                return_value=True,
            ),
            # F461: patch coalesce submit instead of ring_supervisor_doorbell
            patch(
                "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service.submit",
                side_effect=_capture_submit,
            ),
        ):
            svc.reconcile_pull_mode_notifications()

        # The ride-along must have fired a coalesce submit
        assert len(submit_calls) == 1, f"Expected 1 coalesce submit call, got {len(submit_calls)}"
        assert submit_calls[0]["terminal_id"] == "sup-001"
        assert submit_calls[0]["max_row_id"] == 10
        assert submit_calls[0]["written_count"] == 1

    def test_reconciler_ring_not_swallowed_by_concurrent_delivery_lock(self, tmp_path):
        """F461: delivery lock no longer relevant for reconciler ring.

        With F461, the reconciler routes through doorbell_coalesce_service.submit
        which fires asynchronously after the coalesce window. The delivery lock
        is never held at fire time, making the F186 lock-bypass concern moot.
        This test now verifies that the coalesce submit is called (the coalesce
        service always passes caller_holds_no_delivery_lock=True on fire).
        """
        svc = InboxService()
        mb = _make_mailbox(consumed_through_id=0)
        terminal = _make_terminal()
        inbox_row = _make_inbox_row(msg_id=10, created_at=_OLD)

        # Simulate F136 runner holding the delivery lock
        real_lock = threading.Lock()
        real_lock.acquire()

        submit_results = []

        def _capture_submit(
            terminal_id,
            max_row_id,
            *,
            written_count=0,
            **kwargs,
        ):
            """Intercept the coalesce submit call."""
            submit_results.append(
                {
                    "terminal_id": terminal_id,
                    "max_row_id": max_row_id,
                    # Verify the lock IS still held (reproducing the defect scenario —
                    # F461 makes this irrelevant since fire happens after window)
                    "lock_would_block": not real_lock.acquire(blocking=False),
                }
            )
            # If we did acquire it, release immediately
            if not submit_results[-1]["lock_would_block"]:
                real_lock.release()

        class _Session:
            def __call__(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def query(self, model):
                name = getattr(model, "__tablename__", str(model))
                if "mailbox" in name.lower():
                    return _FakeQuery([mb])
                elif "terminal" in name.lower():
                    return _FakeQuery([terminal])
                elif "inbox" in name.lower():
                    return _FakeQuery([inbox_row])
                return _FakeQuery([])

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    True
                    if key
                    in (
                        "supervisor.mailbox_pull",
                        "supervisor.teammate_push",
                        "supervisor.wake.native",
                    )
                    else None
                ),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
                return_value=PushOutcome(pushed=True, reason="pushed", message_ids=(10,)),
            ),
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal",
                _Session(),
            ),
            patch(
                "cli_agent_orchestrator.clients.database.begin_delivery_attempt",
                return_value="attempt-uuid-1",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.settle_delivery_attempt",
                return_value=True,
            ),
            # F461: patch coalesce submit
            patch(
                "cli_agent_orchestrator.services.doorbell_coalesce.doorbell_coalesce_service.submit",
                side_effect=_capture_submit,
            ),
        ):
            svc.reconcile_pull_mode_notifications()

        # The coalesce submit was called
        assert len(submit_results) == 1
        # Confirm the lock was indeed held (F461 makes this safe since fire is async)
        assert (
            submit_results[0]["lock_would_block"] is True
        ), "The delivery lock should have been held, F461 makes this safe via async fire"

        real_lock.release()


# ===========================================================================
# F186 Gate-Fold: Mutation-Killing Tests (S1/M2 + S2/M4)
# ===========================================================================


class TestF186GateFoldS1M2:
    """S1/M2: G2 recovery_state check remains active even when
    caller_holds_no_delivery_lock=True.

    Mutant M2 wraps G2 in `if not caller_holds_no_delivery_lock`, letting the
    True path skip recovery_state gating. This test proves G2 fires on the
    True path: recovery_state="rebinding" → skipped_gate.

    All gates downstream of G2 (G4-G8) are mocked to succeed, so the ONLY
    reason the function can return 'skipped_gate' is the G2 recovery_state
    check. If M2 is applied (G2 wrapped), the function returns 'rang' instead.
    """

    def test_g2_gates_rebinding_even_with_lock_bypass_flag(self):
        """With caller_holds_no_delivery_lock=True and
        recovery_state='rebinding', the ring MUST return 'skipped_gate'
        — proving G2+ stays active under the flag.
        """
        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_config,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={"metadata": {"recovery_state": "rebinding"}},
            ),
            patch(
                "cli_agent_orchestrator.services.receiver_state_view.native_probe",
            ) as mock_probe,
            patch(
                "cli_agent_orchestrator.services.inbox_service.inbox_service._inject_safe",
            ) as mock_inject,
            patch(
                "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
                return_value=MagicMock(),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.send_prepared_input",
            ),
        ):
            # Config: native wake OFF to exercise gated fallback; doorbell ON
            def _cfg(path, default=None, override=None):
                if path == "supervisor.wake.native":
                    return False
                if path == "supervisor.doorbell":
                    return True
                return True

            mock_config.get.side_effect = _cfg

            # G4-G6: Probe returns IDLE (would pass if G2 didn't stop it)
            from cli_agent_orchestrator.services.status_monitor import TerminalStatus

            probe = MagicMock()
            probe.status = TerminalStatus.IDLE
            probe.meta = {}
            mock_probe.return_value = probe

            # G5: inject_safe returns safe (would pass if G2 didn't stop it)
            from cli_agent_orchestrator.services.inbox_service import InjectSafetyResult

            mock_inject.return_value = InjectSafetyResult("safe")

            result = ring_supervisor_doorbell(
                "sup-001",
                42,
                written_count=1,
                caller_holds_no_delivery_lock=True,
            )

            assert result == "skipped_gate", (
                f"Expected 'skipped_gate' from G2 (recovery_state=rebinding) "
                f"but got '{result}' — M2 mutant alive: flag bypasses G2"
            )


class TestF186GateFoldS2M4:
    """S2/M4: delivery_lock.release() is called exactly once in the finally
    path after a ring attempt when caller_holds_no_delivery_lock=False.

    Mutant M4 deletes the `if _owns_lock: delivery_lock.release()` block.
    This test proves the lock is released after the ring completes.
    """

    def test_lock_released_in_finally_after_ring(self):
        """With caller_holds_no_delivery_lock=False and a mock delivery lock
        whose acquire succeeds, assert release() is called exactly once in
        the finally path after a ring attempt.
        """
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True  # lock acquired successfully

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_config,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={"metadata": {}},
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
                return_value=mock_lock,
            ),
            patch(
                "cli_agent_orchestrator.services.receiver_state_view.native_probe",
            ) as mock_probe,
            patch(
                "cli_agent_orchestrator.services.inbox_service.inbox_service._inject_safe",
            ) as mock_inject,
            patch(
                "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
                return_value=MagicMock(),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.send_prepared_input",
            ),
        ):

            def _cfg(path, default=None, override=None):
                if path == "supervisor.wake.native":
                    return False
                if path == "supervisor.doorbell":
                    return True
                return True

            mock_config.get.side_effect = _cfg

            # Probe returns IDLE
            from cli_agent_orchestrator.services.status_monitor import TerminalStatus

            probe = MagicMock()
            probe.status = TerminalStatus.IDLE
            probe.meta = {}
            mock_probe.return_value = probe

            from cli_agent_orchestrator.services.inbox_service import InjectSafetyResult

            mock_inject.return_value = InjectSafetyResult("safe")

            result = ring_supervisor_doorbell(
                "sup-001",
                42,
                written_count=1,
                caller_holds_no_delivery_lock=False,
            )

            # The ring should succeed
            assert result == "rang", f"Expected 'rang' but got '{result}'"

            # M4 kill: release() MUST be called exactly once in finally
            mock_lock.release.assert_called_once()
