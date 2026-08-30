"""F521 D15: dropped falling-edge recovery from the independent pane level."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import event_bus as event_bus_module
from cli_agent_orchestrator.services import status_monitor as status_monitor_module


def test_drop_seq_is_monotonic_per_terminal_and_ignores_other_topics() -> None:
    bus = event_bus_module.EventBus()

    bus._record_drop("terminal.t1.output")
    bus._record_drop("terminal.t1.status")
    bus._record_drop("terminal.t2.output")
    bus._record_drop("system.health")

    assert bus.get_drop_seq("t1") == 2
    assert bus.get_drop_seq("t2") == 1
    assert bus.get_drop_seq("health") == 0


def test_pruning_a_terminal_drop_level_is_observed_as_a_change(monkeypatch) -> None:
    bus = event_bus_module.EventBus()
    monkeypatch.setattr(event_bus_module, "_DROP_STATE_TTL_SECS", 1.0)
    monkeypatch.setattr(event_bus_module.time, "monotonic", lambda: 1.0)
    bus._record_drop("terminal.t1.output")
    assert bus.get_drop_seq("t1") == 1

    bus._prune_drop_state(3.0)

    assert bus.get_drop_seq("t1") == 0


def test_drop_forces_pane_tail_redetect_and_publishes_ready_with_audit_reason(
    monkeypatch,
) -> None:
    monitor = status_monitor_module.StatusMonitor()
    monitor._last_status["t1"] = TerminalStatus.PROCESSING
    monitor._drop_seq_seen["t1"] = 0
    monitor._last_publish_monotonic["t1"] = 1.0
    provider = MagicMock()
    provider.get_status.return_value = TerminalStatus.COMPLETED
    bus = MagicMock()
    bus.get_drop_seq.return_value = 1
    monkeypatch.setattr(status_monitor_module, "bus", bus)
    monkeypatch.setattr(
        status_monitor_module.provider_manager, "get_provider", lambda _tid: provider
    )

    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.record_published_status"
    ):
        assert monitor.resync_from_pane_tail("t1", "pane says done", now=2.0) is True

    provider.get_status.assert_called_once_with("pane says done")
    assert monitor._last_status["t1"] is TerminalStatus.COMPLETED
    bus.publish.assert_called_once_with(
        "terminal.t1.status",
        {"status": "completed", "fusion_reason": "resync_after_drop"},
    )
    assert monitor.get_boundary_observation("t1").fusion_reason == "resync_after_drop"


async def _queue_full_recovery(monkeypatch) -> tuple[dict, TerminalStatus]:
    bus = event_bus_module.EventBus()
    bus.set_loop(asyncio.get_running_loop())
    monkeypatch.setattr(
        event_bus_module,
        "get_server_settings",
        lambda: {"event_bus_max_queue_size": 1},
    )
    output_queue = bus.subscribe("terminal.*.output")
    status_queue = bus.subscribe("terminal.*.status")
    output_queue.put_nowait({"occupied": True})
    bus._dispatch("terminal.t1.output", {"data": "completion bytes"})
    assert bus.get_drop_seq("t1") == 1

    monitor = status_monitor_module.StatusMonitor()
    monitor._last_status["t1"] = TerminalStatus.PROCESSING
    monitor._drop_seq_seen["t1"] = 0
    monitor._last_publish_monotonic["t1"] = 1.0
    provider = MagicMock()
    provider.get_status.return_value = TerminalStatus.COMPLETED
    monkeypatch.setattr(status_monitor_module, "bus", bus)
    monkeypatch.setattr(
        status_monitor_module.provider_manager, "get_provider", lambda _tid: provider
    )
    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.record_published_status"
    ):
        assert monitor.resync_from_pane_tail("t1", "pane completed", now=2.0) is True
        event = await asyncio.wait_for(status_queue.get(), timeout=0.5)
    bus.set_loop(None)
    return event, monitor._last_status["t1"]


def test_full_output_queue_drop_recovers_the_lost_falling_edge(monkeypatch) -> None:
    event, status = asyncio.run(_queue_full_recovery(monkeypatch))

    assert status is TerminalStatus.COMPLETED
    assert event == {
        "topic": "terminal.t1.status",
        "data": {"status": "completed", "fusion_reason": "resync_after_drop"},
    }


def test_consumed_drop_level_does_not_redetect_again(monkeypatch) -> None:
    monitor = status_monitor_module.StatusMonitor()
    monitor._last_status["t1"] = TerminalStatus.PROCESSING
    monitor._last_publish_monotonic["t1"] = 5.0
    provider = MagicMock()
    provider.get_status.return_value = TerminalStatus.PROCESSING
    bus = MagicMock()
    bus.get_drop_seq.return_value = 4
    monkeypatch.setattr(status_monitor_module, "bus", bus)
    monkeypatch.setattr(
        status_monitor_module.provider_manager, "get_provider", lambda _tid: provider
    )

    assert monitor.resync_from_pane_tail("t1", "still running", now=6.0) is True
    assert monitor.resync_from_pane_tail("t1", "still running", now=7.0) is False
    provider.get_status.assert_called_once_with("still running")


def test_processing_backstop_runs_once_per_interval_without_a_drop(monkeypatch) -> None:
    monitor = status_monitor_module.StatusMonitor()
    monitor._last_status["t1"] = TerminalStatus.PROCESSING
    monitor._drop_seq_seen["t1"] = 0
    monitor._last_publish_monotonic["t1"] = 10.0
    provider = MagicMock()
    provider.get_status.return_value = TerminalStatus.PROCESSING
    bus = MagicMock()
    bus.get_drop_seq.return_value = 0
    monkeypatch.setattr(status_monitor_module, "bus", bus)
    monkeypatch.setattr(
        status_monitor_module.provider_manager, "get_provider", lambda _tid: provider
    )
    monkeypatch.setattr(monitor, "_resync_interval_s", lambda: 60.0)

    assert monitor.resync_from_pane_tail("t1", "sample", now=69.9) is False
    assert monitor.resync_from_pane_tail("t1", "sample", now=70.0) is True
    assert monitor.resync_from_pane_tail("t1", "sample", now=100.0) is False
    assert monitor.resync_from_pane_tail("t1", "sample", now=130.0) is True
    assert provider.get_status.call_count == 2


def test_no_usable_pane_sample_means_no_forced_detection() -> None:
    # The exact tick integration is grep-shaped: the no-sample continue
    # precedes the D15 call, so None never reaches the provider.
    source = Path("src/cli_agent_orchestrator/services/stalled_callback_watchdog.py").read_text(
        encoding="utf-8"
    )
    no_sample = source.index("if observation is None:")
    peek = source.index("retained = pane_liveness.peek", no_sample)
    no_retained = source.index("if retained is not None:", peek)
    resync = source.index("status_monitor.resync_from_pane_tail", no_retained)
    assert no_sample < peek < no_retained < resync


def test_d15_adds_no_new_pane_capture_call() -> None:
    source = Path("src/cli_agent_orchestrator/services/status_monitor.py").read_text(
        encoding="utf-8"
    )
    start = source.index("    def resync_from_pane_tail(")
    end = source.index("\n    def ", start + 1)
    body = source[start:end]
    assert "capture_viewport" not in body
    assert "get_history" not in body
    assert "pane_liveness.observe" not in body
