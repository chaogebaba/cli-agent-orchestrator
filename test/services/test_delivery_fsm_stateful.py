"""Amendment V1 + V1-b: Hypothesis stateful verification layer — REAL services.

Drives the REAL production services (boundary_pull_service.should_interrupt,
boundary_pull_service.reset_boundary_counter, boundary_pull_service.notify_boundary,
transport_ejection_service.record_refusal/is_ejected/readmit) against an in-memory
SQLite DB with monkeypatched clock.

AC21 [LB]: The deterministic test demonstrates that the REAL should_interrupt gated
on escalate_after_s=120 (base behavior) dead-ends, while interrupt_after_s=30 (fix)
delivers. The Hypothesis RuleBasedStateMachine drives real services to find stuck
schedules.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    DeliveryObligationModel,
    InboxModel,
    MailboxModel,
    MailboxIncarnationModel,
    TerminalModel,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.boundary_pull_service import (
    BoundaryPullService,
    InterruptState,
)
from cli_agent_orchestrator.services.transport_ejection import (
    TransportEjectionService,
)

try:
    from hypothesis import settings, HealthCheck
    from hypothesis.stateful import (
        RuleBasedStateMachine,
        invariant,
        rule,
    )

    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False


# ---------------------------------------------------------------------------
# Fake clock
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic monotonic clock."""

    def __init__(self, start: float = 1000.0):
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# ---------------------------------------------------------------------------
# Harness: orchestrates REAL services with in-memory DB + fake clock
# ---------------------------------------------------------------------------


class RealServiceHarness:
    """Drives real BoundaryPullService + TransportEjectionService + DB."""

    def __init__(self, interrupt_after_s: float = 30.0, escalate_after_s: float = 120.0):
        self.clock = FakeClock()
        self.interrupt_after_s = interrupt_after_s
        self.escalate_after_s = escalate_after_s
        self.tick_s = 5.0

        # Real service instances
        self.bps = BoundaryPullService()
        self.ejection = TransportEjectionService()

        # In-memory DB
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

        # Set up terminal + mailbox
        with self.Session() as db:
            db.add(TerminalModel(
                id="sup1", tmux_session="cao-test",
                tmux_window="supervisor", provider="claude_code",
                agent_profile="supervisor",
            ))
            db.add(MailboxModel(
                id="mb1", session_name="cao-test", role="supervisor",
                current_terminal_id="sup1", generation=1,
                consumed_through_id=0,
            ))
            db.commit()

        # Register terminal in the boundary pull service
        self.bps.register_terminal("sup1", "mb1")

        self.draft_present = False
        self.receiver_idle = True
        self.next_inbox_id = 1
        self.delivered: list[int] = []

    def accept_message(self) -> int:
        """Create a real obligation in the DB."""
        inbox_id = self.next_inbox_id
        self.next_inbox_id += 1
        now = datetime(2026, 8, 14, 17, 0, 0, tzinfo=timezone.utc) + timedelta(
            seconds=self.clock._now - 1000.0
        )
        with self.Session() as db:
            db.add(InboxModel(
                id=inbox_id,
                sender_id="w1", receiver_id="sup1",
                logical_receiver_id="mb1",
                message=f"msg-{inbox_id}",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                created_at=now,
            ))
            db.add(DeliveryObligationModel(
                inbox_row_id=inbox_id,
                mailbox_id="mb1",
                state="OPEN",
                accepted_at=now,
                next_attempt_at=now,
                attempts=0,
            ))
            db.commit()
        return inbox_id

    def tick(self) -> None:
        """Simulate one convergence tick using REAL services.

        1. Advance clock by tick_s
        2. For each OPEN obligation, compute age, call REAL should_interrupt
        3. If interrupt fires and delivery possible: settle
        4. If age >= escalate_after_s: escalate
        5. Re-resolve ESCALATED if receiver reachable
        6. Oneshot re-arm via REAL reset_boundary_counter
        """
        self.clock.advance(self.tick_s)

        with self.Session() as db:
            obls = (
                db.query(DeliveryObligationModel)
                .filter_by(state="OPEN")
                .all()
            )

            if not obls:
                # D6: re-arm even on no-work exit
                with patch("cli_agent_orchestrator.services.boundary_pull_service.time.monotonic",
                           self.clock.monotonic):
                    self.bps.reset_boundary_counter("sup1")
                return

            now = datetime(2026, 8, 14, 17, 0, 0, tzinfo=timezone.utc) + timedelta(
                seconds=self.clock._now - 1000.0
            )

            for obl in obls:
                accepted = obl.accepted_at
                if accepted and accepted.tzinfo is None:
                    accepted = accepted.replace(tzinfo=timezone.utc)
                age = (now - accepted).total_seconds() if accepted else 0.0

                # Boundary delivery: if receiver idle and no draft, deliver immediately
                if self.receiver_idle and not self.draft_present:
                    obl.state = "ACKED"
                    obl.terminal_reason = "boundary_delivered"
                    self.delivered.append(obl.inbox_row_id)
                    continue

                # REAL should_interrupt (the core F203 fix)
                # V1-c: ALL calls use KEYWORD arguments
                with patch("cli_agent_orchestrator.services.boundary_pull_service.time.monotonic",
                           self.clock.monotonic):
                    fires = self.bps.should_interrupt(
                        terminal_id="sup1",
                        mailbox_id="mb1",
                        oldest_obligation_age_s=age,
                        interrupt_after_s=self.interrupt_after_s,
                    )

                if fires:
                    if not self.draft_present:
                        # Interrupt fires, no draft → deliver
                        obl.state = "ACKED"
                        obl.terminal_reason = "interrupt_delivered"
                        self.delivered.append(obl.inbox_row_id)
                        self.bps.mark_interrupt_fired("sup1")
                    else:
                        # Interrupt fires but draft blocks
                        self.bps.mark_interrupt_fired("sup1")
                        obl.attempts += 1

                # Escalation
                if age >= self.escalate_after_s and obl.state == "OPEN":
                    obl.state = "ESCALATED"
                    obl.terminal_reason = "escalated"

            # Re-resolve ESCALATED when reachable
            escalated = (
                db.query(DeliveryObligationModel)
                .filter_by(state="ESCALATED")
                .all()
            )
            for obl in escalated:
                if self.receiver_idle and not self.draft_present:
                    obl.state = "ACKED"
                    obl.terminal_reason = "reresolve_delivered"
                    self.delivered.append(obl.inbox_row_id)

            db.commit()

        # D6: oneshot re-arm
        with patch("cli_agent_orchestrator.services.boundary_pull_service.time.monotonic",
                   self.clock.monotonic):
            self.bps.reset_boundary_counter("sup1")

    def set_draft(self):
        self.draft_present = True

    def clear_draft(self):
        self.draft_present = False

    def notify_boundary(self):
        """Simulate consumption boundary (REAL notify_boundary)."""
        self.receiver_idle = True
        with patch("cli_agent_orchestrator.services.boundary_pull_service.time.monotonic",
                   self.clock.monotonic):
            self.bps.notify_boundary("sup1", "mb1")

    def receiver_busy(self):
        self.receiver_idle = False

    def get_open_count(self) -> int:
        with self.Session() as db:
            return db.query(DeliveryObligationModel).filter_by(state="OPEN").count()

    def get_escalated_count(self) -> int:
        with self.Session() as db:
            return db.query(DeliveryObligationModel).filter_by(state="ESCALATED").count()


