"""The projector's periodic sweep, as a task (WP-ARCH phase 2, r8 N5).

A projector only ever runs when something arrives, so it cannot by itself observe
that nothing has.  ``Projector.sweep`` is the answer to that — it is the sole
producer of ``degraded(no_signal)`` and, since phase 2's D1e, the only thing that
can lower a standing "this terminal is projected" mark — and until this module it
had no driver at all: phase 1 wrote the method and left the cadence to phase 2.

The shape is ``RetentionTask``'s, deliberately, down to sleeping FIRST: a server
that crash-loops must not turn into a sweep loop against the live database, and a
sweep at the instant of boot would judge every terminal silent before its first
probe has had a chance to land.

One sweep per ``PANE_HEARTBEAT_S``, which is the cadence ``timing.py`` documents
for it and the same tick the liveness probe runs on.  The two are separate tasks
on purpose: the probe writes what it saw and the sweep judges what it did not,
and coupling them would mean a backend with no pane listing also lost its silence
detection — which is the one thing that still works when a backend goes dark.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from cli_agent_orchestrator.core.timing import PANE_HEARTBEAT_S

logger = logging.getLogger(__name__)

__all__ = ["ProjectorSweep", "Sweeper"]


class Sweeper(Protocol):
    """What this task drives: one method, returning whatever it likes.

    Typed as a Protocol rather than as ``Projector`` so the task can be driven by
    a double in a test without building an event store, and so that nothing here
    depends on the projector's outcome vocabulary — the return is discarded, and
    a task that read it would be making decisions the projector already made.
    """

    def sweep(self) -> object: ...


class ProjectorSweep:
    """Runs :meth:`Sweeper.sweep` every ``PANE_HEARTBEAT_S``."""

    def __init__(
        self,
        projector: Sweeper,
        sleeper: Callable[[float], Awaitable[bool]] | None = None,
    ) -> None:
        self._projector = projector
        # The cadence, injectable.  A test that wants to assert WHEN this sweeps
        # must not have to sleep through ``PANE_HEARTBEAT_S`` or monkeypatch the
        # constant to zero — the first is unrunnable and the second deletes the
        # very property under test, since a period of zero makes "sleeps first"
        # and "sweeps first" indistinguishable.
        self._sleeper = sleeper
        self._task: asyncio.Task[None] | None = None
        self._stopping: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the periodic sweep.  Idempotent."""
        if self.running:
            return
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="worker-truth-sweep")

    async def stop(self) -> None:
        """Ask the loop to finish and WAIT for the sweep that is in flight.

        Not a cancel, for the reason ``LivenessProbe.stop`` is not: a task parked
        on ``asyncio.to_thread`` raises in the coroutine while the worker thread
        runs on, so cancelling would return here with a sweep still writing to
        the projection after shutdown said it had stopped.
        """
        stopping = self._stopping
        if stopping is not None:
            stopping.set()
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, or wake early to stop.  True when the loop should exit."""
        if self._sleeper is not None:
            return await self._sleeper(seconds)
        stopping = self._stopping
        if stopping is None:  # pragma: no cover - start() always sets it
            await asyncio.sleep(seconds)
            return False
        try:
            await asyncio.wait_for(stopping.wait(), timeout=seconds)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return True

    async def _run(self) -> None:
        while True:
            if await self._sleep(PANE_HEARTBEAT_S):
                return
            try:
                # Off the loop: the sweep reads every projection row and the
                # event log behind each one, against SQLite.
                await asyncio.to_thread(self._projector.sweep)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad sweep must not end the sweep
                logger.warning("worker-truth projection sweep failed; continuing", exc_info=True)
