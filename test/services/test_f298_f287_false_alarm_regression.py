"""Regression tests for #152 (F298) and #141 (F287).

Both issues describe the same false-alarm class: the watchdog fires
'worker <id> deleted before callback — task result may be lost' on
delete_terminal even when the callback was already delivered AND consumed
(acked via the supervisor's consumed_through cursor).

Root cause: the in-memory `episode.callback_seen` flag was not set by the
inbox-drain/auto-surface path — only by the explicit `record_callback_if_to_caller`
call in the push-delivery codepath. When a callback was delivered via the
pull/mailbox-drain path, the flag stayed False.

Fixed by F310 (commit 34c49358): `emit_pre_delete_notice` now consults the
durable inbox before firing. If `get_callback_status_since` finds a message
from the worker to (or routed through) the caller since the episode's dispatch
time, the notice is suppressed regardless of in-memory flag state.

These tests prove:
AC1: Callback delivered via auto-surface (callback_seen stays False in-memory)
     → delete_terminal → NO false alarm (DB ground-truth suppresses).
AC2: Callback delivered to mailbox (logical_receiver_id path) with callback_seen
     False in-memory → delete_terminal → NO false alarm.
AC3: Multiple callbacks from same worker, all consumed → delete → no alarm.
AC4: Genuine loss still fires (no callback in DB either).
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services.stalled_callback_watchdog import (
    StalledCallbackWatchdog,
)


class TestF298AC1AutoSurfaceFalseAlarm:
    """#152 exact repro: callback auto-surfaced+acked, episode.callback_seen=False."""

    def test_auto_surfaced_callback_suppresses_via_db_check(self):
        """The inbox-drain hook delivered the callback but never called
        record_callback_if_to_caller. The DB shows DELIVERED → no alarm."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("kiro_reviewer-9f265c0d", "sup-10cdda20", "kiro_reviewer")

        # The episode's callback_seen is False (the bug scenario from #152)
        episode = svc._episodes["kiro_reviewer-9f265c0d"]
        assert episode.callback_seen is False
        assert episode.caller_id == "sup-10cdda20"

        # DB ground-truth: the callback exists as DELIVERED
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
            return_value=MessageStatus.DELIVERED,
        ) as mock_db:
            notice = svc.emit_pre_delete_notice("kiro_reviewer-9f265c0d")

        assert notice is None, (
            "#152 regression: callback was delivered+consumed via inbox-drain; "
            "watchdog must NOT fire false 'deleted before callback' alarm"
        )
        mock_db.assert_called_once_with(
            "kiro_reviewer-9f265c0d",
            "sup-10cdda20",
            episode.episode_started_wall_at,
        )

    def test_consumed_digested_callback_suppresses(self):
        """Callback consumed through barrier (DIGESTED status) → no alarm."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("kiro_reviewer-d0c0cb82", "sup-10cdda20", "kiro_reviewer")

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
            return_value=MessageStatus.DIGESTED,
        ):
            notice = svc.emit_pre_delete_notice("kiro_reviewer-d0c0cb82")

        assert notice is None, (
            "#152 regression: callback was DIGESTED (acked through barrier); "
            "no false alarm"
        )


class TestF287AC2MailboxPath:
    """#141 exact repro: callback delivered to supervisor's mailbox,
    callback_seen=False because record_callback_if_to_caller was never called."""

    def test_callback_to_mailbox_suppresses_via_logical_receiver(self):
        """The worker sent its callback addressed to the supervisor's mailbox ID.
        get_callback_status_since resolves the mailbox and finds the message."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("de3ec4e4", "sup-aaaabbbb", "kiro_design_reviewer")

        # callback_seen stays False (the bug path)
        assert svc._episodes["de3ec4e4"].callback_seen is False

        # DB finds the callback via logical_receiver_id lookup
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
            return_value=MessageStatus.DELIVERED,
        ):
            notice = svc.emit_pre_delete_notice("de3ec4e4")

        assert notice is None, (
            "#141 regression: callback delivered to mailbox, consumed 5min prior; "
            "watchdog must NOT fire"
        )

    def test_callback_acked_2min_prior_no_alarm(self):
        """#141 occurrence 2: callback acked ~2min before delete_terminal."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("7b6bb177", "sup-aaaabbbb", "kiro_reviewer")

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
            return_value=MessageStatus.DELIVERED,
        ):
            notice = svc.emit_pre_delete_notice("7b6bb177")

        assert notice is None


class TestF298AC3MultipleCallbacks:
    """#152 escalation note: 100% false-positive rate on post-callback reaps.
    Multiple callbacks from same worker, all consumed."""

    def test_multiple_callbacks_all_consumed_no_alarm(self):
        """Worker sent two callbacks (e.g. FINAL + FROZEN), both consumed.
        Episode re-armed by follow-up send_message between them.
        DB finds a callback → no alarm."""
        svc = StalledCallbackWatchdog(grace_seconds=120)

        # First assign
        svc.record_inbound_task("kiro_dev-206680e5", "sup-10cdda20", "kiro_dev")
        # First callback sets callback_seen
        svc._episodes["kiro_dev-206680e5"].callback_seen = True

        # Follow-up re-arms the episode (supervisor sends more work)
        svc.record_inbound_task("kiro_dev-206680e5", "sup-10cdda20", "kiro_dev")
        assert svc._episodes["kiro_dev-206680e5"].generation == 2
        assert svc._episodes["kiro_dev-206680e5"].callback_seen is False

        # Worker sends second callback but record_callback_if_to_caller missed it
        # (auto-surfaced via pull path). DB has it.
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
            return_value=MessageStatus.DELIVERED,
        ):
            notice = svc.emit_pre_delete_notice("kiro_dev-206680e5")

        assert notice is None, (
            "#152 regression: both callbacks (FINAL, FROZEN) delivered+consumed; "
            "no false alarm on delete"
        )


class TestF298AC4GenuineLossStillFires:
    """Sanity: the fix doesn't suppress genuine loss warnings."""

    def test_no_callback_in_db_fires_alarm(self):
        """Worker genuinely never sent a callback → alarm fires correctly."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("kiro_dev-deadbeef", "sup-10cdda20", "kiro_dev")

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
            return_value=None,  # No callback found in DB
        ), patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "insert_barrier_escalation_message",
            return_value=None,
        ), patch(
            "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
        ):
            notice = svc.emit_pre_delete_notice("kiro_dev-deadbeef")

        assert notice is not None
        assert notice.kind == "deletion"
        assert "task result may be lost" in notice.message

    def test_db_error_conservative_fires(self):
        """DB unavailable → conservatively fire (no false negative)."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("kiro_dev-00000000", "sup-10cdda20", "kiro_dev")

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
            side_effect=RuntimeError("DB connection lost"),
        ), patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "insert_barrier_escalation_message",
            return_value=None,
        ), patch(
            "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
        ):
            notice = svc.emit_pre_delete_notice("kiro_dev-00000000")

        assert notice is not None
        assert "task result may be lost" in notice.message
