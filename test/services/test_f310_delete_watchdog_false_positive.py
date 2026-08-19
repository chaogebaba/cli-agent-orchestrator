"""F310: delete_terminal watchdog false-positive fix.

AC1: On delete_terminal, the loss warning fires ONLY if NO callback message from
     that worker was delivered to (or acked by) the caller since the most recent
     task dispatch to that worker.
AC2: Repro test: assign -> callback delivered+acked -> follow-up send_message ->
     second callback delivered+acked -> delete_terminal -> assert NO watchdog loss push.
AC3: Genuine-loss path still fires: assign -> no callback -> delete_terminal ->
     warning present.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.stalled_callback_watchdog import (
    StalledCallbackWatchdog,
    WatchdogNotice,
)


def _persist_patches():
    """Context patches to prevent real DB writes in _persist_notice."""
    return (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "insert_barrier_escalation_message",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
        ),
    )


class TestF310AC1Unit:
    """AC1: loss warning fires ONLY if no callback was delivered since last dispatch."""

    def test_db_shows_delivered_callback_suppresses_notice(self):
        """When get_callback_status_since returns DELIVERED, no notice fires."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")
        # Episode has callback_seen=False (the bug scenario)
        assert svc._episodes["w1"].callback_seen is False

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
            return_value=MessageStatus.DELIVERED,
        ):
            notice = svc.emit_pre_delete_notice("w1")

        assert notice is None

    def test_db_shows_digested_callback_suppresses_notice(self):
        """When get_callback_status_since returns DIGESTED (acked), no notice fires."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
            return_value=MessageStatus.DIGESTED,
        ):
            notice = svc.emit_pre_delete_notice("w1")

        assert notice is None

    def test_db_shows_no_callback_fires_notice(self):
        """When get_callback_status_since returns None, the loss notice fires."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")

        p1, p2 = _persist_patches()
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "get_callback_status_since",
                return_value=None,
            ),
            p1,
            p2,
        ):
            notice = svc.emit_pre_delete_notice("w1")

        assert notice is not None
        assert notice.kind == "deletion"
        assert "w1" in notice.message

    def test_db_failure_falls_through_to_fire(self):
        """When get_callback_status_since raises, conservatively fire."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")

        p1, p2 = _persist_patches()
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "get_callback_status_since",
                side_effect=RuntimeError("db gone"),
            ),
            p1,
            p2,
        ):
            notice = svc.emit_pre_delete_notice("w1")

        assert notice is not None
        assert notice.kind == "deletion"

    def test_callback_seen_true_still_short_circuits(self):
        """Existing fast path: callback_seen=True skips DB entirely."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")
        svc._episodes["w1"].callback_seen = True

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
        ) as mock_db:
            notice = svc.emit_pre_delete_notice("w1")

        assert notice is None
        mock_db.assert_not_called()


