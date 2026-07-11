from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import OrchestrationType
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


def test_msgtrace_confirmed_commit_performs_watchdog_operations_exactly_once():
    """FX7 operations are grouped at the confirmed-delivery commit boundary."""
    from cli_agent_orchestrator.services.inbox_service import InboxService

    service = InboxService()
    with patch(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog"
    ) as watchdog:
        watchdog.has_episode.return_value = True
        service._commit_watchdog_ops(
            "worker1", "caller1", OrchestrationType.SEND_MESSAGE,
            {"caller_id": "caller1", "agent_profile": "developer"},
        )
        watchdog.record_callback_if_to_caller.assert_called_once_with("caller1", "worker1")
        watchdog.record_inbound_task.assert_called_once_with("worker1", "caller1", "developer")


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


@contextmanager
def _waiting_inbox_fakes(
    *,
    pending=None,
    metadata=None,
    status=TerminalStatus.WAITING_USER_ANSWER,
    gate=None,
):
    pending = ["worker1"] if pending is None else pending
    metadata = (
        {
            "id": "worker1",
            "caller_id": "caller1",
            "agent_profile": "developer",
        }
        if metadata is None
        else metadata
    )
    with (
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "list_pending_receiver_ids",
            return_value=pending,
        ) as mock_pending,
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog." "get_terminal_metadata",
            return_value=metadata,
        ) as mock_metadata,
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            return_value=status,
        ) as mock_status,
        patch(
            "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
            return_value=gate,
        ) as mock_gate,
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog." "create_inbox_message"
        ) as mock_create,
        patch(
            "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending"
        ) as mock_deliver,
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog."
            "CAO_WAITING_INBOX_GRACE_SECONDS",
            10,
        ),
    ):
        yield {
            "pending": mock_pending,
            "metadata": mock_metadata,
            "status": mock_status,
            "gate": mock_gate,
            "create": mock_create,
            "deliver": mock_deliver,
        }


