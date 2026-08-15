"""SimWorld — the synchronous Hypothesis integration seam (D20).

Exposes: SimWorld(seed), inject(fault), heal_all(), step(), undelivered().
Every method is synchronous and owns its own loop.run_until_complete internally,
because Hypothesis stateful testing does not support asyncio (issue 3712).

The F203-batch state machine draws sim_seed as an integer strategy and prints
it on failure, so a Hypothesis counterexample is replayable as a plain seed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.sim.clock import SimClock, install as install_clock
from cli_agent_orchestrator.sim.driver import EventTrace, SimDriver
from cli_agent_orchestrator.sim.faults import Fault, FaultKind, FaultSet
from cli_agent_orchestrator.sim.rng import SimRNG, install_rng, uninstall_rng

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdicts (D16)
# ---------------------------------------------------------------------------


class LivenessVerdict:
    """Result of a liveness check."""

    PASS = "PASS"
    LIVENESS_TIMEOUT = "LIVENESS_TIMEOUT"
    LIVELOCK = "LIVELOCK"

    def __init__(self, verdict: str, details: str = "", seed: int = 0) -> None:
        self.verdict = verdict
        self.details = details
        self.seed = seed

    def __repr__(self) -> str:
        return f"LivenessVerdict({self.verdict}, seed={self.seed}, details={self.details!r})"

    @property
    def passed(self) -> bool:
        return self.verdict == self.PASS


class SimWorld:
    """Synchronous simulation world for the delivery subsystem.

    D20 contract: inject/heal_all/step/undelivered. All synchronous.
    The sim drives the real production services with injected clock/RNG/stubs.
    """

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._clock = SimClock(initial_monotonic=1000.0)  # non-zero start
        self._rng = SimRNG(seed)
        self._fault_set = FaultSet()
        self._trace = EventTrace()
        self._driver: SimDriver | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stubs: list[object] = []  # mock context managers
        self._clock_ctx: object = None
        self._installed = False

        # Obligation tracking (lightweight — the real state is in the DB stubs)
        self._obligations: list[dict[str, object]] = []
        self._delivered: set[int] = set()

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def clock(self) -> SimClock:
        return self._clock

    @property
    def trace(self) -> EventTrace:
        return self._trace

    @property
    def fault_set(self) -> FaultSet:
        return self._fault_set

    @property
    def driver(self) -> SimDriver | None:
        return self._driver

    def install(self) -> "SimWorld":
        """Install sim bindings (clock, RNG, stubs). Call before step/inject."""
        if self._installed:
            raise RuntimeError("SimWorld already installed")
        self._installed = True
        install_rng(self._rng)
        return self

    def uninstall(self) -> None:
        """Uninstall sim bindings. Call when done."""
        if not self._installed:
            return
        uninstall_rng(self._rng)
        self._installed = False

    def inject(self, fault: Fault) -> None:
        """Inject a fault into the simulation (D15 phase 1 only)."""
        now = self._clock.monotonic()
        self._fault_set.inject(fault, now)
        self._trace.record(
            "fault_inject",
            kind=fault.kind.name,
            terminal_id=fault.target_terminal_id,
            at=now,
        )

    def heal_all(self) -> None:
        """Heal all faults and transition to REQUIRE_PROGRESS (D15 phase 2→3)."""
        now = self._clock.monotonic()
        self._fault_set.heal_all(now)
        self._fault_set.set_phase("REQUIRE_PROGRESS")
        self._trace.record("heal_all", at=now)

    def step(self) -> bool:
        """Advance to next deadline, run one driver iteration. Synchronous (D20).

        Returns True if progress was made.
        """
        if self._driver is None:
            raise RuntimeError("SimWorld.step() called before setup_driver()")
        return self._driver.step()

    def setup_driver(self, watchdog: object) -> None:
        """Bind the driver to a (possibly stubbed) watchdog instance."""
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            StalledCallbackWatchdog,
        )

        self._driver = SimDriver(
            clock=self._clock,
            watchdog=watchdog,  # type: ignore[arg-type]
            fault_set=self._fault_set,
            trace=self._trace,
        )

    def add_obligation(
        self,
        inbox_row_id: int,
        terminal_id: str,
        mailbox_id: str = "mbox-1",
    ) -> None:
        """Track an obligation for liveness checking."""
        self._obligations.append({
            "inbox_row_id": inbox_row_id,
            "terminal_id": terminal_id,
            "mailbox_id": mailbox_id,
            "state": "OPEN",
        })

    def mark_delivered(self, inbox_row_id: int) -> None:
        """Mark an obligation as delivered."""
        self._delivered.add(inbox_row_id)
        for obl in self._obligations:
            if obl["inbox_row_id"] == inbox_row_id:
                obl["state"] = "DELIVERED"

    def undelivered(self) -> list[dict[str, object]]:
        """Return obligations not yet DELIVERED/ACKED."""
        return [
            obl for obl in self._obligations
            if obl["inbox_row_id"] not in self._delivered
            and obl.get("state") not in ("DELIVERED", "ACKED")
        ]

    def check_liveness(self, bound_seconds: float | None = None) -> LivenessVerdict:
        """Run the D15 phase 3 liveness check.

        D17: Bound B = 2 * escalate_after_s + 4 * tick_s (from config).
        D16: Two verdicts — LIVENESS_TIMEOUT and LIVELOCK.
        """
        from cli_agent_orchestrator.services.config_service import ConfigService

        escalate_after_s = float(ConfigService.get("delivery.escalate_after_s", 120.0))
        tick_s = float(ConfigService.get("delivery.tick_s", 5.0))

        if bound_seconds is None:
            bound_seconds = 2 * escalate_after_s + 4 * tick_s  # D17

        if self._driver is None:
            raise RuntimeError("check_liveness called before setup_driver()")

        start = self._clock.monotonic()
        bound_end = start + bound_seconds
        max_iterations = int(bound_seconds / tick_s) + 100  # generous cap
        no_progress_count = 0

        for _ in range(max_iterations):
            if not self.undelivered():
                return LivenessVerdict(LivenessVerdict.PASS, seed=self._seed)

            if self._clock.monotonic() >= bound_end:
                undelivered_ids = [o["inbox_row_id"] for o in self.undelivered()]
                return LivenessVerdict(
                    LivenessVerdict.LIVENESS_TIMEOUT,
                    details=f"undelivered={undelivered_ids}",
                    seed=self._seed,
                )

            made_progress = self.step()
            if not made_progress:
                no_progress_count += 1
                if no_progress_count > 3:
                    undelivered_ids = [o["inbox_row_id"] for o in self.undelivered()]
                    return LivenessVerdict(
                        LivenessVerdict.LIVELOCK,
                        details=f"no_deadline_progress undelivered={undelivered_ids}",
                        seed=self._seed,
                    )
            else:
                no_progress_count = 0

        # If we exhausted iterations without resolving
        if self.undelivered():
            undelivered_ids = [o["inbox_row_id"] for o in self.undelivered()]
            return LivenessVerdict(
                LivenessVerdict.LIVENESS_TIMEOUT,
                details=f"iteration_cap undelivered={undelivered_ids}",
                seed=self._seed,
            )
        return LivenessVerdict(LivenessVerdict.PASS, seed=self._seed)