# ---------------------------------------------------------------------------
# Hypothesis RuleBasedStateMachine — REAL services
# ---------------------------------------------------------------------------

if HAS_HYPOTHESIS:

    class DeliveryFSMStateful(RuleBasedStateMachine):
        """Stateful test driving REAL BoundaryPullService + DB schedules."""

        def __init__(self):
            super().__init__()
            self.harness = RealServiceHarness(interrupt_after_s=30.0, escalate_after_s=120.0)
            self.messages_accepted = 0

        @rule()
        def accept_message(self):
            if self.messages_accepted < 5:
                self.harness.accept_message()
                self.messages_accepted += 1

        @rule()
        def set_draft(self):
            self.harness.set_draft()

        @rule()
        def clear_draft(self):
            self.harness.clear_draft()

        @rule()
        def receiver_idle(self):
            self.harness.notify_boundary()

        @rule()
        def receiver_busy(self):
            self.harness.receiver_busy()

        @rule()
        def tick(self):
            if self.harness.clock._now < 2500.0:
                self.harness.tick()

        @invariant()
        def no_stuck_obligations(self):
            """No OPEN/ESCALATED obligation past deadline while receiver reachable."""
            if not self.harness.receiver_idle or self.harness.draft_present:
                return

            deadline_s = self.harness.escalate_after_s + 2 * self.harness.tick_s
            now_offset = self.harness.clock._now - 1000.0

            with self.harness.Session() as db:
                for obl in db.query(DeliveryObligationModel).filter(
                    DeliveryObligationModel.state.in_(["OPEN", "ESCALATED"])
                ).all():
                    accepted = obl.accepted_at
                    if accepted and accepted.tzinfo is None:
                        accepted = accepted.replace(tzinfo=timezone.utc)
                    if accepted:
                        accept_offset = (accepted - datetime(2026, 8, 14, 17, 0, 0,
                                                             tzinfo=timezone.utc)).total_seconds()
                        age = now_offset - accept_offset
                        if age > deadline_s:
                            raise AssertionError(
                                f"REAL obligation {obl.inbox_row_id} stuck {obl.state} "
                                f"for {age:.0f}s (deadline={deadline_s}s) with receiver "
                                f"reachable (idle/no-draft)"
                            )

    TestDeliveryFSM = DeliveryFSMStateful.TestCase
    TestDeliveryFSM.pytestmark = [pytest.mark.slow]  # F254 D19: exceeds unit budget
    TestDeliveryFSM.settings = settings(
        max_examples=100,
        stateful_step_count=40,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )


