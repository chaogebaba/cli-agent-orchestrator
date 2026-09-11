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

    def __init__(self, projector: Sweeper) -> None:
        self._projector = projector
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the periodic sweep.  Idempotent."""
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="worker-truth-sweep")

    async def stop(self) -> None:
        """Cancel the sweep and wait for it to unwind."""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(PANE_HEARTBEAT_S)
            except asyncio.CancelledError:
                raise
            try:
                # Off the loop: the sweep reads every projection row and the
                # event log behind each one, against SQLite.
                await asyncio.to_thread(self._projector.sweep)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad sweep must not end the sweep
                logger.warning("worker-truth projection sweep failed; continuing", exc_info=True)
