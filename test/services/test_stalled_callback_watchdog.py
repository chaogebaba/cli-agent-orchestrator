from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog


def _mark_screen_sampled(svc, terminal_id="worker1"):
    svc._episodes[terminal_id].last_screen_fp = "sample"


def test_watchdog_pushes_exactly_one_due_notification():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)
    _mark_screen_sampled(svc)

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


def test_watchdog_polls_idle_status_when_no_post_task_status_event():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")

    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
        return_value=TerminalStatus.IDLE,
    ) as mock_get_status:
        svc.poll_unarmed_statuses(now=10.0)

    mock_get_status.assert_called_once_with("worker1")
    _mark_screen_sampled(svc)

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


def test_watchdog_polls_already_idle_episode_and_unarms_when_processing():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)
    _mark_screen_sampled(svc)

    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
        return_value=TerminalStatus.PROCESSING,
    ) as mock_get_status:
        svc.poll_unarmed_statuses(now=12.0)

    mock_get_status.assert_called_once_with("worker1")
    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"id": "worker1"},
    ):
        assert svc.collect_due_notifications(now=20.0) == []


def test_watchdog_screen_fingerprint_change_resets_idle_timer_then_static_fires():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    backend = MagicMock()
    backend.get_history.side_effect = ["frame 1", "frame 2", "frame 2"]
    metadata = {"id": "worker1", "tmux_session": "cao-test", "tmux_window": "win"}

    with (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=backend,
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=None,
        ),
    ):
        svc.refresh_screen_fingerprints(now=10.5)
        svc.refresh_screen_fingerprints(now=12.0)
        assert svc.collect_due_notifications(now=14.0) == []
        svc.refresh_screen_fingerprints(now=14.0)
        assert svc.collect_due_notifications(now=15.0) == [
            (
                "worker1",
                "caller1",
                "[watchdog] worker worker1 (developer) idle 3s without callback",
            )
        ]

    backend.get_history.assert_any_call(
        "cao-test",
        "win",
        tail_lines=45,
        strip_escapes=True,
    )


def test_watchdog_waits_for_initial_screen_fingerprint_before_firing():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"id": "worker1"},
    ):
        assert svc.collect_due_notifications(now=20.0) == []


def test_watchdog_excludes_rotating_codex_prompt_from_liveness_fingerprint():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    backend = MagicMock()
    backend.get_history.side_effect = [
        "stable output\n› Summarize recent commits\n? for shortcuts",
        "stable output\n› Explain this codebase\n? for shortcuts",
    ]
    provider = MagicMock()
    provider.liveness_exclude_patterns = [r"^\s*›", r"\?\s+for shortcuts"]
    metadata = {"id": "worker1", "tmux_session": "cao-test", "tmux_window": "win"}

    with (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=backend,
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=provider,
        ),
    ):
        svc.refresh_screen_fingerprints(now=10.5)
        svc.refresh_screen_fingerprints(now=12.0)
        assert svc.collect_due_notifications(now=13.0) == [
            (
                "worker1",
                "caller1",
                "[watchdog] worker worker1 (developer) idle 3s without callback",
            )
        ]


def test_watchdog_keeps_spinner_ticks_as_liveness_signal():
    svc = StalledCallbackWatchdog(grace_seconds=3)
    svc.record_inbound_task("worker1", "caller1", "developer")
    svc.record_status("worker1", TerminalStatus.IDLE, now=10.0)

    backend = MagicMock()
    backend.get_history.side_effect = [
        "• Working (1s • esc to interrupt)\n› Summarize recent commits\n? for shortcuts",
        "• Working (2s • esc to interrupt)\n› Explain this codebase\n? for shortcuts",
    ]
    provider = MagicMock()
    provider.liveness_exclude_patterns = [r"^\s*›", r"\?\s+for shortcuts"]
    metadata = {"id": "worker1", "tmux_session": "cao-test", "tmux_window": "win"}

    with (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=metadata,
        ),
        patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=backend,
        ),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
            return_value=provider,
        ),
    ):
        svc.refresh_screen_fingerprints(now=10.5)
        svc.refresh_screen_fingerprints(now=12.0)
        assert svc.collect_due_notifications(now=13.0) == []


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
    _mark_screen_sampled(svc)
    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        return_value={"id": "worker1"},
    ):
        assert svc.collect_due_notifications(now=13.0)

        svc.record_inbound_task("worker1", "caller1", "developer")
        svc.record_status("worker1", TerminalStatus.IDLE, now=20.0)
        _mark_screen_sampled(svc)

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
    _mark_screen_sampled(svc)

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
