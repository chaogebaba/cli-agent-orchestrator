"""Deterministic simulation clock (D2/D3/D7).

Provides:
- SimClock: a deterministic clock with controllable monotonic and wall-clock time.
- install(): a strict context manager that binds SimClock and asserts restore on exit.

The default binding is the stdlib (time.monotonic / datetime.now(tz=utc)); the sim
driver installs a SimClock for the duration of a scenario, and the install() context
manager guarantees restoration even on exception (D7 — xdist worker isolation).
"""

from __future__ import annotations

import contextlib
import threading
from datetime import datetime, timedelta, timezone
from typing import Generator


class SimClock:
    """A controllable clock for deterministic simulation.

    Constructed with an initial virtual monotonic time and wall-clock instant.
    Advancing the clock is explicit: call advance() or set_monotonic() / set_wall().
    """

    def __init__(
        self,
        *,
        initial_monotonic: float = 0.0,
        initial_wall: datetime | None = None,
    ) -> None:
        self._monotonic = initial_monotonic
        self._wall = initial_wall or datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._monotonic

    def utcnow(self) -> datetime:
        with self._lock:
            return self._wall

    def advance(self, seconds: float) -> None:
        """Advance both monotonic and wall-clock by the given seconds."""
        with self._lock:
            self._monotonic += seconds
            self._wall += timedelta(seconds=seconds)

    def set_monotonic(self, value: float) -> None:
        with self._lock:
            delta = value - self._monotonic
            self._monotonic = value
            self._wall += timedelta(seconds=delta)

    def jump_to(self, monotonic: float) -> None:
        """Jump to a specific monotonic time, adjusting wall-clock proportionally."""
        self.set_monotonic(monotonic)


# ---------------------------------------------------------------------------
# Global binding (D7: strict context manager with restore assert)
# ---------------------------------------------------------------------------

_active_clock: SimClock | None = None
_install_lock = threading.Lock()


def active() -> SimClock | None:
    """Return the currently installed SimClock, or None if in production mode."""
    return _active_clock


@contextlib.contextmanager
def install(clock: SimClock) -> Generator[SimClock, None, None]:
    """Install a SimClock for the duration of a with-block.

    On exit, asserts the production binding is restored. Nested installs are
    forbidden (raises RuntimeError). This is the ONLY way to bind a sim clock;
    there is no env var or global "sim mode" flag (D7).
    """
    global _active_clock
    with _install_lock:
        if _active_clock is not None:
            raise RuntimeError(
                "SimClock already installed — nested installs are forbidden (D7). "
                "Did a previous test leak its clock?"
            )
        _active_clock = clock
    try:
        yield clock
    finally:
        with _install_lock:
            if _active_clock is not clock:
                raise AssertionError(
                    "SimClock restore assertion failed: the active clock was replaced "
                    "by something other than the original during this install() block."
                )
            _active_clock = None
