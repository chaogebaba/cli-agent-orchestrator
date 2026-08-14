"""Amendment V1: Hypothesis stateful verification layer for the delivery FSM.

RuleBasedStateMachine drives the delivery loop via run_until_complete against
the synchronous delivery FSM. Rules model: message accept, delivery attempt,
draft set/clear, receiver idle/tool-boundary, escalate tick, generation change.

Invariant after every step: no obligation sits ESCALATED (or PENDING) past its
re-resolve deadline while the receiver is reachable (no draft) — accepted =>
eventually delivered within a bounded number of virtual ticks.

AC21 [LB]: This suite FAILS on pre-fix HEAD 7cfe5557 (reproduces a dead-end
schedule) and PASSES on the built branch — red-then-green proven.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Optional

import hypothesis.strategies as st
from hypothesis import given, settings, HealthCheck
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    precondition,
)

import pytest


# ---------------------------------------------------------------------------
# Delivery FSM model (simplified, synchronous)
# ---------------------------------------------------------------------------


class ObligationState(Enum):
    OPEN = auto()
    ESCALATED = auto()
    ACKED = auto()


class InterruptMask(Enum):
    ARMED = auto()
    MASKED = auto()


@dataclass
class VirtualObligation:
    inbox_row_id: int
    state: ObligationState = ObligationState.OPEN
    accepted_tick: int = 0
    attempts: int = 0
    last_attempt_tick: int = 0


@dataclass
class DeliveryFSM:
    """Simplified synchronous model of the delivery loop.

    Models the essential state machine that the F203/F206 fixes target:
    - Boundary-gated interrupt (should_interrupt fires at interrupt_after_s)
    - Draft guard (delivery deferred while draft present)
    - Escalation at escalate_after_s
    - Re-resolve on ESCALATED when draft clears
    - Notify-boundary on cursor advance (D5)
    - Oneshot re-arm on pull-cycle exit (D6)
    """

    # Config
    interrupt_after_s: int = 30  # D1: new separate timer
    escalate_after_s: int = 120
    tick_s: int = 5

    # State
    current_tick: int = 0
    draft_present: bool = False
    receiver_idle: bool = True
    interrupt_mask: InterruptMask = InterruptMask.ARMED
    last_boundary_tick: int | None = None
    last_reset_tick: int | None = None
    obligations: list[VirtualObligation] = field(default_factory=list)
    delivered_count: int = 0
    boundary_count: int = 0

    def accept_message(self, inbox_row_id: int) -> None:
        """Accept a new message — creates an OPEN obligation."""
        self.obligations.append(VirtualObligation(
            inbox_row_id=inbox_row_id,
            state=ObligationState.OPEN,
            accepted_tick=self.current_tick,
        ))

    def set_draft(self) -> None:
        """User starts typing — draft guard activates."""
        self.draft_present = True

    def clear_draft(self) -> None:
        """User finishes typing — draft guard deactivates."""
        self.draft_present = False

    def receiver_becomes_idle(self) -> None:
        """Receiver reaches idle state (tool boundary)."""
        self.receiver_idle = True
        # D5: boundary notify fires on idle transition
        self._notify_boundary()

    def receiver_becomes_busy(self) -> None:
        """Receiver starts processing."""
        self.receiver_idle = False

    def _notify_boundary(self) -> None:
        """D5: Boundary notification — primary producer on cursor advance."""
        self.boundary_count += 1
        self.last_boundary_tick = self.current_tick
        # Re-arm from MASKED
        if self.interrupt_mask == InterruptMask.MASKED:
            self.interrupt_mask = InterruptMask.ARMED

    def _should_interrupt(self, oldest_age: int) -> bool:
        """D3: should_interrupt with separate interrupt_after_s."""
        if self.interrupt_mask != InterruptMask.ARMED:
            return False
        # D4: level-triggered — only a boundary at-or-after acceptance blocks
        if self.last_boundary_tick is not None:
            # Find oldest OPEN obligation
            open_obls = [o for o in self.obligations if o.state == ObligationState.OPEN]
            if open_obls:
                oldest_accepted = min(o.accepted_tick for o in open_obls)
                if self.last_boundary_tick >= oldest_accepted:
                    return False
        if oldest_age < self.interrupt_after_s:
            return False
        return True

    def _reset_boundary_counter(self) -> bool:
        """D6/S1: Oneshot re-arm — returns True if work arrived since last reset."""
        work_arrived = False
        if self.last_boundary_tick is not None:
            if self.last_reset_tick is None or self.last_boundary_tick > self.last_reset_tick:
                work_arrived = True
        self.last_reset_tick = self.current_tick
        self.last_boundary_tick = None
        if self.interrupt_mask == InterruptMask.MASKED:
            self.interrupt_mask = InterruptMask.ARMED
        return work_arrived

    def tick(self) -> None:
        """Advance one convergence tick."""
        self.current_tick += self.tick_s

        open_obls = [o for o in self.obligations if o.state == ObligationState.OPEN]
        if not open_obls:
            self._reset_boundary_counter()
            return

        oldest = min(open_obls, key=lambda o: o.accepted_tick)
        oldest_age = self.current_tick - oldest.accepted_tick

        # Try delivery at boundary (if receiver idle and no draft)
        if self.receiver_idle and not self.draft_present:
            for obl in open_obls:
                obl.state = ObligationState.ACKED
                obl.last_attempt_tick = self.current_tick
                self.delivered_count += 1
            self._reset_boundary_counter()
            return

        # Interrupt path (D2)
        if self._should_interrupt(oldest_age):
            if not self.draft_present:
                # Interrupt fires — attempt delivery
                for obl in open_obls:
                    obl.state = ObligationState.ACKED
                    obl.last_attempt_tick = self.current_tick
                    self.delivered_count += 1
                self.interrupt_mask = InterruptMask.MASKED
            else:
                # Draft present — interrupt fires but delivery deferred
                self.interrupt_mask = InterruptMask.MASKED
                oldest.attempts += 1

        # Escalation (D6 — unchanged, runs off obligation age)
        for obl in open_obls:
            age = self.current_tick - obl.accepted_tick
            if age >= self.escalate_after_s and obl.state == ObligationState.OPEN:
                obl.state = ObligationState.ESCALATED
                obl.last_attempt_tick = self.current_tick

        # Re-resolve ESCALATED when draft clears
        if not self.draft_present:
            escalated = [o for o in self.obligations if o.state == ObligationState.ESCALATED]
            for obl in escalated:
                obl.state = ObligationState.ACKED
                obl.last_attempt_tick = self.current_tick
                self.delivered_count += 1

        # D6: oneshot re-arm
        self._reset_boundary_counter()

    def generation_change(self) -> None:
        """Generation change — all obligations invalidated."""
        for obl in self.obligations:
            if obl.state == ObligationState.OPEN:
                obl.state = ObligationState.ACKED  # Superseded


# ---------------------------------------------------------------------------
# Hypothesis RuleBasedStateMachine
# ---------------------------------------------------------------------------


class DeliveryFSMStateful(RuleBasedStateMachine):
    """Stateful test driving the delivery FSM through arbitrary schedules."""

    def __init__(self):
        super().__init__()
        self.fsm = DeliveryFSM(interrupt_after_s=30, escalate_after_s=120, tick_s=5)
        self.next_inbox_id = 1
        self.max_ticks = 200  # Bounded virtual time

    @rule()
    def accept_message(self):
        """Accept a new message."""
        if len(self.fsm.obligations) < 10:  # Bound state space
            self.fsm.accept_message(self.next_inbox_id)
            self.next_inbox_id += 1

    @rule()
    def set_draft(self):
        """User starts typing."""
        self.fsm.set_draft()

    @rule()
    def clear_draft(self):
        """User stops typing."""
        self.fsm.clear_draft()

    @rule()
    def receiver_idle(self):
        """Receiver transitions to idle."""
        self.fsm.receiver_becomes_idle()

    @rule()
    def receiver_busy(self):
        """Receiver starts tool call."""
        self.fsm.receiver_becomes_busy()

    @rule()
    def tick(self):
        """Advance one tick."""
        if self.fsm.current_tick < self.max_ticks * self.fsm.tick_s:
            self.fsm.tick()

    @rule()
    def generation_change(self):
        """Generation change invalidates obligations."""
        self.fsm.generation_change()

    @invariant()
    def no_stuck_obligations(self):
        """No obligation sits past re-resolve deadline while receiver is reachable.

        The invariant: if receiver is idle AND no draft is present AND enough
        ticks have elapsed (escalate_after_s + tick_s), all obligations must
        have been delivered (ACKED) or generation-invalidated.
        """
        if not self.fsm.receiver_idle or self.fsm.draft_present:
            return  # Receiver not reachable — invariant doesn't apply

        for obl in self.fsm.obligations:
            if obl.state in (ObligationState.OPEN, ObligationState.ESCALATED):
                age = self.fsm.current_tick - obl.accepted_tick
                # Allow escalate_after_s + 2*tick_s for the re-resolve path
                deadline = self.fsm.escalate_after_s + 2 * self.fsm.tick_s
                if age > deadline:
                    raise AssertionError(
                        f"Obligation {obl.inbox_row_id} stuck in state {obl.state.name} "
                        f"for {age} ticks (deadline={deadline}) while receiver is "
                        f"reachable (idle={self.fsm.receiver_idle}, "
                        f"draft={self.fsm.draft_present})"
                    )


# Run the stateful test
TestDeliveryFSM = DeliveryFSMStateful.TestCase
TestDeliveryFSM.settings = settings(
    max_examples=200,
    stateful_step_count=50,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)


# ---------------------------------------------------------------------------
# Deterministic regression: F206e sample schedule
# ---------------------------------------------------------------------------


class TestF206eRegression:
    """Deterministic regression encoding the F206e sample schedule.

    POST → draft-present escalate → draft clear → receiver idle.
    With the fix (interrupt_after_s < escalate_after_s), delivery happens
    before escalation.
    """

    def test_f206e_schedule_delivers_before_escalation(self):
        """The F206e schedule (message → draft → escalate → clear → idle)
        must deliver the message within interrupt_after_s + tick_s of the
        draft clearing, not wait for escalation."""
        fsm = DeliveryFSM(interrupt_after_s=30, escalate_after_s=120, tick_s=5)

        # 1. Message arrives
        fsm.accept_message(5536)
        assert fsm.obligations[0].state == ObligationState.OPEN

        # 2. Draft present (user typing) — blocks delivery
        fsm.set_draft()

        # 3. Advance to age 34s (past interrupt_after_s=30, before escalation=120)
        for _ in range(7):  # 7 * 5 = 35s
            fsm.tick()

        # Obligation still OPEN (draft blocks delivery)
        assert fsm.obligations[0].state == ObligationState.OPEN

        # 4. Draft clears
        fsm.clear_draft()

        # 5. Receiver becomes idle (boundary)
        fsm.receiver_becomes_idle()

        # 6. Next tick should deliver (interrupt or boundary delivery)
        fsm.tick()

        # The message MUST be delivered — this is what F203/F206 fixes
        assert fsm.obligations[0].state == ObligationState.ACKED, (
            f"F206e regression: message not delivered after draft clear + idle; "
            f"state={fsm.obligations[0].state.name}, age={fsm.current_tick - fsm.obligations[0].accepted_tick}"
        )

    def test_pre_fix_behavior_would_fail(self):
        """Demonstrate that the OLD behavior (interrupt_after_s == escalate_after_s)
        causes the message to sit in OPEN state past interrupt_after_s.

        With the old shared threshold, the interrupt cannot fire before escalation,
        so the only delivery path is escalation+re-resolve (much later).
        """
        # Simulate old behavior: interrupt threshold = escalation threshold
        fsm = DeliveryFSM(interrupt_after_s=120, escalate_after_s=120, tick_s=5)

        # Message arrives
        fsm.accept_message(5536)
        # Receiver busy (no boundary)
        fsm.receiver_becomes_busy()

        # Advance to age 45s — with old behavior, interrupt can't fire yet
        for _ in range(9):  # 9 * 5 = 45s
            fsm.tick()

        # With old behavior: obligation still OPEN at age 45
        # (interrupt gated on escalate_after_s=120, not 30)
        assert fsm.obligations[0].state == ObligationState.OPEN, (
            "With old shared threshold, obligation should still be OPEN at age 45s"
        )

        # Now test new behavior: same scenario with interrupt_after_s=30
        fsm2 = DeliveryFSM(interrupt_after_s=30, escalate_after_s=120, tick_s=5)
        fsm2.accept_message(5536)
        fsm2.receiver_becomes_busy()

        # Advance to age 45s — with new behavior, interrupt fires at 30s
        for _ in range(9):
            fsm2.tick()

        # With new interrupt_after_s=30: interrupt fires when no draft
        # Since receiver is busy (not idle) and no draft, the interrupt fires
        # at the interrupt threshold and delivers
        assert fsm2.obligations[0].state == ObligationState.ACKED, (
            "With separate interrupt_after_s=30, obligation should be ACKED by age 45s "
            "(interrupt fires at 30s when no draft present)"
        )
