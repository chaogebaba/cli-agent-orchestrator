"""F582 D14: provider-local, epoch-attributed Cline abort truth."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import cline_cli
from cli_agent_orchestrator.providers.cline_cli import (
    _ABORT_REPORT_HOLD_S,
    ABORT_LINE,
    DISPATCHER_IDLE_CMD,
    ClineCliProvider,
)
from cli_agent_orchestrator.services.agent_step import _CompletionOutcome, _wait_for_completion
from cli_agent_orchestrator.services.pane_liveness import PANE_LIVENESS_TAIL_LINES
from cli_agent_orchestrator.services.status_monitor import StatusMonitor, status_monitor


@pytest.fixture
def provider() -> ClineCliProvider:
    instance = ClineCliProvider("f582", "session", "window", agent_profile="cline_dev")
    instance._initialized = True
    instance.shell_baseline = "zsh"
    instance._resolve_native_status = lambda: None  # type: ignore[method-assign]
    instance._pane_cmd = lambda: DISPATCHER_IDLE_CMD  # type: ignore[method-assign]
    return instance


def _authoritative(*lines: str) -> str:
    filler = [f"buffer line {index}" for index in range(PANE_LIVENESS_TAIL_LINES + 1)]
    return "\n".join([*filler, *lines])


def _clock(monkeypatch: pytest.MonkeyPatch, value: list[float]) -> None:
    monkeypatch.setattr(cline_cli, "time", SimpleNamespace(monotonic=lambda: value[0]))


def test_abort_report_is_an_occurrence_edge_then_hold_then_idle(
    provider: ClineCliProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider._task_dispatched_flag = True
    now = [10.0]
    _clock(monkeypatch, now)
    retry = MagicMock()
    monkeypatch.setattr(status_monitor, "schedule_detection_retry", retry)
    output = _authoritative(ABORT_LINE)

    assert provider.get_status(output) is TerminalStatus.ERROR
    assert provider._abort_reported_occ == 1
    assert provider._abort_retry_armed is True
    now[0] += 1.0
    assert provider.get_status(output) is TerminalStatus.ERROR
    now[0] = 10.0 + _ABORT_REPORT_HOLD_S
    assert provider.get_status(output) is TerminalStatus.IDLE

    assert retry.call_count == 2
    retry.assert_called_with("f582", delay_s=_ABORT_REPORT_HOLD_S)


def test_reported_abort_never_falls_through_to_completed(
    provider: ClineCliProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider._task_dispatched_flag = True
    now = [0.0]
    _clock(monkeypatch, now)
    monkeypatch.setattr(status_monitor, "schedule_detection_retry", MagicMock())
    output = _authoritative(ABORT_LINE)

    assert provider.get_status(output) is TerminalStatus.ERROR
    now[0] = _ABORT_REPORT_HOLD_S
    assert provider.get_status(output) is TerminalStatus.IDLE
    assert provider.get_status(output) is TerminalStatus.IDLE


def test_new_epoch_reports_a_fresh_abort_again(
    provider: ClineCliProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider._task_dispatched_flag = True
    now = [0.0]
    _clock(monkeypatch, now)
    monkeypatch.setattr(status_monitor, "schedule_detection_retry", MagicMock())
    output = _authoritative(ABORT_LINE)

    assert provider.get_status(output) is TerminalStatus.ERROR
    now[0] = _ABORT_REPORT_HOLD_S
    assert provider.get_status(output) is TerminalStatus.IDLE
    provider.notify_status_buffer_reset(2)
    now[0] += 1.0
    assert provider.get_status(output) is TerminalStatus.ERROR
    assert provider._abort_reported_occ == 1


def test_reset_hook_clears_exactly_the_three_epoch_fields(
    provider: ClineCliProvider,
) -> None:
    provider._abort_reported_occ = 7
    provider._abort_reported_at = 12.5
    provider._abort_retry_armed = True
    provider._message_count = 9

    provider.notify_status_buffer_reset(44)

    assert provider._abort_reported_occ == 0
    assert provider._abort_reported_at is None
    assert provider._abort_retry_armed is False
    assert provider._message_count == 9


def test_sub_floor_abort_is_accepted_as_completed(
    provider: ClineCliProvider,
) -> None:
    provider._task_dispatched_flag = True
    output = "\n".join([ABORT_LINE, *["short run"] * (PANE_LIVENESS_TAIL_LINES - 1)])

    assert provider.get_status(output) is TerminalStatus.COMPLETED
    assert provider._abort_reported_occ == 0
    assert provider._abort_reported_at is None


def test_d15_tail_from_previous_run_is_non_authoritative(
    provider: ClineCliProvider,
) -> None:
    provider._task_dispatched_flag = True
    provider.notify_status_buffer_reset(2)
    tail = "\n".join([*["old pane row"] * (PANE_LIVENESS_TAIL_LINES - 1), ABORT_LINE])

    assert provider.get_status(tail) is TerminalStatus.COMPLETED
    assert provider._abort_reported_occ == 0


def test_new_run_clean_buffer_never_replays_previous_abort(
    provider: ClineCliProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider._task_dispatched_flag = True
    now = [0.0]
    _clock(monkeypatch, now)
    monkeypatch.setattr(status_monitor, "schedule_detection_retry", MagicMock())

    assert provider.get_status(_authoritative(ABORT_LINE)) is TerminalStatus.ERROR
    provider.notify_status_buffer_reset(2)
    assert provider.get_status(_authoritative("clean completion")) is TerminalStatus.COMPLETED


def test_dispatcher_crash_remains_error_even_with_visible_abort(
    provider: ClineCliProvider,
) -> None:
    provider._task_dispatched_flag = True
    provider._pane_cmd = lambda: "zsh"  # type: ignore[method-assign]

    assert provider.get_status(_authoritative(ABORT_LINE)) is TerminalStatus.ERROR
    assert provider._abort_reported_occ == 0


def test_fresh_instance_ignores_old_abort_then_clean_dispatch_completes(
    provider: ClineCliProvider,
) -> None:
    old_pane = _authoritative(ABORT_LINE)
    assert provider.get_status(old_pane) is TerminalStatus.IDLE

    provider._task_dispatched_flag = True
    provider.notify_status_buffer_reset(1)
    assert provider.get_status(_authoritative("clean")) is TerminalStatus.COMPLETED


def test_retry_is_a_leaf_call_with_provider_flush_lock_unheld(
    provider: ClineCliProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider._task_dispatched_flag = True
    monkeypatch.setattr(cline_cli, "time", SimpleNamespace(monotonic=lambda: 0.0))
    observed = []

    def assert_unheld(*_args, **_kwargs) -> None:
        observed.append(not provider._flush_lock._is_owned())  # type: ignore[attr-defined]

    monkeypatch.setattr(status_monitor, "schedule_detection_retry", assert_unheld)

    assert provider.get_status(_authoritative(ABORT_LINE)) is TerminalStatus.ERROR
    assert observed == [True]


def test_abort_path_uses_only_the_permitted_state_and_monitor_api() -> None:
    source = Path("src/cli_agent_orchestrator/providers/cline_cli.py").read_text(encoding="utf-8")
    get_status = source[
        source.index("    def get_status(") : source.index(
            "    def classify_", source.index("    def get_status(")
        )
    ]
    abort_rule = get_status[: get_status.index("# Correlate session ID")]

    assert "_ABORT_SCAN_LINES" not in source
    assert "_abort_epoch_maxlen" not in source
    assert "_abort_evidence_hwm" not in source
    assert "_message_count" not in abort_rule
    assert "len(lines) <= PANE_LIVENESS_TAIL_LINES" in abort_rule
    assert "schedule_detection_retry" in abort_rule
    assert "recovery_state" not in get_status


def test_abort_1_fixture_is_the_accepted_sub_floor_residual(
    provider: ClineCliProvider,
) -> None:
    fixture = (
        Path(__file__).parent / "fixtures" / "status_truth" / "cline_cli" / "abort-1.txt"
    ).read_text(encoding="utf-8")
    provider._task_dispatched_flag = True

    assert ABORT_LINE in fixture
    assert len(fixture.splitlines()) <= PANE_LIVENESS_TAIL_LINES
    assert provider.get_status(fixture) is TerminalStatus.COMPLETED
    assert provider._abort_reported_occ == 0


def test_abort_2_replay_reports_once_then_closes_idle_never_completed(
    provider: ClineCliProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = (
        Path(__file__).parent / "fixtures" / "status_truth" / "cline_cli" / "abort-2.txt"
    ).read_text(encoding="utf-8")
    provider._task_dispatched_flag = True
    now = [100.0]
    _clock(monkeypatch, now)
    retry = MagicMock()
    monkeypatch.setattr(status_monitor, "schedule_detection_retry", retry)

    assert ABORT_LINE in fixture
    assert len(fixture.splitlines()) > PANE_LIVENESS_TAIL_LINES
    assert provider.get_status(fixture) is TerminalStatus.ERROR
    assert provider._abort_reported_occ == 1
    now[0] += 1.0
    assert provider.get_status(fixture) is TerminalStatus.ERROR
    now[0] = 100.0 + _ABORT_REPORT_HOLD_S
    assert provider.get_status(fixture) is TerminalStatus.IDLE
    assert provider.get_status(fixture) is TerminalStatus.IDLE
    assert retry.call_count == 2


def test_level_consumer_sees_held_error_after_first_detection_is_discarded(
    provider: ClineCliProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider._task_dispatched_flag = True
    now = [0.0]
    _clock(monkeypatch, now)
    monkeypatch.setattr(status_monitor, "schedule_detection_retry", MagicMock())
    output = _authoritative(ABORT_LINE)

    # The first report is deliberately discarded, as if its chunk generation
    # became stale between detection and apply.
    assert provider.get_status(output) is TerminalStatus.ERROR
    now[0] = 1.0
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.agent_step.receiver_state_view.snapshot_view",
        lambda *_args, **_kwargs: provider.get_status(output),
    )

    outcome = asyncio.run(_wait_for_completion("f582", timeout=1.1, polling_interval=1.0))

    assert outcome is _CompletionOutcome.ERROR


def test_over_cap_head_trimmed_abort_is_the_accepted_residual(
    provider: ClineCliProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider._task_dispatched_flag = True
    monitor = StatusMonitor()
    output = ABORT_LINE + "\n" + "\n".join(f"long output {index}" for index in range(800))
    assert len(output) > 4096
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda _tid: provider,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.get_server_settings",
        lambda: {"state_buffer_max": 4096},
    )
    bus = MagicMock()
    bus.get_drop_seq.return_value = 0
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.bus", bus)

    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.record_published_status"
    ):
        monitor._process_chunk(provider.terminal_id, output)

    assert ABORT_LINE not in monitor.get_buffer(provider.terminal_id)
    assert monitor._last_status[provider.terminal_id] is TerminalStatus.COMPLETED
    assert provider._abort_reported_occ == 0


def test_clear_without_registered_provider_never_resets_provider_abort_state(
    provider: ClineCliProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider._abort_reported_occ = 7
    monitor = StatusMonitor()

    monitor.clear_rolling_buffer(provider.terminal_id, provider=None)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda _tid: None,
    )

    assert provider._abort_reported_occ == 7
    assert monitor._detect_status(provider.terminal_id, "") is TerminalStatus.UNKNOWN


def test_reset_hook_and_dispatch_path_never_read_monitor_buffer() -> None:
    provider_source = Path("src/cli_agent_orchestrator/providers/cline_cli.py").read_text(
        encoding="utf-8"
    )
    hook_start = provider_source.index("    def notify_status_buffer_reset")
    hook_end = provider_source.index("\n    def ", hook_start + 1)
    hook = provider_source[hook_start:hook_end]
    dispatch_start = provider_source.index("    def _after_dispatch_commit_locked")
    dispatch_end = provider_source.index("\n    def ", dispatch_start + 1)
    dispatch = provider_source[dispatch_start:dispatch_end]

    assert "status_monitor" not in hook
    assert "get_buffer" not in hook
    assert "get_buffer" not in dispatch
