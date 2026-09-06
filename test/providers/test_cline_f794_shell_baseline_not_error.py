"""F794 (#651): a warm cline worker parked at the dispatcher ``cat`` must read
IDLE/COMPLETED, never a wedged ERROR that starves its inbox.

Live incident (session cao-claude-orch5, terminal f5824e3d, 2026-09-06): the
secretary answered Q1, the one-shot ``cline`` exited, and the detection tick
that followed sampled the pane while the dispatcher's ``while`` loop was
between iterations — ``pane_current_command`` was the shell baseline, not yet
``cat``. ``get_status`` mapped that single reading to ERROR
(cline_cli.py, pre-fix "Dispatcher exited back to shell → error"). Because
screen/raw detection is OUTPUT-driven, a worker that then parks at ``cat``
emits nothing more, so that wrong verdict was the final one: the seat read
``error [BUSY]`` for the rest of its life and message 4663 was never delivered
(delivery pastes only to IDLE/COMPLETED). Reviewer terminal 2b31cd53 repeated
it verbatim.

The fix requires the baseline reading to PERSIST for ``_BASELINE_CONFIRM_S``
before it means "the dispatcher died" — a real crash never leaves the baseline,
a healthy loop iteration leaves it in microseconds.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.cline_cli import (
    _BASELINE_CONFIRM_S,
    DISPATCHER_IDLE_CMD,
    ClineCliProvider,
)
from cli_agent_orchestrator.services.pane_liveness import PANE_LIVENESS_TAIL_LINES
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


@pytest.fixture
def provider() -> ClineCliProvider:
    instance = ClineCliProvider("f5824e3d", "cao-claude-orch5", "window", agent_profile="secretary")
    instance._initialized = True
    instance.shell_baseline = "zsh"
    instance._resolve_native_status = lambda: None  # type: ignore[method-assign]
    return instance


def _authoritative(*lines: str) -> str:
    """A pane tail long enough to be authoritative for the abort scan."""
    filler = [f"buffer line {index}" for index in range(PANE_LIVENESS_TAIL_LINES + 1)]
    return "\n".join([*filler, *lines])


def test_first_shell_baseline_sample_is_processing_not_error(
    provider: ClineCliProvider,
) -> None:
    """The observed trigger: one tick lands between ``cline`` runs."""
    provider._task_dispatched_flag = True
    provider._pane_cmd = lambda: "zsh"  # type: ignore[method-assign]

    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.schedule_detection_retry"
    ):
        assert provider.get_status(_authoritative("Answered Q1")) is TerminalStatus.PROCESSING


def test_first_shell_baseline_sample_rearms_detection(provider: ClineCliProvider) -> None:
    """The unconfirmed verdict must re-arm a detection tick, or the confirmation
    would never happen on a worker that has stopped emitting output."""
    provider._task_dispatched_flag = True
    provider._pane_cmd = lambda: "zsh"  # type: ignore[method-assign]

    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.schedule_detection_retry"
    ) as retry:
        provider.get_status(_authoritative("Answered Q1"))

    retry.assert_called_once_with("f5824e3d", delay_s=_BASELINE_CONFIRM_S)


def test_parked_at_cat_with_no_abort_line_is_completed(provider: ClineCliProvider) -> None:
    """The exact reported end-state: pane cmd ``cat``, task dispatched, no
    ``[abort]`` line. Must be COMPLETED so the inbox can drain."""
    provider._task_dispatched_flag = True
    provider._pane_cmd = lambda: DISPATCHER_IDLE_CMD  # type: ignore[method-assign]

    assert provider.get_status(_authoritative("Answered Q1")) is TerminalStatus.COMPLETED


def test_baseline_flicker_then_cat_recovers_to_ready(provider: ClineCliProvider) -> None:
    """The whole incident in one test: flicker, then the pane settles on ``cat``."""
    provider._task_dispatched_flag = True
    pane = {"cmd": "zsh"}
    provider._pane_cmd = lambda: pane["cmd"]  # type: ignore[method-assign]
    output = _authoritative("Answered Q1")

    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.schedule_detection_retry"
    ):
        assert provider.get_status(output) is TerminalStatus.PROCESSING
    pane["cmd"] = DISPATCHER_IDLE_CMD
    assert provider.get_status(output) is TerminalStatus.COMPLETED


def test_cat_sighting_closes_the_baseline_episode(provider: ClineCliProvider) -> None:
    """A later, unrelated baseline sighting must start its OWN confirmation
    window rather than inheriting the stale first-seen stamp (which would make
    the very first sample of the next episode report ERROR immediately)."""
    provider._task_dispatched_flag = True
    pane = {"cmd": "zsh"}
    clock = {"t": 0.0}
    provider._pane_cmd = lambda: pane["cmd"]  # type: ignore[method-assign]
    output = _authoritative("Answered Q1")

    with patch("cli_agent_orchestrator.providers.cline_cli.time.monotonic", lambda: clock["t"]):
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor"
            ".schedule_detection_retry"
        ):
            assert provider.get_status(output) is TerminalStatus.PROCESSING
            pane["cmd"] = DISPATCHER_IDLE_CMD
            assert provider.get_status(output) is TerminalStatus.COMPLETED
            # Much later, a fresh flicker: still PROCESSING, not an instant ERROR.
            clock["t"] = 3600.0
            pane["cmd"] = "zsh"
            assert provider.get_status(output) is TerminalStatus.PROCESSING


def test_persistent_shell_baseline_still_reports_error(provider: ClineCliProvider) -> None:
    """Crash detection is preserved: a dispatcher that never returns to ``cat``
    reports ERROR once the reading has persisted past the confirm window."""
    provider._task_dispatched_flag = True
    clock = {"t": 0.0}
    provider._pane_cmd = lambda: "zsh"  # type: ignore[method-assign]
    output = _authoritative("crashed dispatcher")

    with patch("cli_agent_orchestrator.providers.cline_cli.time.monotonic", lambda: clock["t"]):
        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor"
            ".schedule_detection_retry"
        ):
            assert provider.get_status(output) is TerminalStatus.PROCESSING
        clock["t"] = _BASELINE_CONFIRM_S + 0.1
        assert provider.get_status(output) is TerminalStatus.ERROR


def test_monitor_never_latches_error_from_a_baseline_flicker(
    monkeypatch: pytest.MonkeyPatch, provider: ClineCliProvider
) -> None:
    """End-to-end through StatusMonitor: a flicker chunk followed by a quiet
    ``cat`` chunk leaves the published status COMPLETED, never ERROR."""
    provider._task_dispatched_flag = True
    pane = {"cmd": "zsh"}
    provider._pane_cmd = lambda: pane["cmd"]  # type: ignore[method-assign]

    monitor = StatusMonitor()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda _tid: provider,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.get_server_settings",
        lambda: {"state_buffer_max": 65536},
    )
    bus = MagicMock()
    bus.get_drop_seq.return_value = 0
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.bus", bus)

    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.record_published_status"
    ):
        monitor._process_chunk(provider.terminal_id, _authoritative("Q1 answer"))
        assert monitor._last_status[provider.terminal_id] is not TerminalStatus.ERROR
        pane["cmd"] = DISPATCHER_IDLE_CMD
        monitor._process_chunk(provider.terminal_id, _authoritative("Q1 answer done"))

    assert monitor._last_status[provider.terminal_id] is TerminalStatus.COMPLETED
