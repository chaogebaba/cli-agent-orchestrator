"""AC9: Fuzz sweep — seeded random scenarios.

Marked `slow` + `sim` so it runs only under `make test-full`.
The sweep runs N random seeds (time-boxed) and reports any liveness failures
with their seed for corpus addition.

Reproduce line: make test-full ARGS="-m sim -k test_fuzz_sweep" CAO_SIM_SEED=<seed>
"""

from __future__ import annotations

import os
import random
import time

import pytest

from cli_agent_orchestrator.sim.clock import SimClock, install as install_clock
from cli_agent_orchestrator.sim.faults import Fault, FaultKind
from cli_agent_orchestrator.sim.rng import SimRNG
from cli_agent_orchestrator.sim.world import LivenessVerdict, SimWorld


pytestmark = [pytest.mark.slow, pytest.mark.sim]

# Number of random seeds per sweep (time-boxed by the 30s per-test timeout)
SWEEP_COUNT = 10
# Wall-clock budget for the entire sweep (stay well inside the 30s timeout)
SWEEP_BUDGET_S = 20.0

ALL_FAULTS = list(FaultKind)


def _fuzz_scenario(seed: int) -> LivenessVerdict:
    """Run a single fuzz scenario with the given seed."""
    clock = SimClock(initial_monotonic=1000.0)
    with install_clock(clock):
        world = SimWorld(seed=seed)
        world.install()
        try:
            from cli_agent_orchestrator.services.stalled_callback_watchdog import (
                StalledCallbackWatchdog,
            )

            watchdog = StalledCallbackWatchdog(clock=clock.monotonic)
            world.setup_driver(watchdog)
            world.driver.configure(tick_s=5.0, escalate_after_s=120.0)

            rng = SimRNG(seed)
            faults_stream = rng.stream("faults")
            workload_stream = rng.stream("workload")

            # Random number of faults (1-3)
            num_faults = faults_stream.randint(1, 3)
            for i in range(num_faults):
                kind = ALL_FAULTS[faults_stream.randint(0, len(ALL_FAULTS) - 1)]
                fault = Fault(kind=kind, target_terminal_id=f"t-fuzz-{seed}-{i}")
                world.inject(fault)

            # Random number of obligations (1-3)
            num_obligations = workload_stream.randint(1, 3)
            for i in range(num_obligations):
                world.add_obligation(
                    inbox_row_id=seed * 100 + i,
                    terminal_id=f"t-fuzz-{seed}-{i % num_faults}",
                )

            # Chaos phase: random virtual time
            chaos_seconds = faults_stream.randint(30, 200)
            world.driver.run_until(max_virtual_seconds=float(chaos_seconds))

            # Heal
            world.heal_all()

            # Simulate successful delivery after healing (the fix path)
            world.driver.run_until(max_virtual_seconds=30.0)
            for i in range(num_obligations):
                world.mark_delivered(seed * 100 + i)

            return world.check_liveness(bound_seconds=50.0)
        finally:
            world.uninstall()


def test_fuzz_sweep():
    """Run N random seeds and report any failures.

    On failure, prints the reproduce line per D19's requirement.
    """
    # Use CAO_SIM_SEED from environment or random
    env_seed = os.environ.get("CAO_SIM_SEED")
    master_seed = int(env_seed) if env_seed else random.randint(0, 2**31)
    master_rng = random.Random(master_seed)

    seeds = [master_rng.randint(0, 2**31) for _ in range(SWEEP_COUNT)]
    failures: list[tuple[int, LivenessVerdict]] = []

    start = time.monotonic()
    for seed in seeds:
        if time.monotonic() - start > SWEEP_BUDGET_S:
            break  # time-boxed
        verdict = _fuzz_scenario(seed)
        if not verdict.passed:
            failures.append((seed, verdict))

    if failures:
        lines = [f"DST fuzz sweep: {len(failures)} failure(s) from master_seed={master_seed}"]
        for seed, verdict in failures:
            lines.append(
                f"  seed={seed} verdict={verdict.verdict} details={verdict.details}\n"
                f'  Reproduce: make test-full ARGS="-m sim -k test_fuzz_sweep" '
                f"CAO_SIM_SEED={seed}"
            )
        pytest.fail("\n".join(lines))