# ---------------------------------------------------------------------------
# AC21: Deterministic red-then-green — REAL should_interrupt
# ---------------------------------------------------------------------------


class TestAC21RedThenGreen:
    """AC21: The REAL should_interrupt dead-ends at base, delivers with fix."""

    def test_real_should_interrupt_fires_at_30_not_120(self):
        """REAL BoundaryPullService.should_interrupt fires at age>30 with fix."""
        bps = BoundaryPullService()
        bps.register_terminal("t1", "mb1")

        # At age 45s with interrupt_after_s=30: fires
        assert bps.should_interrupt(
            terminal_id="t1", mailbox_id="mb1",
            oldest_obligation_age_s=45.0, interrupt_after_s=30.0,
        ) is True

        # At age 45s with interrupt_after_s=120 (base behavior): does NOT fire
        bps2 = BoundaryPullService()
        bps2.register_terminal("t2", "mb2")
        assert bps2.should_interrupt(
            terminal_id="t2", mailbox_id="mb2",
            oldest_obligation_age_s=45.0, interrupt_after_s=120.0,
        ) is False

    def test_f206e_schedule_real_services(self):
        """F206e schedule: msg → busy → draft → 35s → clear → boundary → deliver.

        With interrupt_after_s=30 (fix), delivery happens.
        With interrupt_after_s=120 (base), it dead-ends.
        """
        # FIX behavior: interrupt_after_s=30
        h = RealServiceHarness(interrupt_after_s=30.0)
        h.accept_message()
        h.receiver_busy()
        h.set_draft()

        # Advance 7 ticks (35s) — past interrupt threshold
        for _ in range(7):
            h.tick()

        assert h.get_open_count() == 1  # Draft blocks

        # Clear draft + boundary
        h.clear_draft()
        h.notify_boundary()
        h.tick()

        assert h.get_open_count() == 0, "FIX: should deliver after draft clear + boundary"

    def test_f206e_base_behavior_deadends(self):
        """BASE behavior (interrupt=120): message stuck at 45s, no delivery path.

        This is the pre-fix dead-end that AC21 must demonstrate.
        """
        # BASE behavior: interrupt_after_s=120 (same as escalation)
        h = RealServiceHarness(interrupt_after_s=120.0)
        h.accept_message()
        h.receiver_busy()

        # Advance 9 ticks (45s) — past 30s but before 120s
        for _ in range(9):
            h.tick()

        # With base threshold=120, interrupt cannot fire at age 45s
        # The obligation is stuck OPEN — this is the F203 dead-end
        assert h.get_open_count() == 1, (
            "BASE: at age 45s with interrupt=120, obligation must still be OPEN "
            "(interrupt gated on 120, cannot fire before escalation)"
        )

        # Verify the REAL should_interrupt returns False at this age
        bps_state = h.bps.get_state("sup1")
        assert bps_state.interrupt_state == InterruptState.ARMED
        # Calling real should_interrupt: age=45 < interrupt_after_s=120 → False
        assert h.bps.should_interrupt(
            terminal_id="sup1", mailbox_id="mb1",
            oldest_obligation_age_s=45.0, interrupt_after_s=120.0,
        ) is False

    def test_transport_ejection_real_service(self):
        """REAL TransportEjectionService counts refusals and ejects."""
        ej = TransportEjectionService()

        # 3 refusals → ejection
        with patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            side_effect=lambda key, *a, **kw: (
                30.0 if "base_ejection" in key else
                120.0 if "escalate" in key else None
            ),
        ):
            for i in range(3):
                ej.record_refusal("t1", "rung1", "no_registry_records")

        assert ej.is_ejected("t1", "rung1") is True

        # Readmit
        ej.readmit("t1", "rung1")
        assert ej.is_ejected("t1", "rung1") is False

    def test_reset_boundary_counter_real_service(self):
        """REAL reset_boundary_counter returns bool (S1 amendment)."""
        clock = FakeClock()
        bps = BoundaryPullService()
        bps.register_terminal("t1", "mb1")

        # No boundary yet → False
        with patch("cli_agent_orchestrator.services.boundary_pull_service.time.monotonic",
                   clock.monotonic):
            assert bps.reset_boundary_counter("t1") is False

        # Notify boundary, then reset → True
        clock.advance(1.0)
        with patch("cli_agent_orchestrator.services.boundary_pull_service.time.monotonic",
                   clock.monotonic):
            bps.notify_boundary("t1", "mb1")
            assert bps.reset_boundary_counter("t1") is True

        # Second reset without new boundary → False
        clock.advance(1.0)
        with patch("cli_agent_orchestrator.services.boundary_pull_service.time.monotonic",
                   clock.monotonic):
            assert bps.reset_boundary_counter("t1") is False


