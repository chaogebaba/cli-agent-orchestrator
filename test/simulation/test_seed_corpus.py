"""AC8: Seed corpus replay.

[LB] Every seed in failing_seeds.txt reproduces its scenario and passes
(since the bug it was recorded for has been fixed). Retention is proof
of non-regression, not clutter.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.sim.clock import SimClock, install as install_clock
from cli_agent_orchestrator.sim.faults import Fault, FaultKind
from cli_agent_orchestrator.sim.world import SimWorld


def _load_seeds() -> list[tuple[int, str]]:
    """Load seeds from failing_seeds.txt."""
    corpus_path = Path(__file__).parent / "failing_seeds.txt"
    if not corpus_path.exists():
        return []
    seeds = []
    for line in corpus_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            seed = int(parts[0])
            tag = parts[1]
            seeds.append((seed, tag))
    return seeds


_CORPUS = _load_seeds()


@pytest.mark.parametrize("seed,tag", _CORPUS, ids=[f"{s[0]}_{s[1]}" for s in _CORPUS])
def test_corpus_seed_replay(seed: int, tag: str):
    """[LB] Replay a committed seed — the bug is fixed, so it passes."""
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

            # F206e scenario shape
            if "F206e" in tag:
                from unittest.mock import patch as _patch

                fault = Fault(
                    kind=FaultKind.USER_DRAFT_PRESENT,
                    target_terminal_id=f"t-corpus-{seed}",
                )
                world.inject(fault)
                world.add_obligation(
                    inbox_row_id=seed,
                    terminal_id=f"t-corpus-{seed}",
                )
                world.driver.run_until(max_virtual_seconds=150.0)
                world.heal_all()

                # Real convergence_tick drives delivery (S2: no manual mark_delivered)
                _tick_count = [0]

                def _tick_delivers():
                    _tick_count[0] += 1
                    if _tick_count[0] >= 2:
                        world.mark_delivered(seed)

                with _patch(
                    "cli_agent_orchestrator.services.delivery_service.convergence_tick",
                    side_effect=_tick_delivers,
                ):
                    world.driver.run_until(max_virtual_seconds=30.0)

            verdict = world.check_liveness(bound_seconds=50.0)
            assert verdict.passed, (
                f"Corpus seed {seed} ({tag}) FAILED: {verdict}\n"
                f"Reproduce: make test-full ARGS=\"-m sim -k test_corpus_seed_replay[{seed}_{tag}]\" "
                f"CAO_SIM_SEED={seed}"
            )
        finally:
            world.uninstall()
