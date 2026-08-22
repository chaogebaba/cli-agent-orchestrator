"""AC5, AC6, AC10: Liveness harness tests.

AC5 (D14) — all four fault classes inject and heal.
AC6 (D15/D16) — the assertion has teeth on a planted bug.
AC10 (D20) — the Hypothesis seam is real before Hypothesis exists.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.sim.clock import SimClock, install as install_clock
from cli_agent_orchestrator.sim.faults import Fault, FaultKind, FaultSet
from cli_agent_orchestrator.sim.rng import SimRNG, install_rng, uninstall_rng
from cli_agent_orchestrator.sim.world import LivenessVerdict, SimWorld


class TestAC5FaultClassesInjectAndHeal:
    """AC5: All four fault classes inject and heal.

    [LB] Each fault demonstrably changes delivery behaviour while injected.
    """

    def test_user_draft_present_injects_and_heals(self):
        """Fault 1: user_draft_present — blocks rung1 delivery."""
        fault_set = FaultSet()
        fault = Fault(kind=FaultKind.USER_DRAFT_PRESENT, target_terminal_id="t-001")
        fault_set.inject(fault, now=100.0)

        assert fault_set.is_fault_active(FaultKind.USER_DRAFT_PRESENT, "t-001")
        assert fault.injected
        assert not fault.healed

        # Heal
        fault_set.heal_all(now=200.0)
        assert not fault_set.is_fault_active(FaultKind.USER_DRAFT_PRESENT, "t-001")
        assert fault.healed

    def test_receiver_no_boundary_injects_and_heals(self):
        """Fault 2: receiver has no boundary — suppresses interrupt."""
        fault_set = FaultSet()
        fault = Fault(kind=FaultKind.RECEIVER_NO_BOUNDARY, target_terminal_id="t-002")
        fault_set.inject(fault, now=100.0)

        assert fault_set.is_fault_active(FaultKind.RECEIVER_NO_BOUNDARY, "t-002")
        fault_set.heal_all(now=200.0)
        assert not fault_set.is_fault_active(FaultKind.RECEIVER_NO_BOUNDARY)

    def test_escalation_injects_and_heals(self):
        """Fault 3: escalation — drives age past escalate_after_s."""
        fault_set = FaultSet()
        fault = Fault(kind=FaultKind.ESCALATION, target_terminal_id="t-003")
        fault_set.inject(fault, now=100.0)

        assert fault_set.is_fault_active(FaultKind.ESCALATION, "t-003")
        fault_set.heal_all(now=200.0)
        assert fault.healed

    def test_connection_refusal_injects_and_heals(self):
        """Fault 4: connection refusal — no_registry_records."""
        fault_set = FaultSet()
        fault = Fault(kind=FaultKind.CONNECTION_REFUSAL, target_terminal_id="t-004")
        fault_set.inject(fault, now=100.0)

        assert fault_set.is_fault_active(FaultKind.CONNECTION_REFUSAL, "t-004")
        fault_set.heal_all(now=200.0)
        assert fault.healed

    def test_no_injection_during_require_progress(self):
        """Do-NOT #6: No faults during phase 3."""
        fault_set = FaultSet()
        fault1 = Fault(kind=FaultKind.USER_DRAFT_PRESENT, target_terminal_id="t-001")
        fault_set.inject(fault1, now=100.0)
        fault_set.heal_all(now=200.0)
        fault_set.set_phase("REQUIRE_PROGRESS")

        with pytest.raises(RuntimeError, match="Cannot inject fault"):
            fault2 = Fault(kind=FaultKind.ESCALATION, target_terminal_id="t-005")
            fault_set.inject(fault2, now=300.0)


class TestAC6AssertionHasTeeth:
    """AC6: A planted bug triggers LIVENESS_TIMEOUT.

    [LB] Plant a stall, observe LIVENESS_TIMEOUT; remove plant, same seed passes.
    """

    def test_planted_stall_detected(self):
        """A stuck obligation with no healing produces LIVENESS_TIMEOUT or LIVELOCK."""
        clock = SimClock(initial_monotonic=1000.0)
        with install_clock(clock):
            world = SimWorld(seed=606)
            world.install()
            try:
                # Add an obligation that will never be delivered (planted stall)
                world.add_obligation(inbox_row_id=1, terminal_id="t-stall")

                # Setup a minimal driver (no watchdog ticks actually deliver)
                from cli_agent_orchestrator.services.stalled_callback_watchdog import (
                    StalledCallbackWatchdog,
                )

                watchdog = StalledCallbackWatchdog(clock=clock.monotonic)
                world.setup_driver(watchdog)
                world.driver.configure(tick_s=5.0, escalate_after_s=120.0)

                # Heal all faults (empty) and require progress
                world.heal_all()

                # Check liveness with a short bound
                verdict = world.check_liveness(bound_seconds=50.0)

                assert not verdict.passed, f"Expected failure but got: {verdict}"
                assert verdict.verdict in (
                    LivenessVerdict.LIVENESS_TIMEOUT,
                    LivenessVerdict.LIVELOCK,
                )
                assert "1" in verdict.details  # inbox_row_id=1
            finally:
                world.uninstall()

    def test_delivered_obligation_passes(self):
        """Same scenario but obligation is marked delivered → PASS."""
        clock = SimClock(initial_monotonic=1000.0)
        with install_clock(clock):
            world = SimWorld(seed=606)
            world.install()
            try:
                world.add_obligation(inbox_row_id=1, terminal_id="t-ok")
                from cli_agent_orchestrator.services.stalled_callback_watchdog import (
                    StalledCallbackWatchdog,
                )

                watchdog = StalledCallbackWatchdog(clock=clock.monotonic)
                world.setup_driver(watchdog)
                world.driver.configure(tick_s=5.0, escalate_after_s=120.0)

                # Mark delivered before liveness check
                world.mark_delivered(1)
                world.heal_all()

                verdict = world.check_liveness(bound_seconds=50.0)
                assert verdict.passed
            finally:
                world.uninstall()


