"""F457 r2 — Tests for BLOCKER/SHOULD fixes from the gate report.

Tests:
(a) B1: reconciler path (reconcile_pull_mode_notifications) suppressed when wake.native=false.
(b) B2: attempt_rung1 honors wake.native=false AND skips acked rows.
(c) S1: DB error in get_pending_messages_by_ids → push still attempted (fail-open).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.inbox_service import InboxService


_NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
_TERMINAL_ID = "r2t00001"
_SENDER_ID = "r2s00001"
_MAILBOX_ID = "r2mb0001"


def _msg(msg_id: int = 800, status: MessageStatus = MessageStatus.PENDING) -> InboxMessage:
    return InboxMessage(
        id=msg_id,
        sender_id=_SENDER_ID,
        receiver_id=_TERMINAL_ID,
        message="callback result",
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=status,
        created_at=_NOW,
    )


# ===========================================================================
# (a) B1: reconciler path suppressed when wake.native=false
# ===========================================================================


class TestB1ReconcilerWakeNativeGate:
    """reconcile_pull_mode_notifications must NOT call attempt_teammate_push_reported
    when supervisor.wake.native=false."""

    def test_reconciler_suppressed_when_wake_native_false(self):
        """With wake.native=false, reconciler must not push even though
        supervisor.mailbox_pull=true and pending rows exist."""
        service = InboxService.__new__(InboxService)

        # Build a realistic pending row mock
        mock_inbox_row = MagicMock()
        mock_inbox_row.id = 800
        mock_inbox_row.sender_id = _SENDER_ID
        mock_inbox_row.receiver_id = _TERMINAL_ID
        mock_inbox_row.message = "test msg"
        mock_inbox_row.orchestration_type = "send_message"
        mock_inbox_row.status = "pending"
        mock_inbox_row.created_at = _NOW - timedelta(seconds=120)
        mock_inbox_row.logical_receiver_id = _MAILBOX_ID

        mock_mb = MagicMock()
        mock_mb.id = _MAILBOX_ID
        mock_mb.current_terminal_id = _TERMINAL_ID
        mock_mb.consumed_through_id = 0
        mock_mb.role = "supervisor"

        mock_terminal = MagicMock()
        mock_terminal.id = _TERMINAL_ID

        # Track which query model is being used to route responses
        call_count = {"n": 0}

        def _make_db_context():
            mock_db = MagicMock()

            def _query_side_effect(model):
                call_count["n"] += 1
                q = MagicMock()
                q.filter_by.return_value = q
                q.filter.return_value = q
                q.order_by.return_value = q
                q.limit.return_value = q
                q.count.return_value = 1
                # Route based on call order:
                # 1st session: query mailboxes
                # 2nd session: query terminal
                # 3rd session (if reached): query pending rows
                n = call_count["n"]
                if n == 1:
                    q.all.return_value = [mock_mb]
                elif n == 2:
                    q.one_or_none.return_value = mock_terminal
                elif n == 3:
                    q.all.return_value = [mock_inbox_row]
                else:
                    q.all.return_value = []
                return q

            mock_db.query.side_effect = _query_side_effect
            return mock_db

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get"
            ) as mock_config_get,
            patch(
                "cli_agent_orchestrator.services.inbox_service._utcnow",
                return_value=_NOW,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal"
            ) as mock_sl,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported"
            ) as mock_push_reported,
        ):
            # ConfigService.get routing
            def _config_get(key, default=None):
                if key == "supervisor.mailbox_pull":
                    return True
                if key == "supervisor.wake.native":
                    return False  # ← the kill switch
                return default
            mock_config_get.side_effect = _config_get

            mock_sl.return_value.__enter__ = MagicMock(side_effect=_make_db_context)
            mock_sl.return_value.__exit__ = MagicMock(return_value=False)

            service.reconcile_pull_mode_notifications()

            # The push must NOT have been called
            mock_push_reported.assert_not_called()


# ===========================================================================
# (b) B2: attempt_rung1 honors wake.native=false + acked row
# ===========================================================================


class TestB2AttemptRung1Gates:
    """attempt_rung1 must check wake.native and _is_row_still_pending
    before calling _attempt_native_ring."""

    def _make_target(self) -> MagicMock:
        target = MagicMock()
        target.terminal_id = _TERMINAL_ID
        target.liveness = "presumed_live"
        target.has_registry = True
        target.cc_inbox_path = "/tmp/test/inbox.json"
        return target

    def test_rung1_skipped_when_wake_native_false(self):
        """wake.native=false → rung1 returns skipped_disabled, no socket ring."""
        from cli_agent_orchestrator.services.delivery_service import attempt_rung1

        target = self._make_target()

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                return_value=False,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring"
            ) as mock_native,
            patch("os.path.isdir", return_value=True),
        ):
            result = attempt_rung1(target, inbox_row_id=800)

            assert result.decision == "skipped_disabled"
            assert result.reason == "wake_native_disabled"
            mock_native.assert_not_called()

    def test_rung1_skipped_for_acked_row(self):
        """Row no longer PENDING → rung1 returns skipped_acked, no socket ring."""
        from cli_agent_orchestrator.services.delivery_service import attempt_rung1

        target = self._make_target()

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending",
                return_value=False,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring"
            ) as mock_native,
            patch("os.path.isdir", return_value=True),
        ):
            result = attempt_rung1(target, inbox_row_id=800)

            assert result.decision == "skipped_acked"
            assert result.reason == "row_not_pending"
            mock_native.assert_not_called()

    def test_rung1_proceeds_when_enabled_and_pending(self):
        """wake.native=true + pending row → _attempt_native_ring is called (no regression)."""
        from cli_agent_orchestrator.services.delivery_service import attempt_rung1

        target = self._make_target()

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring",
                return_value="rang",
            ) as mock_native,
            patch("os.path.isdir", return_value=True),
        ):
            result = attempt_rung1(target, inbox_row_id=800)

            assert result.delivered is True
            assert result.decision == "proceed"
            mock_native.assert_called_once_with(_TERMINAL_ID, 800)


# ===========================================================================
# (c) S1: DB error in get_pending_messages_by_ids → push still attempted
# ===========================================================================


class TestS1FailOpenDbError:
    """A DB exception in get_pending_messages_by_ids must not suppress the push —
    fall back to the original messages list (fail-open)."""

    def test_db_error_falls_back_to_original_messages(self):
        """When get_pending_messages_by_ids raises, push is still attempted
        with the original messages list."""
        service = InboxService.__new__(InboxService)
        service._delivery_loop = MagicMock()
        service._delivery_tasks = set()
        service._tnf_lock = threading.Lock()
        service._terminal_not_found_streaks = {}

        with (
            patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_dl,
            patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages") as mock_pending,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal"
            ) as mock_pull,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push"
            ) as mock_should,
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push"
            ) as mock_push,
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get"
            ) as mock_config_get,
            patch(
                "cli_agent_orchestrator.services.inbox_service._delivery_wake_seq", {}
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt",
                MagicMock(),
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_pending_messages_by_ids"
            ) as mock_recheck,
        ):
            mock_lock = MagicMock()
            mock_lock.acquire.return_value = True
            mock_dl.return_value = mock_lock

            mock_meta.return_value = {"recovery_state": None}
            mock_pending.return_value = [_msg(800)]
            mock_pull.return_value = True
            mock_should.return_value = True

            # ConfigService.get responses
            def _config_get(key, default=None):
                if key == "supervisor.wake.native":
                    return True
                if key == "supervisor.teammate_push":
                    return True
                return default
            mock_config_get.side_effect = _config_get

            # DB error on recheck
            mock_recheck.side_effect = RuntimeError("DB connection lost")

            service.deliver_pending(_TERMINAL_ID)

            # Push must still fire with the original messages (fail-open)
            mock_push.assert_called_once()
            pushed_messages = mock_push.call_args[0][1]
            assert len(pushed_messages) == 1
            assert pushed_messages[0].id == 800
