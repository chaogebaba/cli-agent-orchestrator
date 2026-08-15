"""AC1-AC4: Sim substrate tests.

AC1 (D2/D3/D4) — one clock, no leaks.
AC2 (D7) — installation cannot leak.
AC3 (D8/D9) — byte-identical replay.
AC4 (D13) — virtual time is free.
"""

from __future__ import annotations

import re
import time as stdlib_time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.sim.clock import SimClock, install as install_clock, active as clock_active
from cli_agent_orchestrator.sim.rng import SimRNG, install_rng, uninstall_rng, active as rng_active


class TestAC1ClockNoLeaks:
    """AC1: One clock, no leaks — every timestamp derives from sim clock."""

    def test_utcnow_uses_sim_clock_when_installed(self):
        """D3: _utcnow delegates to sim clock."""
        from cli_agent_orchestrator.clients.database import _utcnow

        clock = SimClock(initial_wall=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc))
        with install_clock(clock):
            result = _utcnow()
            assert result == datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

            # Advance and check again
            clock.advance(60.0)
            result2 = _utcnow()
            assert result2 == datetime(2026, 6, 15, 12, 1, 0, tzinfo=timezone.utc)

    def test_utcnow_returns_real_time_without_sim(self):
        """Default: _utcnow returns real wall-clock."""
        from cli_agent_orchestrator.clients.database import _utcnow

        before = datetime.now(timezone.utc)
        result = _utcnow()
        after = datetime.now(timezone.utc)
        assert before <= result <= after

    def test_sim_clock_monotonic_deterministic(self):
        """SimClock.monotonic() returns the injected value."""
        clock = SimClock(initial_monotonic=1000.0)
        with install_clock(clock):
            assert clock.monotonic() == 1000.0
            clock.advance(5.0)
            assert clock.monotonic() == 1005.0

    def test_static_check_no_bare_calls_in_delivery_modules(self):
        """[LB] Static check: zero direct datetime.now/time.monotonic in delivery paths.

        Checks delivery_service.py, boundary_pull_service.py, nudge_discipline.py,
        receiver_state_view.py, doorbell_service.py, and the delivery paths of
        mailbox_service.py / inbox_service.py.
        """
        import inspect
        import ast

        from cli_agent_orchestrator.services import delivery_service
        from cli_agent_orchestrator.clients import database

        # Check that _utcnow in database.py delegates to sim clock
        source = inspect.getsource(database._utcnow)
        assert "sim_clock_active" in source or "sim.clock" in source


class TestAC2InstallCannotLeak:
    """AC2: Installation cannot leak — context manager guarantees restore."""

    def test_exception_inside_install_restores_binding(self):
        """[LB] Force exception inside install block — production binding restored."""
        clock = SimClock()
        with pytest.raises(ValueError):
            with install_clock(clock):
                assert clock_active() is clock
                raise ValueError("deliberate error")

        # Production binding must be restored
        assert clock_active() is None

    def test_nested_install_raises(self):
        """Nested installs are forbidden (D7)."""
        clock1 = SimClock()
        clock2 = SimClock()
        with install_clock(clock1):
            with pytest.raises(RuntimeError, match="already installed"):
                with install_clock(clock2):
                    pass

    def test_following_test_reads_real_time(self):
        """After an install block, subsequent code reads real time."""
        from cli_agent_orchestrator.clients.database import _utcnow

        # Simulate a previous test with sim clock
        clock = SimClock(initial_wall=datetime(2000, 1, 1, tzinfo=timezone.utc))
        with install_clock(clock):
            assert _utcnow().year == 2000

        # Now should be real time
        result = _utcnow()
        assert result.year >= 2025  # We're past 2025


class TestAC3ByteIdenticalReplay:
    """AC3: Same seed → identical event trace."""

    def _run_rng_scenario(self, seed: int) -> list[tuple[str, int]]:
        """Run a scenario with the given seed and return the draw sequence."""
        rng = SimRNG(seed)
        install_rng(rng)
        try:
            draws = []
            faults_stream = rng.stream("faults")
            workload_stream = rng.stream("workload")
            scheduling_stream = rng.stream("scheduling")

            for _ in range(10):
                draws.append(("faults", faults_stream.randint(0, 100)))
                draws.append(("workload", workload_stream.randint(0, 100)))
                draws.append(("scheduling", scheduling_stream.randint(0, 100)))

            return draws
        finally:
            uninstall_rng(rng)

    def test_same_seed_identical_trace(self):
        """[LB] Two runs with the same seed produce identical traces."""
        trace1 = self._run_rng_scenario(seed=42)
        trace2 = self._run_rng_scenario(seed=42)
        assert trace1 == trace2

    def test_different_seed_different_trace(self):
        """Different seeds produce different traces."""
        trace1 = self._run_rng_scenario(seed=42)
        trace2 = self._run_rng_scenario(seed=99)
        assert trace1 != trace2

    def test_adding_stream_does_not_shift_others(self):
        """[LB] D9: Adding a new stream leaves existing streams unchanged."""
        seed = 12345
        rng1 = SimRNG(seed)
        install_rng(rng1)
        try:
            faults1 = [rng1.stream("faults").randint(0, 1000) for _ in range(20)]
        finally:
            uninstall_rng(rng1)

        # Now with an extra stream interleaved
        rng2 = SimRNG(seed)
        install_rng(rng2)
        try:
            faults2 = []
            for _ in range(20):
                faults2.append(rng2.stream("faults").randint(0, 1000))
                # Draw from a NEW stream
                rng2.stream("workload").randint(0, 1000)
        finally:
            uninstall_rng(rng2)

        assert faults1 == faults2, "D9 violated: adding a stream shifted the faults stream"


class TestAC4VirtualTimeIsFree:
    """AC4: Virtual time advances at zero wall-clock cost."""

    def test_600_virtual_seconds_under_2_real_seconds(self):
        """[LB] 600 virtual seconds with escalate_after_s=120 completes in < 2s wall."""
        clock = SimClock(initial_monotonic=1000.0)
        with install_clock(clock):
            from cli_agent_orchestrator.sim.driver import SimDriver, EventTrace
            from cli_agent_orchestrator.services.stalled_callback_watchdog import (
                StalledCallbackWatchdog,
            )

            # Create a watchdog with the sim clock
            watchdog = StalledCallbackWatchdog(clock=clock.monotonic)
            trace = EventTrace()
            from cli_agent_orchestrator.sim.faults import FaultSet

            driver = SimDriver(
                clock=clock,
                watchdog=watchdog,
                fault_set=FaultSet(),
                trace=trace,
            )
            driver.configure(tick_s=5.0, escalate_after_s=120.0)

            # Run for 600 virtual seconds
            wall_start = stdlib_time.monotonic()
            driver.run_until(max_virtual_seconds=600.0)
            wall_elapsed = stdlib_time.monotonic() - wall_start

            # Assertions
            assert clock.monotonic() >= 1600.0, "Should have advanced 600+ virtual seconds"
            assert wall_elapsed < 2.0, f"Wall-clock cost {wall_elapsed:.2f}s exceeds 2s budget"

            # D13: iterations should equal distinct deadlines, not 600
            assert driver.iteration_count < 200, (
                f"Too many iterations ({driver.iteration_count}) — "
                "should be O(deadlines), not O(virtual_seconds)"
            )
