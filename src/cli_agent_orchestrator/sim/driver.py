"""Deterministic tick driver for the DST liveness harness (D10/D13).

The sim is single-threaded and drives the tick functions directly. The harness
never starts StalledCallbackWatchdog.run(); it substitutes the run loop and
calls the ticks inline, in a seeded order.

Virtual time advances by jump-to-next-deadline (D13), not fixed increment.
Wall-clock cost of a 10-minute virtual scenario stays milliseconds.

F254 D12: The tick roster is data (a list of Tick tuples), not hardcoded branches.
The current tick roster is `DELIVERY_TICK_NAMES`, a module constant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, List, NamedTuple

from cli_agent_orchestrator.sim.clock import SimClock
from cli_agent_orchestrator.sim.faults import FaultSet

if TYPE_CHECKING:
    from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# D12: Tick is a named callable. The roster is configuration, not a switch.
# ---------------------------------------------------------------------------


class Tick(NamedTuple):
    """A single tick in the driver's roster."""

    name: str
    fn: Callable[[float], None]


@dataclass
class EventTrace:
    """Records simulation events for replay verification (AC3)."""

    events: list[dict[str, object]] = field(default_factory=list)

    def record(self, event_type: str, **kwargs: object) -> None:
        self.events.append({"type": event_type, **kwargs})


def _build_delivery_ticks(watchdog: "StalledCallbackWatchdog") -> List[Tick]:
    """Build the default delivery ticks from a watchdog instance.

    This is the DELIVERY_TICKS constant materialized for a specific watchdog.
    The tick order and set are identical to the pre-D12 hardcoded body.

    NOTE: the delivery tick is resolved at CALL TIME (not build time) to support
    test patching — the original _run_ticks also imported per-call.

    WP-ARCH 3c K7: the first entry used to be the watchdog's
    ``_fx191_convergence_tick``, a throttled call into
    ``delivery_service.convergence_tick``. Both are deleted with the obligation
    ladder, so the roster is re-pointed at the real scheduled observer,
    ``DeliveryTick.run_once``. A simulation whose first tick drove a function
    that returned immediately at the shipped position was simulating the wrong
    system; this makes the sim's delivery step the one production runs.
    """

    def _delivery(now: float) -> None:
        # The composition root holds the built tick (``bootstrap.py`` keeps it on
        # the runtime precisely so a caller can drive ``run_once`` directly), and
        # it is absent for a position that is not served — in which case there is
        # no delivery step to simulate and this is correctly a no-op.
        from cli_agent_orchestrator import bootstrap

        runtime = bootstrap.current_runtime()
        tick = runtime.delivery_tick if runtime is not None else None
        if tick is not None:
            tick.run_once()

    def _poll_unarmed(now: float) -> None:
        watchdog.poll_unarmed_statuses(now=now)

    def _refresh_screen(now: float) -> None:
        watchdog.refresh_screen_fingerprints(now=now)

    # WP-ARCH 3c K4: four more entries went here — ``notify_due``,
    # ``tick_waiting_inbox``, ``tick_ready_backlog`` and ``tick_quiescence``,
    # plus ``tick_no_progress`` and ``tick_wedge`` before them. All six were the
    # watchdog's notifier half, deleted with it. What the roster drives now is
    # exactly what the server schedules: the delivery tick, and the two liveness
    # ticks the watchdog keeps.
    return [
        Tick("delivery_tick", _delivery),
        Tick("poll_unarmed_statuses", _poll_unarmed),
        Tick("refresh_screen_fingerprints", _refresh_screen),
    ]


