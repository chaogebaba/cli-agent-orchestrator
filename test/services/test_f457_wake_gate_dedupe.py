"""F457 — Unified socket-wake gate + acked-row dedupe.

Tests:
(a) supervisor.wake.native=false suppresses the inbox_service teammate-push path
    (supervisor.teammate_push stays true — proves the new gate is independent).
(b) Acked row → no wake from either the inbox teammate-push or doorbell native ring.
(c) supervisor.wake.native=true + pending row → wake still fires (no regression).
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
    """Helper: run deliver_pending through the mailbox-pull branch, return mock_push."""
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
        patch("cli_agent_orchestrator.services.inbox_service._delivery_wake_seq", {}),
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
        mock_pending.return_value = [_msg(700)]
        mock_pull.return_value = True
        mock_should.return_value = True  # teammate_push always ON

        # ConfigService.get responses
        def _config_get(key, default=None):
            if key == "supervisor.wake.native":
                return wake_native
            if key == "supervisor.teammate_push":
                return True
            return default

        mock_config_get.side_effect = _config_get

        # Dedupe re-check
        if messages_still_pending:
            mock_recheck.return_value = [_msg(700)]
        else:
            mock_recheck.return_value = []

        service.deliver_pending(_TERMINAL_ID)
        return mock_push


# ===========================================================================
# (a) wake.native=false suppresses inbox_service teammate-push
# ===========================================================================


class TestAC1WakeNativeFalseSuppressesPush:
    """supervisor.wake.native=false must suppress the teammate push in the
    mailbox-pull branch of deliver_pending, even when supervisor.teammate_push=true.
    """

    def test_wake_native_false_suppresses_teammate_push(self):
        """With wake.native=false and teammate_push=true, attempt_teammate_push must NOT fire."""
        mock_push = _deliver_with_patches(wake_native=False, messages_still_pending=True)
        mock_push.assert_not_called()


# ===========================================================================
# (b) Acked row → no wake from either path
# ===========================================================================


class TestAC2AckedRowNoWake:
    """Messages already acked/delivered must not trigger a wake from either path."""

    def test_acked_row_no_teammate_push(self):
        """When messages are no longer PENDING at send time, teammate push is skipped."""
        mock_push = _deliver_with_patches(wake_native=True, messages_still_pending=False)
        mock_push.assert_not_called()

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
# (c) wake.native=true + pending row → wake fires (no regression)
# ===========================================================================


class TestAC3WakeNativeTruePendingStillFires:
    """Normal path: wake.native=true + pending row → teammate push fires."""

    def test_wake_native_true_pending_row_push_fires(self):
        """With wake.native=true and messages still PENDING, push must fire."""
        mock_push = _deliver_with_patches(wake_native=True, messages_still_pending=True)
        mock_push.assert_called_once()