class TestF310AC2Repro:
    """AC2: Full repro — assign -> callback -> follow-up send_message ->
    second callback -> delete_terminal -> NO loss push."""

    def test_full_repro_no_false_positive(self):
        """The exact scenario from the bug report.

        1. assign -> worker (episode gen 1, callback_seen=False)
        2. worker callback delivered+acked (callback_seen=True on gen 1)
        3. supervisor sends follow-up send_message -> record_inbound_task
           sees callback_seen=True -> creates NEW episode gen 2 (callback_seen=False)
        4. worker sends second callback -> callback_seen=True on gen 2
        5. delete_terminal -> NO notice (callback_seen=True fast path)
        """
        svc = StalledCallbackWatchdog(grace_seconds=120)

        # Step 1: assign
        svc.record_inbound_task("w1", "caller1", "developer")
        assert svc._episodes["w1"].generation == 1
        assert svc._episodes["w1"].callback_seen is False

        # Step 2: worker callback delivered+acked
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_terminal_metadata",
            return_value={"caller_id": "caller1", "caller_mailbox_id": None},
        ):
            svc.record_callback_if_to_caller("w1", "caller1")
        assert svc._episodes["w1"].callback_seen is True

        # Step 3: supervisor follow-up send_message re-arms episode
        svc.record_inbound_task("w1", "caller1", "developer")
        assert svc._episodes["w1"].generation == 2
        assert svc._episodes["w1"].callback_seen is False

        # Step 4: worker sends second callback
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_terminal_metadata",
            return_value={"caller_id": "caller1", "caller_mailbox_id": None},
        ):
            svc.record_callback_if_to_caller("w1", "caller1")
        assert svc._episodes["w1"].callback_seen is True

        # Step 5: delete_terminal — should NOT fire (callback_seen=True)
        notice = svc.emit_pre_delete_notice("w1")
        assert notice is None

    def test_repro_with_late_follow_up_after_both_callbacks(self):
        """The EXACT timing bug from issue #164.

        1. assign -> worker
        2. worker callback delivered+acked
        3. worker sends SECOND callback (e.g. result + status)
        4. supervisor sends follow-up send_message (arrives AFTER callbacks)
           -> re-arms episode -> gen 2, callback_seen=False
        5. delete_terminal -> episode.callback_seen=False BUT DB has the acked callback
           -> F310 fix: DB check suppresses the false warning.
        """
        svc = StalledCallbackWatchdog(grace_seconds=120)

        # Step 1: assign
        svc.record_inbound_task("w1", "caller1", "developer")

        # Step 2+3: two worker callbacks
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_terminal_metadata",
            return_value={"caller_id": "caller1", "caller_mailbox_id": None},
        ):
            svc.record_callback_if_to_caller("w1", "caller1")
        assert svc._episodes["w1"].callback_seen is True

        # Step 4: late follow-up re-arms the episode
        svc.record_inbound_task("w1", "caller1", "developer")
        assert svc._episodes["w1"].generation == 2
        assert svc._episodes["w1"].callback_seen is False

        # Step 5: delete_terminal — F310 DB check catches this
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_callback_status_since",
            return_value=MessageStatus.DELIVERED,
        ):
            notice = svc.emit_pre_delete_notice("w1")

        assert notice is None, (
            "F310: DB shows delivered callback since episode start — "
            "loss warning must NOT fire"
        )


class TestF310AC3GenuineLoss:
    """AC3: Genuine-loss path still fires: assign -> no callback -> delete."""

    def test_genuine_loss_no_callback_fires(self):
        """assign -> no callback at all -> delete_terminal -> warning present."""
        svc = StalledCallbackWatchdog(grace_seconds=120)
        svc.record_inbound_task("w1", "caller1", "developer")

        p1, p2 = _persist_patches()
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "get_callback_status_since",
                return_value=None,
            ),
            p1,
            p2 as mock_create,
        ):
            notice = svc.emit_pre_delete_notice("w1")

        assert notice is not None
        assert notice.kind == "deletion"
        assert "task result may be lost" in notice.message
        assert "w1" in notice.message
        assert "developer" in notice.message
        mock_create.assert_called_once()

    def test_genuine_loss_after_rearmed_episode(self):
        """assign -> callback -> follow-up -> NO second callback -> delete -> fires.

        The follow-up created a new episode (gen 2) expecting a new callback.
        Worker never delivered on gen 2. DB confirms no message since gen 2 start.
        Warning fires correctly.
        """
        svc = StalledCallbackWatchdog(grace_seconds=120)

        # assign
        svc.record_inbound_task("w1", "caller1", "developer")
        # first callback (gen 1 satisfied)
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "get_terminal_metadata",
            return_value={"caller_id": "caller1", "caller_mailbox_id": None},
        ):
            svc.record_callback_if_to_caller("w1", "caller1")

        # follow-up re-arms (gen 2)
        svc.record_inbound_task("w1", "caller1", "developer")
        assert svc._episodes["w1"].generation == 2
        assert svc._episodes["w1"].callback_seen is False

        # delete without second callback — DB also returns None
        p1, p2 = _persist_patches()
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "get_callback_status_since",
                return_value=None,
            ),
            p1,
            p2,
        ):
            notice = svc.emit_pre_delete_notice("w1")

        assert notice is not None
        assert "task result may be lost" in notice.message
