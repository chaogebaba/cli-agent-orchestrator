"""Turn-end truth for F792 (#649): a narrow hook-truth marker read by fuse_status.

OPTION 1 of the #649 fix (per supervisor): a minimal, in-process turn-boundary
marker that mirrors :mod:`question_state`. It records whether a claude_code
seat's LAST turn-boundary hook event was a turn-END (``Stop``) that is newer than
its last turn-START (``UserPromptSubmit`` / ``PreToolUse``). While turn-ended,
the seat is idle at a turn boundary regardless of what the pane repaints, so
``fuse_status`` reads this flag and refuses to let pane-delta churn upgrade the
seat to PROCESSING (a background Agent animating the pane cannot flip the seat to
``working``).

Precedence, as AC3 states it: a ``Stop`` newer than the last ``PostToolUse`` pins
the seat non-busy until the next ``PreToolUse`` / ``UserPromptSubmit``. We realise
that with two edges only:

* ``mark_turn_ended`` — the ``Stop`` hook fired; the turn is over. (``PostToolUse``
  is deliberately NOT an edge here: a ``Stop`` AFTER a ``PostToolUse`` must keep
  the seat ended, which is exactly "ended stays set until a fresh turn start".)
* ``mark_turn_active`` — a ``UserPromptSubmit`` or a ``PreToolUse`` fired; a new
  turn has begun, so the seat is no longer at a turn-end boundary.

``is_turn_ended(tid)`` is the single read the fusion helper consumes.

This is a HOT-FIX SEAM. The modular-core blueprint's D8 (the worker-truth event
log becoming the authority for seat busy/idle) RETIRES this marker: once seat
status is projected from the worker-truth log, ``fuse_status`` reads turn-end
truth from there and this module is deleted. It is deliberately small so that
removal is a clean subtraction.

State is in-process (module singleton, like ``question_state`` /
``status_monitor``); turn markers are ephemeral runtime truth, not durable rows.
Nothing here is claude-specific on the wire — a future provider can drive the
same two edges.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

# The two closed edge kinds accepted on the wire (provider-agnostic).
TURN_ENDED = "turn_ended"
TURN_ACTIVE = "turn_active"


@dataclass
class _TurnState:
    # True iff the last turn-boundary edge was a turn-END newer than any
    # turn-START. Defaults False: a terminal with no recorded edge is treated as
    # "not known to be ended", so fuse_status applies no turn-end veto (the
    # pre-F792 behaviour) until a Stop hook actually fires.
    ended: bool = False
    # monotonic timestamp of the edge that set the CURRENT value (diagnostics).
    at: float | None = None


@dataclass
class TurnStateService:
    """Owns turn-end truth for all terminals; process singleton."""

    _clock: Callable[[], float] = time.monotonic
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _states: dict[str, _TurnState] = field(default_factory=dict)

    # ---- edge input (endpoint) -------------------------------------------
    def mark(self, terminal_id: str, kind: str, *, now: float | None = None) -> None:
        """Apply one turn-boundary edge. Unknown kinds are ignored (no error).

        ``turn_ended`` sets the flag; ``turn_active`` clears it. Idempotent — a
        repeat of the same edge only refreshes the timestamp.
        """
        now = self._clock() if now is None else now
        if kind == TURN_ENDED:
            ended = True
        elif kind == TURN_ACTIVE:
            ended = False
        else:
            return
        with self._lock:
            self._states[terminal_id] = _TurnState(ended=ended, at=now)

    # ---- read (fusion) ----------------------------------------------------
    def is_turn_ended(self, terminal_id: str) -> bool:
        """True iff the seat's last turn-boundary edge was a turn-END.

        The single read ``fuse_status`` consumes. A terminal with no recorded
        edge returns False (no veto — pre-F792 behaviour).
        """
        with self._lock:
            state = self._states.get(terminal_id)
            return bool(state and state.ended)

    def forget(self, terminal_id: str) -> None:
        """Drop all turn state for a terminal (teardown)."""
        with self._lock:
            self._states.pop(terminal_id, None)


turn_state = TurnStateService()

__all__ = ["TurnStateService", "turn_state", "TURN_ENDED", "TURN_ACTIVE"]
