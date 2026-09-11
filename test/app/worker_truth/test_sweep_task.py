"""The sweep, driven by its task rather than by a test calling it (2b).

``Projector.sweep`` had unit tests from phase 1 and no driver at all, which is
the shape a feature has when it is written and then forgotten: every test green,
nothing running.  This is the end-to-end of the cadence — a real projector, a
real ``ProjectorSweep``, and the one outcome that has no other producer.

The task's sleep is injected, so this neither sleeps through ``PANE_HEARTBEAT_S``
nor races a spin loop against thread dispatch: the sweeper ends the task at a
tick boundary and the assertion is about what the projection holds afterwards.
"""

from __future__ import annotations

from test.app.conftest import Rig

import pytest

from cli_agent_orchestrator.app.worker_truth.sweep import ProjectorSweep
from cli_agent_orchestrator.core.events import EventKind
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S


class _TwoTicks:
    """Grants one sleep, then ends the loop on the next — exactly one pass."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> bool:
        self.sleeps.append(seconds)
        return len(self.sleeps) >= 2


@pytest.mark.asyncio
async def test_the_task_degrades_a_silent_terminal(rig: Rig) -> None:
    """Silence is not an event, so only the sweep can ever notice it."""
    rig.emit("term-s1", EventKind.TURN_STARTED)
    rig.clock.advance(NO_SIGNAL_S + 1)

    task = ProjectorSweep(rig.projector, sleeper=_TwoTicks())
    await task.start()
    await task.stop()

    assert rig.state_of("term-s1") is WorkerState.DEGRADED
    assert rig.states.get("term-s1").degraded_reason is DegradedReason.NO_SIGNAL
