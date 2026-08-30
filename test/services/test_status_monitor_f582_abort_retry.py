"""F582 D14 live-loop reachability and slot-theft regression arms."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.cline_cli import (
    _ABORT_REPORT_HOLD_S,
    ABORT_LINE,
    DISPATCHER_IDLE_CMD,
    ClineCliProvider,
)
from cli_agent_orchestrator.services import inbox_service as inbox_module
from cli_agent_orchestrator.services import status_monitor as status_module
from cli_agent_orchestrator.services.event_bus import EventBus
from cli_agent_orchestrator.services.pane_liveness import PANE_LIVENESS_TAIL_LINES


def _provider() -> ClineCliProvider:
    provider = ClineCliProvider("f582-live", "session", "window", agent_profile="cline_dev")
    provider._initialized = True
    provider._task_dispatched_flag = True
    provider.shell_baseline = "zsh"
    provider._resolve_native_status = lambda: None  # type: ignore[method-assign]
    provider._pane_cmd = lambda: DISPATCHER_IDLE_CMD  # type: ignore[method-assign]
    return provider


def _abort_buffer() -> str:
    fixture = (
        Path(__file__).parents[1]
        / "providers"
        / "fixtures"
        / "status_truth"
        / "cline_cli"
        / "abort-2.txt"
    )
    output = fixture.read_text(encoding="utf-8")
    assert ABORT_LINE in output
    assert len(output.splitlines()) > PANE_LIVENESS_TAIL_LINES
    return output


async def _wait_until_idle(monitor: status_module.StatusMonitor, timeout: float) -> None:
    async def poll() -> None:
        while monitor._last_status.get("f582-live") is not TerminalStatus.IDLE:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=timeout)


async def _run_live_reachability(
    monkeypatch: pytest.MonkeyPatch, *, steal_slot: bool
) -> tuple[status_module.StatusMonitor, MagicMock, list[float], int]:
    loop = asyncio.get_running_loop()
    provider = _provider()
    monitor = status_module.StatusMonitor()
    monitor._loop = loop
    monitor._buffers[provider.terminal_id] = _abort_buffer()
    monitor._chunk_seq[provider.terminal_id] = 1
    monitor._last_status[provider.terminal_id] = TerminalStatus.ERROR
    bus = EventBus()
    bus.set_loop(loop)
    inbox = inbox_module.InboxService()
    inbox.deliver_pending = MagicMock()
    retry_times: list[float] = []
    detect_count = 0

    original_schedule = monitor.schedule_detection_retry

    def schedule(*args, **kwargs) -> None:
        retry_times.append(loop.time())
        original_schedule(*args, **kwargs)

    original_get_status = provider.get_status

    def get_status(output: str) -> TerminalStatus:
        nonlocal detect_count
        detect_count += 1
        return original_get_status(output)

    monkeypatch.setattr(provider, "get_status", get_status)
    monkeypatch.setattr(status_module.provider_manager, "get_provider", lambda _tid: provider)
    monkeypatch.setattr(status_module.status_monitor, "schedule_detection_retry", schedule)
    monkeypatch.setattr(status_module, "bus", bus)
    monkeypatch.setattr(inbox_module, "bus", bus)

    inbox_task = asyncio.create_task(inbox.run())
    await asyncio.sleep(0)
    started = loop.time()
    with patch(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.record_published_status"
    ):
        assert provider.get_status(monitor._buffers[provider.terminal_id]) is TerminalStatus.ERROR
        if steal_slot:
            await asyncio.sleep(0.5)
            monitor._process_chunk(provider.terminal_id, "\nlate output chunk")
            assert len(monitor._quiesce_handle) <= 1
        await _wait_until_idle(monitor, timeout=_ABORT_REPORT_HOLD_S + 0.9)
        await asyncio.sleep(0.05)
    elapsed = loop.time() - started

    inbox_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await inbox_task
    bus.set_loop(None)
    assert inbox.deliver_pending.call_args_list == [((provider.terminal_id,), {"registry": None})]
    assert elapsed <= _ABORT_REPORT_HOLD_S + 1.0
    assert len(monitor._quiesce_handle) <= 1
    return monitor, inbox.deliver_pending, retry_times, detect_count


@pytest.mark.asyncio
async def test_silent_abort_reask_leaves_error_and_wakes_pending_delivery(monkeypatch) -> None:
    monitor, _deliver, retries, detections = await _run_live_reachability(
        monkeypatch, steal_slot=False
    )

    assert monitor._last_status["f582-live"] is TerminalStatus.IDLE
    assert retries
    assert len(retries) <= detections


@pytest.mark.asyncio
async def test_chunk_slot_theft_still_rearms_until_idle_and_delivery(monkeypatch) -> None:
    monitor, _deliver, retries, detections = await _run_live_reachability(
        monkeypatch, steal_slot=True
    )

    assert monitor._last_status["f582-live"] is TerminalStatus.IDLE
    assert len(retries) >= 2
    assert len(retries) <= detections
