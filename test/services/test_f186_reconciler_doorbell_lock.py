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
    ds._last_doorbell_row_id.clear()
    ds._last_warn_time.clear()
    yield
    ds._last_doorbell_row_id.clear()
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
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ConfigService"
            ) as mock_config,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={"metadata": {}},
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service.set_terminal_last_doorbell_row_id",
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
                "sup-001", 42, written_count=1,
                caller_holds_no_delivery_lock=True,
            )

            assert result == "rang", (
                f"Expected 'rang' but got '{result}' — the delivery lock bypass failed"
            )
            mock_send.assert_called_once()

        # Release the lock we held to simulate contention
        real_lock.release()

    def test_without_flag_still_skips_when_lock_held(self):
        """Baseline: without the flag, a held lock still causes skipped_gate."""
        real_lock = threading.Lock()
        real_lock.acquire()

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ConfigService"
            ) as mock_config,
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
                "sup-001", 42, written_count=1,
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
        """End-to-end: reconciler push triggers a doorbell ring that succeeds
        even when the delivery lock is concurrently held (simulating the F136
        runner holding it, which is the production scenario).
        """
        svc = InboxService()
        mb = _make_mailbox(consumed_through_id=0)
        terminal = _make_terminal()
        inbox_row = _make_inbox_row(msg_id=10, created_at=_OLD)

        inbox_path = tmp_path / "inbox.json"
        doorbell_calls = []

        def _capture_doorbell(terminal_id, max_row_id, *, written_count=0, caller_holds_no_delivery_lock=False):
            doorbell_calls.append({
                "terminal_id": terminal_id,
                "max_row_id": max_row_id,
                "written_count": written_count,
                "caller_holds_no_delivery_lock": caller_holds_no_delivery_lock,
            })
            return "rang"

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
                side_effect=lambda key, *a, **kw: True if key in (
                    "supervisor.mailbox_pull", "supervisor.teammate_push"
                ) else None,
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
                "cli_agent_orchestrator.services.teammate_push_service.set_terminal_last_notified_inbox_id",
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
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ring_supervisor_doorbell",
                side_effect=_capture_doorbell,
            ),
        ):
            svc.reconcile_pull_mode_notifications()

        # The ride-along must have fired with the bypass flag
        assert len(doorbell_calls) == 1, (
            f"Expected 1 doorbell call, got {len(doorbell_calls)}"
        )
        assert doorbell_calls[0]["caller_holds_no_delivery_lock"] is True, (
            "Reconciler ride-along must pass caller_holds_no_delivery_lock=True"
        )
        assert doorbell_calls[0]["terminal_id"] == "sup-001"
        assert doorbell_calls[0]["max_row_id"] == 10
        assert doorbell_calls[0]["written_count"] == 1

    def test_reconciler_ring_not_swallowed_by_concurrent_delivery_lock(self, tmp_path):
        """Full defect reproduction: delivery lock is held by another thread
        (simulating F136 runner), reconciler pushes and rings — with the fix
        the ring succeeds because G1 is bypassed.

        This is the exact scenario from the AC12 sandbox arm that proved 4/4
        rings ending skipped_gate reason=delivery_lock. We verify the fix by
        confirming that the reconciler invokes ring_supervisor_doorbell with
        caller_holds_no_delivery_lock=True (unit tests above prove this flag
        bypasses G1).
        """
        svc = InboxService()
        mb = _make_mailbox(consumed_through_id=0)
        terminal = _make_terminal()
        inbox_row = _make_inbox_row(msg_id=10, created_at=_OLD)

        # Simulate F136 runner holding the delivery lock
        real_lock = threading.Lock()
        real_lock.acquire()

        doorbell_results = []

        def _capture_ring(terminal_id, max_row_id, *, written_count=0, caller_holds_no_delivery_lock=False):
            """Intercept the doorbell call and verify the flag."""
            doorbell_results.append({
                "terminal_id": terminal_id,
                "caller_holds_no_delivery_lock": caller_holds_no_delivery_lock,
                # Verify the lock IS held (reproducing the defect scenario)
                "lock_would_block": not real_lock.acquire(blocking=False),
            })
            # If we did acquire it, release immediately
            if not doorbell_results[-1]["lock_would_block"]:
                real_lock.release()
            return "rang"

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
                side_effect=lambda key, *a, **kw: True if key in (
                    "supervisor.mailbox_pull", "supervisor.teammate_push"
                ) else None,
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
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ring_supervisor_doorbell",
                side_effect=_capture_ring,
            ),
        ):
            svc.reconcile_pull_mode_notifications()

        # The doorbell was called with the bypass flag
        assert len(doorbell_results) == 1
        assert doorbell_results[0]["caller_holds_no_delivery_lock"] is True
        # Confirm the lock was indeed held (reproducing the AC12 scenario)
        assert doorbell_results[0]["lock_would_block"] is True, (
            "The delivery lock should have been held, reproducing the F186 scenario"
        )

        real_lock.release()
