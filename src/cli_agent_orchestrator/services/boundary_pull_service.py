"""FX194: Message pull-first architecture — boundary consumption + masked interrupt.

This module implements the pull-first delivery pivot:
- D1: In-context delivery happens at consumption boundaries (tool-call return,
  idle transition) — NOT via the tick loop's nudge mechanism.
- D1b: Health warning when an obligation crosses E with zero boundary deliveries
  observed, distinguishing "model stuck thinking" from harness contract regression.
- D2: Composer injection demoted to a single bounded interrupt (NAPI-style):
  fires ONLY when no consumption boundary has occurred for the whole E-bound window.
  One fire, draft-guarded (fx193 D2b), jittered (fx193 A1). Re-arm semantics:
  after the interrupt fires it is MASKED; it re-arms on the FIRST consumption
  boundary that follows — the model must reach at least one tool-call or idle
  boundary after an interrupt before any second interrupt can fire.
- D3: One coalesced signal per terminal carrying a count (extends fx193 D3).
- D4: Human-facing indicator via tmux user variable @cao_pending.
- D6: Escalation unchanged (runs off obligation age, not nudge count).

Decisions cite precedents: Kafka consumer pull, Erlang mailbox `receive`,
NAPI interrupt-coalesce-poll-re-arm, NIC interrupt coalescing, TCP OOB separation.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# D2: Masked interrupt state machine (NAPI-style)
#
# States:
#   ARMED   — interrupt can fire if no boundary occurs within E-window
#   MASKED  — interrupt has fired; waiting for a consumption boundary to re-arm
#   IDLE    — no OPEN obligations (no interrupt needed)
#
# Transitions:
#   IDLE → ARMED:   first OPEN obligation created
#   ARMED → MASKED: interrupt fires (no boundary for E-window)
#   MASKED → ARMED: first consumption boundary after fire (re-arm)
#   ARMED → IDLE:   all obligations settled
#   MASKED → IDLE:  all obligations settled
# ---------------------------------------------------------------------------


class InterruptState:
    """Per-terminal interrupt mask state."""

    ARMED = "armed"
    MASKED = "masked"
    IDLE = "idle"


@dataclass
class _TerminalPullState:
    """Per-terminal state for the pull-first architecture.

    Tracks whether a consumption boundary has been observed during the current
    E-window for each OPEN obligation set, and the interrupt mask state.
    """

    terminal_id: str
    mailbox_id: str

    # D2: NAPI interrupt mask state
    interrupt_state: str = InterruptState.IDLE

    # D1b: Has any boundary delivery occurred since the oldest OPEN obligation
    # was created? Used to distinguish "stuck thinking" from "harness broken".
    boundary_deliveries_observed: int = 0

    # Monotonic time of last observed consumption boundary (tool-call or idle)
    last_boundary_at: float | None = None

    # Monotonic time when the interrupt fired (for D2 one-fire-per-E-window)
    interrupt_fired_at: float | None = None

    # D4: Last pending count written to tmux (avoid churn)
    last_pending_count_written: int = 0


class BoundaryPullService:
    """FX194: Manages pull-first consumption and the masked interrupt state machine.

    Thread-safe — called from convergence tick, boundary hooks, and status transitions.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, _TerminalPullState] = {}

    # ------------------------------------------------------------------
    # D1: Consumption boundary notification
    # ------------------------------------------------------------------

    def notify_boundary(self, terminal_id: str, mailbox_id: str) -> None:
        """Called when a consumption boundary occurs (tool-call return or idle).

        This is the pull-side "demand signal": the supervisor has reached a point
        where it can consume. At this point:
        - D1: Delivery happens here (outside this module — the harness surfaces
          pending messages in-context).
        - D2: Re-arm the interrupt if it was MASKED.
        - D1b: Increment boundary delivery counter.
        """
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                return

            state.last_boundary_at = time.monotonic()
            state.boundary_deliveries_observed += 1

            # D2: Re-arm on first boundary after fire (NAPI poll→re-arm)
            if state.interrupt_state == InterruptState.MASKED:
                state.interrupt_state = InterruptState.ARMED
                logger.debug(
                    "fx194 interrupt re-armed terminal=%s (boundary after fire)",
                    terminal_id,
                )

    # ------------------------------------------------------------------
    # D2: Interrupt eligibility check (called from convergence tick)
    # ------------------------------------------------------------------

    def should_interrupt(
        self,
        terminal_id: str,
        mailbox_id: str,
        oldest_obligation_age_s: float,
        escalate_after_s: float,
    ) -> bool:
        """Check if the masked interrupt should fire for this terminal.

        Returns True if ALL conditions are met:
        - Interrupt state is ARMED (not MASKED, not IDLE)
        - No consumption boundary has occurred for the entire E-window
          (oldest obligation age >= E AND no boundary since obligation created)
        - The obligation is still OPEN (not yet escalated)

        D2: Per obligation, at most one interrupt per E-window. Multiple OPEN
        obligations share one coalesced interrupt (D3) governed by the OLDEST
        obligation's age.
        """
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                # First time seeing this terminal — initialize
                state = _TerminalPullState(
                    terminal_id=terminal_id,
                    mailbox_id=mailbox_id,
                    interrupt_state=InterruptState.ARMED,
                )
                self._states[terminal_id] = state

            # Ensure mailbox is current
            state.mailbox_id = mailbox_id

            # Can only fire when ARMED
            if state.interrupt_state != InterruptState.ARMED:
                return False

            # D2: Fire only when no boundary for the E-window
            # The E-window is the obligation's own age clock
            if state.last_boundary_at is not None:
                # A boundary occurred — no interrupt needed (pull is working)
                return False

            # No boundary has occurred at all — check if obligation age >= E
            # (the one case pull cannot reach: stuck thinking for >E seconds)
            if oldest_obligation_age_s < escalate_after_s:
                return False

            return True

    def mark_interrupt_fired(self, terminal_id: str) -> None:
        """Mark the interrupt as fired — transitions ARMED → MASKED.

        After this, no second interrupt can fire until a consumption boundary
        re-arms it (MASKED → ARMED).
        """
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                return
            state.interrupt_state = InterruptState.MASKED
            state.interrupt_fired_at = time.monotonic()
            logger.debug(
                "fx194 interrupt fired and masked terminal=%s",
                terminal_id,
            )

    # ------------------------------------------------------------------
    # D1b: Health warning — harness contract monitoring
    # ------------------------------------------------------------------

    def check_health_warning(
        self,
        terminal_id: str,
        oldest_obligation_age_s: float,
        escalate_after_s: float,
    ) -> str | None:
        """D1b: Check if a health warning should be emitted.

        Returns a diagnostic string if the obligation crosses E with zero
        boundary deliveries observed. The diagnostic distinguishes:
        - "stuck_thinking": interrupt state is ARMED, no boundary seen
          (expected D2 case — model in a long think, interrupt will handle it)
        - "harness_contract_broken": interrupt already fired (MASKED) but still
          no boundary observed (H1/H2 regression — the harness isn't surfacing)

        Returns None if no warning needed.
        """
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                return None

            # Only warn when obligation has crossed E
            if oldest_obligation_age_s < escalate_after_s:
                return None

            # If boundaries HAVE been observed, no warning needed
            if state.boundary_deliveries_observed > 0:
                return None

            # Zero boundary deliveries — distinguish the cases
            if state.interrupt_state == InterruptState.MASKED:
                # Interrupt already fired, still no boundary → harness broken
                return "harness_contract_broken"
            else:
                # No boundary yet, interrupt hasn't fired → stuck thinking
                return "stuck_thinking"

    # ------------------------------------------------------------------
    # D4: Pending count tracking for tmux @cao_pending
    # ------------------------------------------------------------------

    def update_pending_count(
        self,
        terminal_id: str,
        tmux_session: str,
        pending_count: int,
    ) -> None:
        """D4: Update the @cao_pending tmux user variable on count change.

        Sets @cao_pending to N while obligations are OPEN.
        Unsets (-u) on drain (count=0).
        Write-through only on count change (no per-tick tmux churn).
        NEVER writes the status-right format string.
        """
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                if pending_count == 0:
                    return
                state = _TerminalPullState(
                    terminal_id=terminal_id,
                    mailbox_id="",
                    interrupt_state=InterruptState.ARMED,
                )
                self._states[terminal_id] = state

            # Only write on count change
            if state.last_pending_count_written == pending_count:
                return

            state.last_pending_count_written = pending_count

        # Write to tmux outside the lock
        self._write_tmux_pending(tmux_session, pending_count)

    def _write_tmux_pending(self, tmux_session: str, count: int) -> None:
        """Write @cao_pending user variable to tmux. Never touches status-right format."""
        try:
            import subprocess

            from cli_agent_orchestrator.utils.tmux_command import tmux_argv

            if count > 0:
                subprocess.run(
                    tmux_argv("set-option", "-t", tmux_session, "@cao_pending", str(count)),
                    capture_output=True,
                    timeout=5,
                )
            else:
                # Unset on drain
                subprocess.run(
                    tmux_argv("set-option", "-t", tmux_session, "-u", "@cao_pending"),
                    capture_output=True,
                    timeout=5,
                )
        except Exception as e:
            logger.debug("fx194 tmux @cao_pending write failed: %s", e)

    # ------------------------------------------------------------------
    # Lifecycle: register/unregister terminals
    # ------------------------------------------------------------------

    def register_terminal(self, terminal_id: str, mailbox_id: str) -> None:
        """Register a terminal for pull-first tracking."""
        with self._lock:
            if terminal_id not in self._states:
                self._states[terminal_id] = _TerminalPullState(
                    terminal_id=terminal_id,
                    mailbox_id=mailbox_id,
                    interrupt_state=InterruptState.ARMED,
                )

    def unregister_terminal(self, terminal_id: str) -> None:
        """Remove tracking for a terminal."""
        with self._lock:
            self._states.pop(terminal_id, None)

    def reset_boundary_counter(self, terminal_id: str) -> None:
        """Reset boundary counter when all obligations settle (new E-window)."""
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                return
            state.boundary_deliveries_observed = 0
            state.last_boundary_at = None
            state.interrupt_fired_at = None
            if state.interrupt_state == InterruptState.MASKED:
                state.interrupt_state = InterruptState.ARMED

    # ------------------------------------------------------------------
    # Introspection (for testing)
    # ------------------------------------------------------------------

    def get_state(self, terminal_id: str) -> _TerminalPullState | None:
        """Return a snapshot of terminal state for testing."""
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                return None
            return _TerminalPullState(
                terminal_id=state.terminal_id,
                mailbox_id=state.mailbox_id,
                interrupt_state=state.interrupt_state,
                boundary_deliveries_observed=state.boundary_deliveries_observed,
                last_boundary_at=state.last_boundary_at,
                interrupt_fired_at=state.interrupt_fired_at,
                last_pending_count_written=state.last_pending_count_written,
            )


# Module-level singleton
boundary_pull_service = BoundaryPullService()
