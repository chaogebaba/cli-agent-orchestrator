"""Autouse guard: no clock/RNG binding leaks out of any test in this directory.

D7: The production binding must be restored on exit. This conftest asserts
that at the end of every test in test/simulation/, the sim clock and RNG
are uninstalled.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.sim.clock import active as clock_active
from cli_agent_orchestrator.sim.rng import active as rng_active


@pytest.fixture(autouse=True)
def _sim_leak_guard():
    """Assert no sim bindings leak across tests (D7 enforcement)."""
    # Pre-check: should not be installed
    assert clock_active() is None, "SimClock was leaked from a previous test"
    assert rng_active() is None, "SimRNG was leaked from a previous test"

    yield

    # Post-check: must be clean after test
    leaked_clock = clock_active()
    leaked_rng = rng_active()
    if leaked_clock is not None or leaked_rng is not None:
        # Force cleanup to not poison the worker
        import cli_agent_orchestrator.sim.clock as _clk
        import cli_agent_orchestrator.sim.rng as _rng

        _clk._active_clock = None
        _rng._active_rng = None
        parts = []
        if leaked_clock is not None:
            parts.append("SimClock")
        if leaked_rng is not None:
            parts.append("SimRNG")
        pytest.fail(
            f"Sim binding leak detected: {', '.join(parts)} still installed after test. "
            "Wrap sim usage in a context manager (D7)."
        )
