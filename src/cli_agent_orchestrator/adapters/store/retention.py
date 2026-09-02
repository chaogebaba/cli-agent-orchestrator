"""Event retention sweep (WP-ARCH phase 1, AC3).

An asyncio task in the one process (U7), started by ``bootstrap.py`` ONLY when
``CAO_WORKER_TRUTH_INGEST=1``.  That gating is AC5's "no behaviour change" made
mechanical rather than asserted: with the switch off this task does not exist, so
it cannot write, cannot checkpoint, and cannot contend for the single writer.

The sweep is deliberately dumb — one ``DELETE`` and one passive WAL checkpoint
per day.  Failures are logged and the loop continues: a maintenance sweep that
can kill its own task would silently stop pruning, and the first symptom would
be a database that grew for a month.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from cli_agent_orchestrator.core.ports import Clock, EventStore
from cli_agent_orchestrator.core.timing import RETENTION_DAYS, RETENTION_SWEEP_S

logger = logging.getLogger(__name__)

__all__ = ["RetentionTask"]


class RetentionTask:
    """Prunes events older than ``RETENTION_DAYS``, once per ``RETENTION_SWEEP_S``."""

    def __init__(self, store: EventStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def sweep_once(self) -> int:
        """Run one prune and return the number of rows deleted."""
        horizon = self._clock.now() - timedelta(days=RETENTION_DAYS)
        return self._store.prune(horizon)

    async def start(self) -> None:
        """Start the periodic sweep.  Idempotent."""
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="worker-truth-retention")

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
        # Sleeps FIRST: a server restart loop must never turn into a delete loop
        # on the live database.
        while True:
            try:
                await asyncio.sleep(RETENTION_SWEEP_S)
            except asyncio.CancelledError:
                raise
            try:
                deleted = await asyncio.to_thread(self.sweep_once)
                if deleted:
                    logger.info("worker-truth retention pruned %d event(s)", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a sweep failure must not end the sweep
                logger.warning("worker-truth retention sweep failed; continuing", exc_info=True)
