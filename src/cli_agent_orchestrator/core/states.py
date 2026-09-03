"""Worker state vocabulary and the transition classifier (WP-ARCH phase 1, AC1).

The projector is an **observer of a foreign process**, not its authoriser.  That
single sentence (blueprint r9) decides the shape of everything here:
``validate()`` CLASSIFIES a transition and returns; it never rejects what a
worker actually did.  An impossible-looking arrival is applied anyway and
recorded as ``DIAG-BAD-TRANSITION`` with the offending cell, because the
alternative — dropping it — leaves the projection stale and silent, which is the
failure mode this whole work package exists to end.

Raising exists only for tests, behind ``CAO_WORKER_TRUTH_STRICT=1``.  The flag is
read per call rather than at import so a test may set it with ``monkeypatch`` and
have the very next call observe it.

``TRANSITIONS`` is the audit §3.1 table transcribed cell by cell.  All 49 cells
are present as explicit entries: an absent key would be a silent third
classification, and the table-driven test asserts completeness.
"""

from __future__ import annotations

import os
from enum import StrEnum

__all__ = [
    "DEGRADED_REASON_RANK",
    "STRICT_ENV_VAR",
    "TRANSITIONS",
    "DegradedReason",
    "TransitionClass",
    "WorkerState",
    "reason_rises",
    "strict_mode_enabled",
    "validate",
]

STRICT_ENV_VAR = "CAO_WORKER_TRUTH_STRICT"


class WorkerState(StrEnum):
    """The seven states a worker terminal can be projected into.

    ``DEGRADED`` replaces the fork's legacy ``RENDER_UNCERTAIN``/``UNKNOWN`` pair
    and always carries a :class:`DegradedReason`.
    """

    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    AWAITING_INPUT = "awaiting_input"
    CAPPED = "capped"
    DEGRADED = "degraded"
    EXITED = "exited"


class DegradedReason(StrEnum):
    """Closed enum of why a terminal is degraded (blueprint B8).

    Closed is the point: the capped-lane policy (F681 D3) reads EVERY member as
    ``Unknown``, so a new reason can never leak past the policy as a
    normal-looking state.  A clean read with no banner is ``IDLE``, never
    ``DEGRADED``.
    """

    RENDER_UNCERTAIN = "render_uncertain"
    PANE_UNREADABLE = "pane_unreadable"
    NO_SIGNAL = "no_signal"
    PRODUCER_ERROR = "producer_error"
    ROLLOUT_MISSING = "rollout_missing"
    CONFLICTING_SOURCES = "conflicting_sources"


# Blueprint AC6 rule (a): a degraded -> degraded arrival is a no-op UNLESS the
# incoming reason outranks the standing one, in which case the projector appends
# ``status.reason_changed``.  Higher number = more severe.  The order is the
# blueprint's, verbatim:
#   producer_error > rollout_missing > conflicting_sources
#                  > pane_unreadable > render_uncertain > no_signal
DEGRADED_REASON_RANK: dict[DegradedReason, int] = {
    DegradedReason.PRODUCER_ERROR: 6,
    DegradedReason.ROLLOUT_MISSING: 5,
    DegradedReason.CONFLICTING_SOURCES: 4,
    DegradedReason.PANE_UNREADABLE: 3,
    DegradedReason.RENDER_UNCERTAIN: 2,
    DegradedReason.NO_SIGNAL: 1,
}


class TransitionClass(StrEnum):
    """How the transition authority classifies one (from, to) pair.

    ``ALLOWED``    — a normal edge; the projector applies it and appends
                     ``status.transition``.
    ``NO_OP``      — the diagonal; same-state re-entry keeps ``since``, advances
                     ``last_event_seq``, and appends no transition row.
    ``ANOMALOUS``  — a cell the state machine has no edge for.  Still applied,
                     and counted as ``DIAG-BAD-TRANSITION``.
    """

    ALLOWED = "Allowed"
    NO_OP = "NoOp"
    ANOMALOUS = "Anomalous"


_A = TransitionClass.ALLOWED
_N = TransitionClass.NO_OP
_X = TransitionClass.ANOMALOUS

