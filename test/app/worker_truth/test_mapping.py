"""The two status vocabularies, in both directions (WP-ARCH phase 2, D1).

``LEGACY_STATUS_MAP`` (phase 1) reads the legacy vocabulary in the new one.
``legacy_status`` (phase 2) is the direction the cutover actually publishes in,
and it is the one with a gap: the legacy map is not ONTO, so two of the seven
``WorkerState`` members have no legacy preimage and cannot round-trip.

The blueprint's §5b asks for a round-trip identity over all seven.  That
assertion is unsatisfiable rather than unimplemented, and writing it would pin a
falsehood about the legacy enum into the suite, so the round trip is scoped to
the five states with a preimage and the two lossy rows are asserted lossy BY
NAME — with the reason each lands where it does asserted too, because "lossy" is
not the interesting part.  WHERE it is lost to is: ``capped`` mapped to ``error``
would make the fork raise ``TerminalInputBlockedError`` on a worker that is
merely waiting out a usage window, and ``starting`` mapped to ``idle`` would let
the inbox paste into a booting one.

Importing ``models.terminal`` here is deliberate and is the only place it is
allowed: the pin has to be against the REAL enum, and the test lives on the
legacy side of the ``new-code-never-imports-legacy`` fence where that import is
legal.  The module under test carries the strings precisely so that it does not.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.app.worker_truth.mapping import (
    FORWARD_STATUS_MAP,
    LEGACY_STATUS_MAP,
    LOSSY_FORWARD_STATES,
    legacy_state,
    legacy_status,
)
from cli_agent_orchestrator.core.events import DecisionKind, EventKind
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState
from cli_agent_orchestrator.models.terminal import TerminalStatus

#: The five states ``LEGACY_STATUS_MAP``'s image covers.
ROUND_TRIPPABLE = [state for state in WorkerState if state not in LOSSY_FORWARD_STATES]


# --------------------------------------------------------------- the pin


def test_every_string_either_map_names_is_a_real_terminal_status() -> None:
    """The claim ``mapping.py``'s docstring makes, finally asserted.

    Both maps carry the legacy vocabulary as STRINGS so that ``app`` never
    imports ``models.terminal``.  That is only safe while a test on this side of
    the fence pins them: a legacy rename would otherwise leave the projection
    publishing a status no consumer has ever heard of, silently.
    """
    real = {status.value for status in TerminalStatus}

    assert set(LEGACY_STATUS_MAP) <= real
    assert set(FORWARD_STATUS_MAP.values()) <= real


def test_the_legacy_vocabulary_is_covered_in_both_directions() -> None:
    """Every legacy member is readable, and every one the projection can publish.

    The first half is phase 1's completeness.  The second is phase 2's: a legacy
    status no forward row produces is a status the cutover can never publish, and
    for ``completed`` and ``unknown`` — both conditional rows — that would be an
    easy thing to lose while refactoring the table.
    """
    real = {status.value for status in TerminalStatus}
    publishable = set(FORWARD_STATUS_MAP.values()) | {
        legacy_status(WorkerState.IDLE, causing_kind=EventKind.TURN_ENDED),
        legacy_status(WorkerState.DEGRADED, degraded_reason=DegradedReason.NO_SIGNAL),
    }

    assert set(LEGACY_STATUS_MAP) == real
    assert publishable == real


# --------------------------------------------------- the forward table


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (WorkerState.STARTING, "processing"),
        (WorkerState.IDLE, "idle"),
        (WorkerState.BUSY, "processing"),
        (WorkerState.AWAITING_INPUT, "waiting_user_answer"),
        (WorkerState.CAPPED, "processing"),
        (WorkerState.DEGRADED, "render_uncertain"),
        (WorkerState.EXITED, "error"),
    ],
)
def test_the_unconditional_row_for_every_state(state: WorkerState, expected: str) -> None:
    """All seven members, with no discriminator supplied.

    A sweep, a re-publish and a boot-time publish all reach the publisher with no
    causing kind, so the unconditional row is not a fallback for tests — it is a
    production path, and ``IDLE`` landing on ``idle`` rather than ``completed``
    there is the point: ``completed`` asserts that a turn has just finished.
    """
    assert legacy_status(state) == expected


def test_the_table_names_all_seven_states() -> None:
    assert set(FORWARD_STATUS_MAP) == set(WorkerState)


# ------------------------------------------------- the two discriminators


def test_idle_via_turn_ended_publishes_completed() -> None:
    """``completed`` is DERIVED at publish time, not held as a state.

    The fork uses ``completed`` for "the turn finished", not "the process ended",
    and ``agent_step``'s ``_CompletionOutcome.COMPLETED`` leg reads it.  The
    mutant this kills is an eighth ``WorkerState``: it would break the audit's
    frozen seven-member enum and its 49-cell table for a distinction no consumer
    reads as a state.
    """
    assert legacy_status(WorkerState.IDLE, causing_kind=EventKind.TURN_ENDED) == "completed"


@pytest.mark.parametrize(
    "kind",
    [
        EventKind.SESSION_RESUMED,
        EventKind.PANE_RECOVERED,
        EventKind.PROMPT_ANSWERED,
        DecisionKind.STATUS_TRANSITION,
        None,
    ],
)
def test_idle_reached_any_other_way_publishes_idle(kind: object) -> None:
    """A resume re-attached to a worker that is ready; no turn finished.

    The mutant is a publisher that reads ``IDLE`` alone and always says
    ``completed`` — which would announce a completed turn on every resume and
    every no-signal recovery.
    """
    assert legacy_status(WorkerState.IDLE, causing_kind=kind) == "idle"  # type: ignore[arg-type]


def test_degraded_splits_on_the_reason() -> None:
    assert (
        legacy_status(WorkerState.DEGRADED, degraded_reason=DegradedReason.NO_SIGNAL) == "unknown"
    )
    for reason in DegradedReason:
        if reason is DegradedReason.NO_SIGNAL:
            continue
        assert legacy_status(WorkerState.DEGRADED, degraded_reason=reason) == "render_uncertain"


def test_a_discriminator_that_does_not_apply_changes_nothing() -> None:
    """``turn.ended`` against ``BUSY`` is not a thing; neither is a reason on ``EXITED``.

    A publisher holds both discriminators for every call it makes, so the table
    has to ignore the one that does not belong to the row rather than let it
    leak — the mutant being a forward map keyed on the KIND instead of the state.
    """
    assert legacy_status(WorkerState.BUSY, causing_kind=EventKind.TURN_ENDED) == "processing"
    assert legacy_status(WorkerState.EXITED, degraded_reason=DegradedReason.NO_SIGNAL) == "error"


# ------------------------------------------------------- the round trip


@pytest.mark.parametrize("state", ROUND_TRIPPABLE)
def test_the_round_trip_is_the_identity_for_the_five_with_a_preimage(
    state: WorkerState,
) -> None:
    assert legacy_state(legacy_status(state)) is state


def test_the_completed_row_round_trips_too() -> None:
    """The conditional row must not be the one that loses the state.

    ``LEGACY_STATUS_MAP`` already reads ``completed`` as ``IDLE``, and the whole
    justification for deriving it at publish time rather than holding it is that
    the projection is unchanged by the derivation.
    """
    published = legacy_status(WorkerState.IDLE, causing_kind=EventKind.TURN_ENDED)

    assert published == "completed"
    assert legacy_state(published) is WorkerState.IDLE


def test_both_degraded_rows_round_trip() -> None:
    for reason in DegradedReason:
        assert legacy_state(legacy_status(WorkerState.DEGRADED, degraded_reason=reason)) is (
            WorkerState.DEGRADED
        )


# ------------------------------------------------------- the lossy pair


def test_exactly_two_states_have_no_legacy_preimage() -> None:
    """The §5b correction, stated as an assertion rather than as prose.

    Derived from ``LEGACY_STATUS_MAP`` rather than from the hand-written set, so
    a legacy map that grew a preimage for one of them would fail here and force
    the set to be revisited, instead of leaving a permanent exemption behind.
    """
    with_preimage = set(LEGACY_STATUS_MAP.values())

    assert set(WorkerState) - with_preimage == LOSSY_FORWARD_STATES
    assert LOSSY_FORWARD_STATES == {WorkerState.STARTING, WorkerState.CAPPED}


def test_starting_is_lost_to_busy_and_must_never_read_as_idle() -> None:
    """Lossy BY NAME, and lossy in the one direction that is safe.

    ``idle`` is the dangerous alternative: ``inbox_service``'s admission would
    paste a message into a worker that has not finished booting.
    """
    published = legacy_status(WorkerState.STARTING)

    assert published == "processing"
    assert legacy_state(published) is WorkerState.BUSY
    assert published != TerminalStatus.IDLE.value


def test_capped_is_lost_to_busy_and_must_never_read_as_error() -> None:
    """The cap's legacy carrier is the CONDITION LABEL, not the status.

    ``error`` maps back to ``EXITED`` and makes the fork raise
    ``TerminalInputBlockedError`` — on a worker that is alive and waiting out a
    usage window, which is the capped-lane policy's whole subject.
    """
    published = legacy_status(WorkerState.CAPPED)

    assert published == "processing"
    assert legacy_state(published) is WorkerState.BUSY
    assert published != TerminalStatus.ERROR.value
