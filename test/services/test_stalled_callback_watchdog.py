from unittest.mock import patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog


def test_watchdog_pushes_exactly_one_due_notification():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"id": "worker1"},
    ):
        assert svc.collect_due_notifications(now=12.0) == []
        assert svc.collect_due_notifications(now=13.0) == [
            (
                "worker1",
                "caller1",
                "[watchdog] worker worker1 (developer) idle 3s without callback",
            )
        ]
        assert svc.collect_due_notifications(now=14.0) == []


def test_watchdog_suppresses_notification_after_callback_to_recorded_caller():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"caller_id": "caller1"},
    ):
        svc.record_callback_if_to_caller("worker1", "caller1")

    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    assert svc.collect_due_notifications(now=20.0) == []


def test_watchdog_resets_on_new_task_after_firing():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)
    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"id": "worker1"},
    ):
        assert svc.collect_due_notifications(now=13.0)

        svc.record_inbound_task("worker1", "caller1", "developer")
        svc.record_status("worker1", TerminalStatus.IDLE, now=20.0)

        assert svc.collect_due_notifications(now=23.0) == [
            (
                "worker1",
                "caller1",
                "[watchdog] worker worker1 (developer) idle 3s without callback",
            )
        ]


def test_watchdog_prunes_deleted_terminal_without_push():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value=None,
    ):
        assert svc.collect_due_notifications(now=20.0) == []

    assert not svc.has_episode("worker1")


def test_notify_due_sends_only_to_caller():
    svc = StalledCallbackWatchdog(grace_seconds=3)

    with (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.create_inbox_message"
        ) as mock_create,
        patch(
            "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending"
        ) as mock_deliver,
        patch.object(svc, "collect_due_notifications") as mock_due,
    ):
        mock_due.return_value = [("worker1", "caller1", "notice")]
        svc.notify_due()

    mock_create.assert_called_once_with("watchdog:worker1", "caller1", "notice")
    mock_deliver.assert_called_once_with("caller1", registry=None)
