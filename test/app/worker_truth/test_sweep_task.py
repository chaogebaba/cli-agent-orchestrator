"""The sweep, driven by its task rather than by a test calling it (2b).

``Projector.sweep`` had unit tests from phase 1 and no driver at all, which is
the shape a feature has when it is written and then forgotten: every test green,
nothing running.  This is the end-to-end of the cadence — a real projector, a
real ``ProjectorSweep``, and the one outcome that has no other producer.
"""

from __future__ import annotations

import asyncio
from test.app.conftest import Rig

import pytest

from cli_agent_orchestrator.app.worker_truth import sweep as sweep_module
from cli_agent_orchestrator.app.worker_truth.sweep import ProjectorSweep
from cli_agent_orchestrator.core.events import EventKind
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S


@pytest.mark.asyncio
async def test_the_task_degrades_a_silent_terminal(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence is not an event, so only the sweep can ever notice it."""
    monkeypatch.setattr(sweep_module, "PANE_HEARTBEAT_S", 0)
    rig.emit("term-s1", EventKind.TURN_STARTED)
    rig.clock.advance(NO_SIGNAL_S + 1)

    task = ProjectorSweep(rig.projector)
    await task.start()
    for _ in range(100):
        await asyncio.sleep(0)
        if rig.state_of("term-s1") is WorkerState.DEGRADED:
            break
    await task.stop()

    assert rig.state_of("term-s1") is WorkerState.DEGRADED
    assert rig.states.get("term-s1").degraded_reason is DegradedReason.NO_SIGNAL
