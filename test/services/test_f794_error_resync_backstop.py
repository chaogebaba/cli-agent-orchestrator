"""F794 (#651): a published ERROR must self-heal from the pane, and the seat's
queued inbox must then drain.

Screen/raw status detection is OUTPUT-driven: it runs on pipe-pane chunks plus
one quiescence tick after the last chunk. A worker that finishes its turn and
parks emits nothing more, so whatever verdict the final tick produced is the
last one, forever. The low-frequency pane-tail backstop in
``resync_from_pane_tail`` already covered a stuck PROCESSING (#558); ERROR is
the mirror-image failure and was NOT covered, which is why cline terminals
f5824e3d and 2b31cd53 sat at ``error [BUSY]`` for tens of minutes with
undelivered mail (delivery pastes only to IDLE/COMPLETED).

The backstop costs no extra pane capture: pane_liveness.observe already samples
every live terminal each watchdog tick and peek() hands back the retained tail.
"""

from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import status_monitor as status_monitor_module


def _monitor_with_provider(monkeypatch, published: TerminalStatus, detected: TerminalStatus):
    monitor = status_monitor_module.StatusMonitor()
    monitor._last_status["t1"] = published
    monitor._drop_seq_seen["t1"] = 0
    monitor._last_publish_monotonic["t1"] = 1.0
    provider = MagicMock()
    provider.get_status.return_value = detected
    bus = MagicMock()
    bus.get_drop_seq.return_value = 0  # no drop: the PERIODIC backstop only
    monkeypatch.setattr(status_monitor_module, "bus", bus)
    monkeypatch.setattr(
        status_monitor_module.provider_manager, "get_provider", lambda _tid: provider
    )
    return monitor, provider, bus


def test_error_backstop_redetects_from_the_pane_after_the_interval(monkeypatch) -> None:
    """The defect: a seat published ERROR never got re-derived. It does now."""
    monitor, provider, _bus = _monitor_with_provider(
        monkeypatch, TerminalStatus.ERROR, TerminalStatus.COMPLETED
    )

    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.record_published_status"
    ):
        ran = monitor.resync_from_pane_tail("t1", "dispatcher parked at cat", now=1000.0)

    assert ran is True
    provider.get_status.assert_called_once_with("dispatcher parked at cat")
    assert monitor._last_status["t1"] is TerminalStatus.COMPLETED


def test_error_backstop_leaves_a_genuinely_broken_seat_at_error(monkeypatch) -> None:
    """Self-heal must not invent health: a pane that still reads ERROR keeps it
    and publishes nothing."""
    monitor, _provider, bus = _monitor_with_provider(
        monkeypatch, TerminalStatus.ERROR, TerminalStatus.ERROR
    )

    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.record_published_status"
    ):
        assert monitor.resync_from_pane_tail("t1", "crashed", now=1000.0) is True

    assert monitor._last_status["t1"] is TerminalStatus.ERROR
    bus.publish.assert_not_called()


def test_error_backstop_runs_at_most_once_per_interval(monkeypatch) -> None:
    """Same rate limit as the PROCESSING backstop — no per-tick pane churn."""
    monitor, provider, _bus = _monitor_with_provider(
        monkeypatch, TerminalStatus.ERROR, TerminalStatus.ERROR
    )
    interval = status_monitor_module.StatusMonitor._resync_interval_s()

    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.record_published_status"
    ):
        assert monitor.resync_from_pane_tail("t1", "sample", now=1.0 + interval) is True
        assert monitor.resync_from_pane_tail("t1", "sample", now=1.0 + interval + 1.0) is False

    provider.get_status.assert_called_once()


def test_quiescent_statuses_are_not_backstopped(monkeypatch) -> None:
    """IDLE/COMPLETED seats are already deliverable; they stay off the backstop
    so its cost remains the pre-F794 cost."""
    monitor, provider, _bus = _monitor_with_provider(
        monkeypatch, TerminalStatus.COMPLETED, TerminalStatus.COMPLETED
    )

    assert monitor.resync_from_pane_tail("t1", "sample", now=1000.0) is False
    provider.get_status.assert_not_called()


def test_recovered_seat_becomes_deliverable_and_its_queued_message_drains(
    monkeypatch,
) -> None:
    """The user-visible consequence: message 4663 was never delivered because
    delivery pastes only to IDLE/COMPLETED and the seat was pinned at ERROR.
    Once the backstop recovers the status, the seat is inside the delivery gate
    and the queued message is handed over exactly once."""
    monitor, _provider, _bus = _monitor_with_provider(
        monkeypatch, TerminalStatus.ERROR, TerminalStatus.COMPLETED
    )
    deliverable = {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
    inbox: list[str] = ["message 4663"]
    delivered: list[str] = []

    def _drain() -> None:
        if monitor._last_status["t1"] in deliverable and inbox:
            delivered.append(inbox.pop(0))

    # Before the backstop the seat is outside the gate: nothing drains.
    _drain()
    assert delivered == []

    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.record_published_status"
    ):
        monitor.resync_from_pane_tail("t1", "dispatcher parked at cat", now=1000.0)

    assert monitor._last_status["t1"] in deliverable
    _drain()
    _drain()  # a second cycle must not re-deliver
    assert delivered == ["message 4663"]
    assert inbox == []