class SimDriver:
    """Drives the convergence ticks in deterministic order under simulated time.

    D10: The roster is the full run-loop fan-out.
    D12: The roster is data (ticks parameter), not a hardcoded body.
    D13: Time advances by jump-to-next-deadline.
    """

    def __init__(
        self,
        clock: SimClock,
        watchdog: "StalledCallbackWatchdog",
        fault_set: FaultSet,
        trace: EventTrace | None = None,
        auto_tick: bool = True,
        ticks: List[Tick] | None = None,
    ) -> None:
        self.clock = clock
        self.watchdog = watchdog
        self.fault_set = fault_set
        self.trace = trace or EventTrace()
        self._tick_cadence: float = 5.0  # delivery.tick_s default
        self._escalate_after_s: float = 120.0
        self._iteration_count = 0
        self._deadlines: list[float] = []
        self._auto_tick = auto_tick
        # D12: ticks roster — defaults to DELIVERY_TICKS when None
        self._ticks: List[Tick] = ticks if ticks is not None else _build_delivery_ticks(watchdog)

    @property
    def iteration_count(self) -> int:
        return self._iteration_count

    def configure(self, tick_s: float = 5.0, escalate_after_s: float = 120.0) -> None:
        """Set timing configuration (reads from ConfigService in production)."""
        self._tick_cadence = tick_s
        self._escalate_after_s = escalate_after_s

    def add_deadline(self, at: float) -> None:
        """Register an external deadline (fault expiry, workload arrival, etc.)."""
        if at not in self._deadlines:
            self._deadlines.append(at)
            self._deadlines.sort()

    def _collect_next_deadline(self) -> float | None:
        """Find the earliest pending deadline across all sources.

        Sources: tick cadence (when enabled), registered external deadlines.
        Returns None when no deadlines exist — the livelock detection shape (D16).
        """
        now = self.clock.monotonic()
        candidates: list[float] = []

        # Next tick cadence (only if auto-tick is enabled)
        if self._auto_tick:
            next_tick = now + self._tick_cadence
            candidates.append(next_tick)

        # External deadlines that are in the future
        for d in self._deadlines:
            if d > now:
                candidates.append(d)
                break  # sorted, so first future one is earliest

        return min(candidates) if candidates else None

    def step(self) -> bool:
        """Advance to next deadline and run one driver iteration.

        Returns True if progress was made (time advanced or ticks ran).
        Returns False if there are no pending deadlines (potential livelock).
        """
        next_deadline = self._collect_next_deadline()
        if next_deadline is None:
            return False

        # Jump time to deadline (D13)
        current = self.clock.monotonic()
        if next_deadline > current:
            self.clock.jump_to(next_deadline)
            self.trace.record(
                "time_advance",
                from_t=current,
                to_t=next_deadline,
                delta=next_deadline - current,
            )

        # Remove consumed deadlines
        self._deadlines = [d for d in self._deadlines if d > self.clock.monotonic()]

        # Run the ticks in roster order (D12)
        now = self.clock.monotonic()
        self._run_ticks(now)
        self._iteration_count += 1
        return True

    def _run_ticks(self, now: float) -> None:
        """Run all ticks from the roster (D12: data-driven, not hardcoded)."""
        self.trace.record("tick_iteration", now=now, iteration=self._iteration_count)

        for tick in self._ticks:
            try:
                tick.fn(now)
                self.trace.record("tick", name=tick.name, now=now)
            except Exception as e:
                self.trace.record("tick_error", name=tick.name, error=str(e))
                logger.debug("tick %s error in sim", tick.name, exc_info=True)

    def run_until(
        self,
        *,
        max_virtual_seconds: float,
        max_iterations: int = 10000,
    ) -> None:
        """Run the driver until a time bound or iteration cap is reached."""
        start = self.clock.monotonic()
        bound = start + max_virtual_seconds
        iterations = 0

        while self.clock.monotonic() < bound and iterations < max_iterations:
            if not self.step():
                # No deadline available — add the tick cadence as a fallback
                self.add_deadline(self.clock.monotonic() + self._tick_cadence)
            iterations += 1


# ---------------------------------------------------------------------------
# D12: Module-level constant for the default tick names (documentation only).
# The actual Tick objects are built per-driver via _build_delivery_ticks().
# ---------------------------------------------------------------------------

DELIVERY_TICK_NAMES = [
    "delivery_tick",
    "poll_unarmed_statuses",
    "refresh_screen_fingerprints",
]
