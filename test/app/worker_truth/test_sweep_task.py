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

import threading
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


def test_the_sweep_re_drives_the_condition_for_every_terminal(rig: Rig) -> None:
    """D8's second driver, at the projector seam.

    F611's only driver is a genuine status transition, so a label is never
    revisited on a terminal that has gone quiet — which is when a stale label
    sits on the fleet row longest.  The fold owns the label's VALUE; this pass
    owns its LIFETIME, and it therefore has to visit every terminal rather than
    only the ones this sweep degraded.
    """
    seen: list[str] = []
    rig.projector._reclassify = seen.append  # type: ignore[attr-defined]
    rig.emit("term-a", EventKind.TURN_STARTED)
    rig.emit("term-b", EventKind.TURN_STARTED)

    rig.projector.sweep()

    assert sorted(seen) == ["term-a", "term-b"]


def test_the_re_drive_runs_with_the_projector_lock_released(rig: Rig) -> None:
    """It reaches the legacy monitor and takes its lock, so it obeys the same
    ordering rule the publish does."""
    free: list[bool] = []

    def reclassify(terminal_id: str) -> None:
        taken: list[bool] = []

        def probe() -> None:
            acquired = rig.projector._lock.acquire(timeout=2.0)  # type: ignore[attr-defined]
            taken.append(acquired)
            if acquired:
                rig.projector._lock.release()  # type: ignore[attr-defined]

        worker = threading.Thread(target=probe, name="lock-probe")
        worker.start()
        worker.join(timeout=4.0)
        free.append(bool(taken and taken[0]))

    rig.projector._reclassify = reclassify  # type: ignore[attr-defined]
    rig.emit("term-a", EventKind.TURN_STARTED)

    rig.projector.sweep()

    assert free == [True]