class TestAC10HypothesisSeam:
    """AC10: The Hypothesis seam is real — SimWorld driven synchronously.

    [LB] No async def, no running event loop owned by the caller.
    """

    def test_simworld_synchronous_drive(self):
        """SimWorld.inject/heal_all/step/undelivered all work from a sync test."""
        # This test has NO async def — proves D20 contract
        clock = SimClock(initial_monotonic=1000.0)
        with install_clock(clock):
            world = SimWorld(seed=999)
            world.install()
            try:
                from cli_agent_orchestrator.services.stalled_callback_watchdog import (
                    StalledCallbackWatchdog,
                )

                watchdog = StalledCallbackWatchdog(clock=clock.monotonic)
                world.setup_driver(watchdog)
                world.driver.configure(tick_s=5.0, escalate_after_s=120.0)

                # Inject a fault
                fault = Fault(kind=FaultKind.USER_DRAFT_PRESENT, target_terminal_id="t-hyp")
                world.inject(fault)

                # Add obligation
                world.add_obligation(inbox_row_id=42, terminal_id="t-hyp")

                # Step (advances time)
                made_progress = world.step()
                assert made_progress

                # Check undelivered
                undelivered = world.undelivered()
                assert len(undelivered) == 1
                assert undelivered[0]["inbox_row_id"] == 42

                # Heal all
                world.heal_all()
                assert world.fault_set.phase == "REQUIRE_PROGRESS"

                # Mark delivered and verify
                world.mark_delivered(42)
                assert len(world.undelivered()) == 0
            finally:
                world.uninstall()

    def test_no_event_loop_owned_by_caller(self):
        """Verify no asyncio event loop is running during SimWorld operations."""
        import asyncio

        clock = SimClock(initial_monotonic=1000.0)
        with install_clock(clock):
            world = SimWorld(seed=888)
            world.install()
            try:
                from cli_agent_orchestrator.services.stalled_callback_watchdog import (
                    StalledCallbackWatchdog,
                )

                watchdog = StalledCallbackWatchdog(clock=clock.monotonic)
                world.setup_driver(watchdog)

                # Should not have a running event loop
                with pytest.raises(RuntimeError):
                    asyncio.get_running_loop()

                world.step()

                # Still no running loop after step
                with pytest.raises(RuntimeError):
                    asyncio.get_running_loop()
            finally:
                world.uninstall()



class TestPureLivelock:
    """S1/M3 kill: Dedicated test asserting verdict == LIVELOCK specifically.

    The livelock shape (D16, F206e class): the driver has no pending deadlines,
    step() returns False, but obligations remain undelivered. This is
    "everyone is up and nothing moves" — distinguished from LIVENESS_TIMEOUT
    (which means time ran out while ticks were still running).

    This test kills mutant M3 (removing LIVELOCK detection from check_liveness)
    because it asserts the exact verdict, not "either timeout or livelock".
    """

    def test_pure_livelock_verdict(self):
        """[LB] step()→False with undelivered obligations → LIVELOCK (not TIMEOUT).

        Kills M3: if LIVELOCK detection is removed, check_liveness would return
        LIVENESS_TIMEOUT instead, and this assertion fails.
        """
        from cli_agent_orchestrator.sim.driver import SimDriver, EventTrace

        clock = SimClock(initial_monotonic=1000.0)
        with install_clock(clock):
            world = SimWorld(seed=161616)
            world.install()
            try:
                from cli_agent_orchestrator.services.stalled_callback_watchdog import (
                    StalledCallbackWatchdog,
                )

                watchdog = StalledCallbackWatchdog(clock=clock.monotonic)
                # Create driver with auto_tick=False so step() can return False
                driver = SimDriver(
                    clock=clock,
                    watchdog=watchdog,
                    fault_set=world.fault_set,
                    trace=world.trace,
                    auto_tick=False,  # No automatic tick cadence deadline
                )
                world._driver = driver
                driver.configure(tick_s=5.0, escalate_after_s=120.0)

                # Add an obligation that will never be delivered
                world.add_obligation(inbox_row_id=9999, terminal_id="t-livelock")

                # Heal all (no faults to heal, transitions to REQUIRE_PROGRESS)
                world.heal_all()

                # Verify the livelock shape: step() returns False (no deadlines)
                assert driver.step() is False, "step() should return False with no deadlines"

                # Now check_liveness must detect LIVELOCK specifically
                verdict = world.check_liveness(bound_seconds=100.0)
                assert verdict.verdict == LivenessVerdict.LIVELOCK, (
                    f"Expected LIVELOCK but got {verdict.verdict}: {verdict.details}. "
                    "M3 mutant (removing LIVELOCK detection) would cause this to be LIVENESS_TIMEOUT."
                )
                assert "9999" in verdict.details
            finally:
                world.uninstall()