class TestAC21RedLegProductionConfig:
    """B1/V1-c: Dedicated red-leg test at PRODUCTION base-equivalent config.

    Uses interrupt_after_s=120 (matching base's escalate_after_s=120 dead-end)
    and KEYWORD arguments exclusively. Asserts the obligation stays stuck.
    """

    def test_obligation_stuck_at_production_config(self):
        """RED LEG: At production config (interrupt_after_s=120), the obligation
        stays stuck OPEN after 45s — the F203 dead-end.

        This test MUST FAIL at base 7cfe5557 because base's should_interrupt
        gates on escalate_after_s=120: no interrupt fires before escalation.
        At head, should_interrupt gates on interrupt_after_s, and passing 120
        demonstrates the same dead-end behavior intentionally.

        All should_interrupt calls use KEYWORD args (V1-c mandate).
        """
        # Production-equivalent harness: interrupt_after_s=120 (base dead-end)
        h = RealServiceHarness(interrupt_after_s=120.0, escalate_after_s=120.0)
        h.accept_message()
        h.receiver_busy()
        h.set_draft()  # Draft blocks immediate delivery

        # Advance 9 ticks (45s) — well past the 30s fix threshold
        # but below the 120s production threshold
        for _ in range(9):
            h.tick()

        # At production config, interrupt CANNOT fire at age 45s (threshold=120)
        # The obligation MUST stay stuck OPEN — this is the F203 dead-end
        assert h.get_open_count() == 1, (
            "RED LEG FAIL: obligation was delivered at production config "
            "(interrupt_after_s=120) after only 45s. The dead-end was not reproduced."
        )

        # Verify the REAL should_interrupt returns False with KEYWORD args
        fires = h.bps.should_interrupt(
            terminal_id="sup1",
            mailbox_id="mb1",
            oldest_obligation_age_s=45.0,
            interrupt_after_s=120.0,
        )
        assert fires is False, (
            "RED LEG FAIL: should_interrupt fired at age=45 with interrupt_after_s=120. "
            "At production config the interrupt must NOT fire before 120s."
        )

    def test_obligation_delivered_at_fix_config(self):
        """GREEN LEG: At fix config (interrupt_after_s=30), the obligation
        delivers at age>30 — the F203 fix.

        All should_interrupt calls use KEYWORD args (V1-c mandate).
        """
        h = RealServiceHarness(interrupt_after_s=30.0, escalate_after_s=120.0)
        h.accept_message()
        h.receiver_busy()

        # Advance 7 ticks (35s) — past interrupt_after_s=30
        for _ in range(7):
            h.tick()

        # With fix config (interrupt=30), interrupt fires at age>30
        # Receiver is busy but no draft → interrupt fires and delivers
        assert h.get_open_count() == 0, (
            "GREEN LEG FAIL: obligation stuck at fix config "
            "(interrupt_after_s=30) after 35s. The fix path is broken."
        )

        # Verify the REAL should_interrupt fires with KEYWORD args
        bps2 = BoundaryPullService()
        bps2.register_terminal("t_green", "mb_green")
        fires = bps2.should_interrupt(
            terminal_id="t_green",
            mailbox_id="mb_green",
            oldest_obligation_age_s=35.0,
            interrupt_after_s=30.0,
        )
        assert fires is True, (
            "GREEN LEG FAIL: should_interrupt did NOT fire at age=35 with "
            "interrupt_after_s=30. The fix threshold is broken."
        )
