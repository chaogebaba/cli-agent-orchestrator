"""FX193: Nudge discipline — fire-time revalidation, busy-aware repeats, coalescing.

Sits between convergence_tick and attempt_rung2 to eliminate stale nudge bursts.
The module maintains per-terminal nudge state and gates repeat nudges on terminal
status, while preserving the first-nudge-immediate guarantee and the E-bound
escalation path.

Decision wall: D1 (fire-time revalidation), D2 (first-nudge-immediate + busy-gate),
D3 (single-armed coalescing), D4 (30/60/120 capped backoff), D5 (escalation untouched).

Amendment A1 (fx193-A1-D2): The obligation scheduler is named after the SQS shape —
visibility_timeout_at (per-message next_attempt_at), receive_count (attempts),
escalation/DLQ (ESCALATED state). The repeat backoff uses floor-clamped Full Jitter:
    sleep = random_between(base=30, min(cap=120, base * 2^n))
where n is the 0-indexed repeat index (n=0 degenerates to exactly 30s). The floor
preserves D4's minimum spacing; the cap aligns with the fx191 E-bound (120s).
Config knob delivery.jitter=off restores the deterministic 30/60/120 ladder verbatim.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# fx193-A1-D2: SQS vocabulary mapping (constraint on the code, not commentary)
#
# CAO obligation field       | SQS analog
# ---------------------------|-----------------------------------------
# next_attempt_at            | visibility_timeout_at — per-message timer
#                            |   controlling when the message becomes
#                            |   visible (eligible for redelivery) again
# attempts (obl.attempts)    | receive_count — how many times the message
#                            |   has been received/delivered
# state=ESCALATED            | redrive to DLQ — message moved to the
#                            |   dead-letter queue after maxReceiveCount
#                            |   exceeded
# state=ACKED                | DeleteMessage — consumer confirms processing
# E-bound (escalate_after_s) | maxReceiveCount-based redrive threshold
#                            |   (time-based rather than count-based in CAO
#                            |   because a single obligation = one logical
#                            |   delivery, not per-attempt counting)
# ---------------------------------------------------------------------------

# D4 / A1-D1: Backoff parameters
BACKOFF_BASE: int = 30  # seconds — minimum repeat spacing
BACKOFF_CAP: int = 120  # seconds — aligns with fx191 E-bound

# Legacy deterministic ladder (delivery.jitter=off)
BACKOFF_SEQUENCE: tuple[int, ...] = (30, 60, 120)


class JitterRNG(Protocol):
    """Protocol for injectable RNG — tests pass a seeded generator."""

    def randint(self, a: int, b: int) -> int: ...


class _DefaultRNG:
    """Production RNG — delegates to stdlib random."""

    def randint(self, a: int, b: int) -> int:
        return random.randint(a, b)


_default_rng = _DefaultRNG()


@dataclass
class _NudgeState:
    """Per-terminal nudge state. At most one armed nudge per terminal (D3).

    Field naming follows the SQS vocabulary constraint (fx193-A1-D2):
    - receive_count: how many times this nudge has fired (SQS receiveCount)
    - visibility_timeout_at: monotonic time when the nudge becomes eligible
      for the next fire (SQS per-message visibility timeout)
    """

    # The terminal_id this nudge targets (the supervisor terminal)
    terminal_id: str
    # The mailbox_id for cursor lookup
    mailbox_id: str
    # Coalesced payload: count of pending messages and oldest inbox row id
    message_count: int = 0
    oldest_inbox_row_id: int = 0
    # Has the first nudge for this obligation set fired? (D2)
    first_fired: bool = False
    # Current backoff step index (D4 / A1-D1: 0-indexed repeat number)
    backoff_step: int = 0
    # SQS: receive_count — total fires for this armed nudge set
    receive_count: int = 0
    # SQS: visibility_timeout_at — monotonic time when next repeat may fire
    # (None = not scheduled)
    visibility_timeout_at: float | None = None
    # Last known terminal status for busy-gating (D2)
    last_status: TerminalStatus = TerminalStatus.UNKNOWN
    # Whether the repeat is currently parked due to busy terminal
    parked: bool = False
    # Monotonic time of last successful nudge fire
    last_fired_at: float | None = None

    # Legacy compat property so existing code referencing next_fire_at still works
    @property
    def next_fire_at(self) -> float | None:
        return self.visibility_timeout_at

    @next_fire_at.setter
    def next_fire_at(self, value: float | None) -> None:
        self.visibility_timeout_at = value


class NudgeDiscipline:
    """FX193: Manages nudge gating, coalescing, and backoff.

    Thread-safe — called from the convergence tick (watchdog run loop thread).

    A1: Injectable RNG for jittered backoff. Tests pass a seeded generator;
    production uses stdlib random. Config knob delivery.jitter=off restores
    the deterministic 30/60/120 ladder.
    """

    def __init__(self, *, rng: JitterRNG | None = None) -> None:
        self._lock = threading.Lock()
        # Keyed by terminal_id (the supervisor terminal receiving nudges)
        self._states: dict[str, _NudgeState] = {}
        self._rng: JitterRNG = rng or _default_rng

    # ------------------------------------------------------------------
    # D3: Coalescing — arm or update the single nudge for a terminal
    # ------------------------------------------------------------------

    def arm_or_coalesce(
        self,
        terminal_id: str,
        mailbox_id: str,
        message_count: int,
        oldest_inbox_row_id: int,
    ) -> bool:
        """Arm a nudge or coalesce into the existing armed nudge.

        Returns True if this is a NEW arming (first nudge should fire immediately).
        Returns False if coalesced into an existing armed nudge (D3: merge fires
        immediately — the next fire_due check will see updated payload and reset backoff).
        """
        now = time.monotonic()
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                # New arming — first nudge fires immediately (D2)
                self._states[terminal_id] = _NudgeState(
                    terminal_id=terminal_id,
                    mailbox_id=mailbox_id,
                    message_count=message_count,
                    oldest_inbox_row_id=oldest_inbox_row_id,
                    first_fired=False,
                    visibility_timeout_at=now,  # immediate
                )
                return True
            else:
                # Coalesce: update payload, merge-fires-immediately (D3 amended)
                state.message_count = message_count
                state.oldest_inbox_row_id = oldest_inbox_row_id
                state.mailbox_id = mailbox_id
                # D3 amended: merge fires immediately + backoff reset on merge
                state.visibility_timeout_at = now
                state.backoff_step = 0
                state.parked = False
                return False

    # ------------------------------------------------------------------
    # D2: Record terminal status for busy-gating
    # ------------------------------------------------------------------

    def record_status(self, terminal_id: str, status: TerminalStatus) -> None:
        """Update the terminal status used for busy-gating repeats.

        When a terminal transitions from PROCESSING to IDLE, any parked repeat
        is unparked and scheduled to fire immediately (with revalidation at fire time).
        """
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                return
            old_status = state.last_status
            state.last_status = status
            # Unpark on transition to idle (D2: resume with one revalidated nudge)
            if (
                state.parked
                and old_status == TerminalStatus.PROCESSING
                and status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED)
            ):
                state.parked = False
                state.visibility_timeout_at = time.monotonic()

    # ------------------------------------------------------------------
    # D1 + D2 + D4: Determine which nudges should fire this tick
    # ------------------------------------------------------------------

    def collect_due(
        self,
        now: float | None = None,
        *,
        get_consumption_cursor: "GetCursorFn | None" = None,
        get_pending_oldest: "GetPendingFn | None" = None,
    ) -> list[NudgeFireIntent]:
        """Collect nudges that should fire now, applying D1/D2/D4 gates.

        Args:
            now: Monotonic timestamp (defaults to time.monotonic()).
            get_consumption_cursor: Callable(mailbox_id) -> int|None returning
                the consumed_through_id for a mailbox. Used for D1 revalidation.
            get_pending_oldest: Callable(mailbox_id) -> tuple[int, int]|None returning
                (count, oldest_id) of pending messages for a mailbox. Used for
                payload refresh at fire time.

        Returns:
            List of NudgeFireIntent for nudges that passed all gates.
        """
        if now is None:
            now = time.monotonic()

        intents: list[NudgeFireIntent] = []
        to_disarm: list[str] = []

        with self._lock:
            for terminal_id, state in list(self._states.items()):
                # Not yet due (SQS: visibility timeout not yet expired)
                if state.visibility_timeout_at is None or now < state.visibility_timeout_at:
                    continue

                # D2: Repeats (not first) park while terminal is PROCESSING
                if state.first_fired and state.last_status == TerminalStatus.PROCESSING:
                    state.parked = True
                    state.visibility_timeout_at = None  # will be re-armed on idle transition
                    continue

                # D1: Fire-time revalidation — re-read cursor + pending set
                should_cancel = False
                if get_consumption_cursor is not None:
                    cursor = get_consumption_cursor(state.mailbox_id)
                    if cursor is not None and cursor >= state.oldest_inbox_row_id:
                        should_cancel = True

                if not should_cancel and get_pending_oldest is not None:
                    pending = get_pending_oldest(state.mailbox_id)
                    if pending is None or pending[0] == 0:
                        should_cancel = True
                    else:
                        # Refresh payload from pending set (fire-time truth)
                        state.message_count = pending[0]
                        state.oldest_inbox_row_id = pending[1]

                if should_cancel:
                    to_disarm.append(terminal_id)
                    continue

                # This nudge fires
                intents.append(
                    NudgeFireIntent(
                        terminal_id=terminal_id,
                        mailbox_id=state.mailbox_id,
                        message_count=state.message_count,
                        oldest_inbox_row_id=state.oldest_inbox_row_id,
                        is_first=not state.first_fired,
                    )
                )

                # Mark first as fired, schedule next repeat with backoff (D4 / A1-D1)
                state.first_fired = True
                state.last_fired_at = now
                state.receive_count += 1

                # A1-D1: Floor-clamped Full Jitter backoff
                # sleep = random_between(base=30, min(cap=120, base * 2^n))
                # Config knob delivery.jitter=off restores deterministic ladder.
                step = state.backoff_step
                delay = self._compute_backoff_delay(step)
                state.backoff_step = step + 1
                state.visibility_timeout_at = now + delay

            # Disarm cancelled nudges
            for terminal_id in to_disarm:
                del self._states[terminal_id]

        return intents

    def _compute_backoff_delay(self, step: int) -> float:
        """Compute the backoff delay for repeat `step` (0-indexed).

        A1-D1: Full Jitter with floor clamp:
            sleep = random_between(30, min(120, 30 * 2^step))

        When delivery.jitter=off, returns the deterministic ladder 30/60/120/120.
        n=0 degenerates to exactly 30s (floor == ceiling at step 0).
        """
        from cli_agent_orchestrator.services.config_service import ConfigService

        jitter_mode = ConfigService.get("delivery.jitter", "on")

        if jitter_mode == "off":
            # Deterministic ladder: 30/60/120/120...
            if step < len(BACKOFF_SEQUENCE):
                return float(BACKOFF_SEQUENCE[step])
            return float(BACKOFF_CAP)

        # A1-D1: Floor-clamped Full Jitter
        # ceiling = min(cap, base * 2^step)
        ceiling = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** step))
        # floor = base (always 30s minimum spacing)
        floor = BACKOFF_BASE
        # n=0: ceiling = min(120, 30*1) = 30, floor = 30 → exactly 30s
        if floor >= ceiling:
            return float(floor)
        return float(self._rng.randint(floor, ceiling))

    # ------------------------------------------------------------------
    # Consumption acknowledgment: disarm on cursor advance
    # ------------------------------------------------------------------

    def on_cursor_advance(self, terminal_id: str, mailbox_id: str) -> None:
        """Called when consumption cursor advances — disarm any armed nudge.

        This is the eager path: if ack_messages runs between ticks, the nudge
        is immediately disarmed rather than waiting for fire-time revalidation.
        """
        with self._lock:
            state = self._states.get(terminal_id)
            if state is not None and state.mailbox_id == mailbox_id:
                del self._states[terminal_id]
                # Reset backoff for future nudges (D4: counter resets on consume)

    def disarm(self, terminal_id: str) -> None:
        """Explicitly disarm nudge state for a terminal (e.g., on terminal deletion)."""
        with self._lock:
            self._states.pop(terminal_id, None)

    # ------------------------------------------------------------------
    # Introspection (for testing)
    # ------------------------------------------------------------------

    def get_state(self, terminal_id: str) -> _NudgeState | None:
        """Return a snapshot of the nudge state for testing."""
        with self._lock:
            state = self._states.get(terminal_id)
            if state is None:
                return None
            # Return a copy to avoid races
            return _NudgeState(
                terminal_id=state.terminal_id,
                mailbox_id=state.mailbox_id,
                message_count=state.message_count,
                oldest_inbox_row_id=state.oldest_inbox_row_id,
                first_fired=state.first_fired,
                backoff_step=state.backoff_step,
                receive_count=state.receive_count,
                visibility_timeout_at=state.visibility_timeout_at,
                last_status=state.last_status,
                parked=state.parked,
                last_fired_at=state.last_fired_at,
            )

    def has_armed(self, terminal_id: str) -> bool:
        """Check if a nudge is armed for a terminal."""
        with self._lock:
            return terminal_id in self._states


@dataclass(frozen=True)
class NudgeFireIntent:
    """A nudge that has passed all D1/D2/D4 gates and should be typed into the pane."""

    terminal_id: str
    mailbox_id: str
    message_count: int
    oldest_inbox_row_id: int
    is_first: bool


# Type aliases for the callback functions
from typing import Callable

GetCursorFn = Callable[[str], int | None]
GetPendingFn = Callable[[str], tuple[int, int] | None]


# Module-level singleton
nudge_discipline = NudgeDiscipline()