class TestWaitingInboxAlert:
    def test_a_waiting_pending_below_grace_does_not_push(self):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes() as fakes:
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=109.0)

        assert svc._waiting_inbox_episodes["worker1"].waiting_since == 100.0
        fakes["create"].assert_not_called()

    def test_b_crossing_grace_pushes_exactly_once_to_caller(self):
        svc = StalledCallbackWatchdog()
        metadata = {"id": "worker1", "caller_id": "caller1", "agent_profile": None}
        with _waiting_inbox_fakes(metadata=metadata) as fakes:
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=110.0)

        fakes["create"].assert_called_once()
        sender_id, caller_id, message = fakes["create"].call_args.args
        assert sender_id == "watchdog:worker1"
        assert caller_id == "caller1"
        assert "[waiting-inbox watchdog] terminal worker1 (unknown)" in message
        assert "for 10s" in message
        fakes["deliver"].assert_called_once_with("caller1", registry=None)

    def test_c_fired_episode_does_not_push_twice(self):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes() as fakes:
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=110.0)
            svc.tick_waiting_inbox(now=200.0)

        fakes["create"].assert_called_once()

    def test_d_pending_rows_changing_during_wait_do_not_reopen_episode(self):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes() as fakes:
            fakes["pending"].side_effect = [
                ["worker1"],
                ["worker1", "worker1"],
                ["worker1"],
                ["worker1", "worker1"],
            ]
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=105.0)
            svc.tick_waiting_inbox(now=110.0)
            svc.tick_waiting_inbox(now=200.0)

        fakes["create"].assert_called_once()

    def test_e_drain_closes_episode_and_new_episode_obeys_push_floor(self):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes() as fakes:
            fakes["pending"].side_effect = [
                ["worker1"],
                ["worker1"],
                [],
                ["worker1"],
                ["worker1"],
                ["worker1"],
            ]
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=110.0)
            svc.tick_waiting_inbox(now=111.0)
            assert "worker1" not in svc._waiting_inbox_episodes
            assert svc._waiting_inbox_last_push["worker1"] == 110.0
            svc.tick_waiting_inbox(now=120.0)
            svc.tick_waiting_inbox(now=130.0)
            assert fakes["create"].call_count == 1
            assert not svc._waiting_inbox_episodes["worker1"].fired
            svc.tick_waiting_inbox(now=411.0)

        assert fakes["create"].call_count == 2

    def test_f_waiting_flap_resets_waiting_since(self):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes() as fakes:
            fakes["status"].side_effect = [
                TerminalStatus.WAITING_USER_ANSWER,
                TerminalStatus.PROCESSING,
                TerminalStatus.WAITING_USER_ANSWER,
                TerminalStatus.WAITING_USER_ANSWER,
                TerminalStatus.WAITING_USER_ANSWER,
            ]
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=105.0)
            svc.tick_waiting_inbox(now=106.0)
            svc.tick_waiting_inbox(now=115.0)
            assert fakes["create"].call_count == 0
            svc.tick_waiting_inbox(now=116.0)

        assert fakes["create"].call_count == 1

    @pytest.mark.parametrize("gate", ["unknown_dialog", "wait_rule", "retry_exhausted"])
    def test_g_each_waiting_gate_suppresses_push(self, gate):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes(gate=gate) as fakes:
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=110.0)

        fakes["create"].assert_not_called()
        assert not svc._waiting_inbox_episodes["worker1"].fired

    def test_g_gate_opening_at_due_tick_suppresses_race(self):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes() as fakes:
            fakes["gate"].return_value = "unknown_dialog"
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=110.0)

        fakes["create"].assert_not_called()
        assert not svc._waiting_inbox_episodes["worker1"].fired

    def test_h_deleted_terminal_is_pruned_without_push(self):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes(metadata={}) as fakes:
            svc._waiting_inbox_episodes["worker1"] = MagicMock()
            fakes["metadata"].return_value = None
            svc.tick_waiting_inbox(now=100.0)

        assert "worker1" not in svc._waiting_inbox_episodes
        fakes["status"].assert_not_called()
        fakes["create"].assert_not_called()

    @pytest.mark.parametrize("caller_id", [None, "worker1"])
    def test_i_invalid_caller_warns_and_permanently_suppresses_episode(self, caller_id, caplog):
        svc = StalledCallbackWatchdog()
        metadata = {
            "id": "worker1",
            "caller_id": caller_id,
            "agent_profile": "developer",
        }
        with _waiting_inbox_fakes(metadata=metadata) as fakes:
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=110.0)
            metadata["caller_id"] = "caller1"
            svc.tick_waiting_inbox(now=120.0)

        assert svc._waiting_inbox_episodes["worker1"].fired
        fakes["create"].assert_not_called()
        assert "refusing invalid caller" in caplog.text

    def test_j_non_waiting_status_has_no_episode(self):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes(status=TerminalStatus.PROCESSING) as fakes:
            svc.tick_waiting_inbox(now=100.0)

        assert "worker1" not in svc._waiting_inbox_episodes
        fakes["gate"].assert_not_called()
        fakes["create"].assert_not_called()

    def test_k_gate_clearing_after_grace_pushes_on_next_tick(self):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes(gate="wait_rule") as fakes:
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=110.0)
            fakes["gate"].return_value = None
            svc.tick_waiting_inbox(now=111.0)

        fakes["create"].assert_called_once()
        assert "for 11s" in fakes["create"].call_args.args[2]

    def test_l_gate_precedes_invalid_caller_suppression(self):
        svc = StalledCallbackWatchdog()
        metadata = {"id": "worker1", "caller_id": None, "agent_profile": "developer"}
        with _waiting_inbox_fakes(metadata=metadata, gate="wait_rule") as fakes:
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=110.0)
            assert not svc._waiting_inbox_episodes["worker1"].fired
            metadata["caller_id"] = "caller1"
            fakes["gate"].return_value = None
            svc.tick_waiting_inbox(now=111.0)

        fakes["create"].assert_called_once()

    def test_m_invalid_caller_precedes_active_cross_episode_floor(self):
        svc = StalledCallbackWatchdog()
        metadata = {"id": "worker1", "caller_id": None, "agent_profile": "developer"}
        svc._waiting_inbox_last_push["worker1"] = 105.0
        with _waiting_inbox_fakes(metadata=metadata) as fakes:
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=110.0)

        assert svc._waiting_inbox_episodes["worker1"].fired
        fakes["create"].assert_not_called()

    def test_n_transport_failure_commits_episode_and_floor_without_retry(self, caplog):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes() as fakes:
            fakes["create"].side_effect = RuntimeError("transport failed")
            svc.tick_waiting_inbox(now=100.0)
            svc.tick_waiting_inbox(now=110.0)
            svc.tick_waiting_inbox(now=120.0)

        assert svc._waiting_inbox_episodes["worker1"].fired
        assert svc._waiting_inbox_last_push["worker1"] == 110.0
        fakes["create"].assert_called_once()
        fakes["deliver"].assert_not_called()
        assert "Failed to push waiting-inbox watchdog" in caplog.text

    def test_clear_terminal_drops_episode_and_cross_episode_floor(self):
        svc = StalledCallbackWatchdog()
        with _waiting_inbox_fakes():
            svc.tick_waiting_inbox(now=100.0)
        svc._waiting_inbox_last_push["worker1"] = 90.0

        svc.clear_terminal("worker1")

        assert "worker1" not in svc._waiting_inbox_episodes
        assert "worker1" not in svc._waiting_inbox_last_push
