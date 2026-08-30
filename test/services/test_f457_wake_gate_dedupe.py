"""F457 — Unified socket-wake gate + acked-row dedupe.

F476 r3 (#388): the deliver_pending pull-mode gate no longer calls
attempt_teammate_push directly (that bypass re-emitted already-acked ids via the
"Message N ready. Drain" teammate replay). It now routes through
request_delivery, so the wake goes through the single wake cursor
(claim_unnotified_wake/commit_wake) and the wake.native / acked-row gates are
enforced DOWNSTREAM in the cursor-gated runner and ring_supervisor_doorbell —
not in deliver_pending. These tests are updated accordingly:

(a) the gate signals request_delivery (routing), regardless of wake.native — the
    wake.native suppression is asserted at ring_supervisor_doorbell below.
(b) acked row → ring_supervisor_doorbell returns skipped_acked (native ring gate,
    unchanged by r3).
(c) pending row → the gate signals request_delivery.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.inbox_service import InboxService

_NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
_TERMINAL_ID = "f457t001"
_SENDER_ID = "f457s001"


def _msg(msg_id: int = 700, status: MessageStatus = MessageStatus.PENDING) -> InboxMessage:
    return InboxMessage(
        id=msg_id,
        sender_id=_SENDER_ID,
        receiver_id=_TERMINAL_ID,
        message="callback result",
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=status,
        created_at=_NOW,
    )


def _deliver_with_patches(
    *,
    wake_native: bool = True,
    messages_still_pending: bool = True,
):
    """Helper: run deliver_pending through the mailbox-pull branch.

    F476 r3: returns (mock_req_del, mock_push) so callers can assert the gate
    signals request_delivery (the cursor path) and never calls the direct
    attempt_teammate_push bypass.
    """
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
            "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push"
        ) as mock_push,
        patch(
            "cli_agent_orchestrator.services.inbox_service.request_delivery"
        ) as mock_req_del,
        patch("cli_agent_orchestrator.services.inbox_service._delivery_wake_seq", {}),
        patch(
            "cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt",
            MagicMock(),
        ),
    ):
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_dl.return_value = mock_lock

        mock_meta.return_value = {"recovery_state": None}
        mock_pending.return_value = [_msg(700)]
        mock_pull.return_value = True

        service.deliver_pending(_TERMINAL_ID)
        return mock_req_del, mock_push


# ===========================================================================
# (a) The pull-mode gate routes through the cursor (request_delivery), never
#     the direct attempt_teammate_push bypass — regardless of wake.native.
# ===========================================================================


class TestAC1PullGateRoutesThroughCursor:
    """r3: deliver_pending's supervisor pull-mode gate signals request_delivery
    and never calls attempt_teammate_push directly (bypass closed)."""

    def test_gate_signals_request_delivery_not_direct_push(self):
        mock_req_del, mock_push = _deliver_with_patches(
            wake_native=True, messages_still_pending=True
        )
        mock_req_del.assert_called_once_with(_TERMINAL_ID)
        mock_push.assert_not_called()

    def test_gate_routes_regardless_of_wake_native_flag(self):
        """wake.native gating moved downstream (ring_supervisor_doorbell); the
        gate itself always routes through the cursor."""
        mock_req_del, mock_push = _deliver_with_patches(
            wake_native=False, messages_still_pending=True
        )
        mock_req_del.assert_called_once_with(_TERMINAL_ID)
        mock_push.assert_not_called()


# ===========================================================================
# (b) Acked row → no native ring (ring_supervisor_doorbell gate; unchanged by r3)
# ===========================================================================


class TestAC2AckedRowNoWake:
    """A row no longer PENDING must not trigger the native doorbell ring."""

    def test_acked_row_no_doorbell_native_ring(self):
        """Doorbell skips native ring when max_written_row_id is no longer PENDING."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_config,
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata"
            ) as mock_meta,
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring"
            ) as mock_native,
            patch(
                "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending"
            ) as mock_row_pending,
        ):
            mock_config.get.side_effect = lambda k, default=None: {
                "supervisor.doorbell": True,
                "supervisor.wake.native": True,
            }.get(k, default)
            mock_meta.return_value = {"metadata": {"cc_team_inbox_path": "/tmp/inbox.json"}}

            # Row is no longer pending
            mock_row_pending.return_value = False

            result = ring_supervisor_doorbell(_TERMINAL_ID, 700, written_count=1)

            assert result == "skipped_acked"
            mock_native.assert_not_called()


# ===========================================================================
# (c) pending row → the gate signals request_delivery (no regression)
# ===========================================================================


class TestAC3PendingRowSignalsDelivery:
    """Normal path: pending row → the pull-mode gate signals request_delivery."""

    def test_pending_row_signals_request_delivery(self):
        mock_req_del, mock_push = _deliver_with_patches(
            wake_native=True, messages_still_pending=True
        )
        mock_req_del.assert_called_once_with(_TERMINAL_ID)
        mock_push.assert_not_called()