# Audit §3.1 table.  Row = from-state, column = to-state, reading order:
#   starting, idle, busy, awaiting_input, capped, degraded, exited
#
#   starting        | =  ✔  ✔  ✔  ✔  ✔  ✔
#   idle            | -  =  ✔  ✔  ✔  ✔  ✔
#   busy            | -  ✔  =  ✔  ✔  ✔  ✔
#   awaiting_input  | -  ✔  ✔  =  ✔  ✔  ✔
#   capped          | -  ✔  -  -  =  ✔  ✔
#   degraded        | -  ✔  ✔  ✔  ✔  =  ✔
#   exited          | ✔  -  -  -  -  -  =
#
# Two shapes are worth naming because they are easy to "fix" by accident:
#   * Nothing but a respawn re-enters ``starting`` — every other arrival there is
#     anomalous, which is how a mis-attributed launch shows up rather than hides.
#   * ``capped`` may only go to ``idle``, ``degraded`` or ``exited``.  A rollout
#     that reports ``capped -> busy`` is applied AND flagged; that exact pair is
#     the AC6 test case.
_ORDER: tuple[WorkerState, ...] = (
    WorkerState.STARTING,
    WorkerState.IDLE,
    WorkerState.BUSY,
    WorkerState.AWAITING_INPUT,
    WorkerState.CAPPED,
    WorkerState.DEGRADED,
    WorkerState.EXITED,
)

_ROWS: dict[WorkerState, tuple[TransitionClass, ...]] = {
    WorkerState.STARTING: (_N, _A, _A, _A, _A, _A, _A),
    WorkerState.IDLE: (_X, _N, _A, _A, _A, _A, _A),
    WorkerState.BUSY: (_X, _A, _N, _A, _A, _A, _A),
    WorkerState.AWAITING_INPUT: (_X, _A, _A, _N, _A, _A, _A),
    WorkerState.CAPPED: (_X, _A, _X, _X, _N, _A, _A),
    WorkerState.DEGRADED: (_X, _A, _A, _A, _A, _N, _A),
    WorkerState.EXITED: (_A, _X, _X, _X, _X, _X, _N),
}

TRANSITIONS: dict[tuple[WorkerState, WorkerState], TransitionClass] = {
    (from_state, to_state): classification
    for from_state, row in _ROWS.items()
    for to_state, classification in zip(_ORDER, row, strict=True)
}


def strict_mode_enabled() -> bool:
    """True when ``CAO_WORKER_TRUTH_STRICT=1`` is set in the environment.

    Read per call, never cached, so a test can flip it mid-process.
    """
    return os.environ.get(STRICT_ENV_VAR) == "1"


def validate(from_state: WorkerState, to_state: WorkerState) -> TransitionClass:
    """Classify a transition as ``Allowed``, ``NoOp`` or ``Anomalous``.

    Never raises in production, whatever the pair.  Under
    ``CAO_WORKER_TRUTH_STRICT=1`` an ``Anomalous`` cell raises ``ValueError`` so a
    test can assert the classifier is consulted at all — the mutant "transition
    table loosened" is otherwise invisible to a caller that ignores the return.
    """
    classification = TRANSITIONS[(from_state, to_state)]
    if classification is TransitionClass.ANOMALOUS and strict_mode_enabled():
        raise ValueError(
            f"anomalous worker transition {from_state.value} -> {to_state.value} "
            f"({STRICT_ENV_VAR}=1)"
        )
    return classification


def reason_rises(current: DegradedReason | None, incoming: DegradedReason) -> bool:
    """True when ``incoming`` outranks ``current`` under the fixed severity order.

    The projector uses this for AC6 rule (a): a ``degraded -> degraded`` arrival
    appends ``status.reason_changed`` only when the reason RISES; an equal or
    lower reason is a plain no-op.  A ``current`` of ``None`` means the terminal
    was not degraded, so any reason rises.
    """
    if current is None:
        return True
    return DEGRADED_REASON_RANK[incoming] > DEGRADED_REASON_RANK[current]
