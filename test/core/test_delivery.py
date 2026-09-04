"""The pure delivery domain (WP-ARCH phase 3a, F728 #584).

No database anywhere in this file, which is the point of putting the boot guard
and the deadline in ``core``: the two pieces of phase-3 logic most likely to be
got wrong are decidable from values alone, so they are tested by enumerating the
space rather than by contriving a server state and hoping the case was reached.

Three of the phase's named mutants have their killer here:

* recomputing ``dead_by`` from the current ``available_at`` (D12) — the arithmetic
  half; the store half is in ``test/adapters/test_queue_store.py``;
* the boot guard counting SHADOW rows as occupancy (B16), which would resolve a
  bounced shadow deployment to ``drain``;
* dropping ``expire_after_s`` from the deadline (D8/B22), which would make every
  expiring message non-expiring.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from cli_agent_orchestrator.core.delivery import (
    TERMINAL_STATES,
    AttemptOutcome,
    DeadReason,
    MsgState,
    QueueOccupancy,
    SwitchPosition,
    compute_dead_by,
    parse_switch,
    resolve_switch,
)
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.timing import (
    DELIVERY_BACKOFF_S,
    DELIVERY_DEDUP_WINDOW_S,
    DELIVERY_INJECT_BUDGET_S,
    DELIVERY_LEASE_S,
    DELIVERY_MAX_ATTEMPTS,
    DELIVERY_MAX_LIFETIME_S,
    DELIVERY_TICK_S,
    DELIVERY_VETO_CEILING_S,
    IDLE_STALL_AGE_S,
    PANE_HEARTBEAT_S,
    check_delivery_orderings,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------- §5c


def test_the_five_ordering_invariants_hold_at_the_shipped_constants() -> None:
    """I1 through I5, recomputed from the table rather than restated."""
    check_delivery_orderings()

    attempt_span = (DELIVERY_LEASE_S + DELIVERY_BACKOFF_S) * DELIVERY_MAX_ATTEMPTS
    assert DELIVERY_TICK_S < DELIVERY_LEASE_S
    assert PANE_HEARTBEAT_S < DELIVERY_LEASE_S
    assert attempt_span < IDLE_STALL_AGE_S
    assert attempt_span < DELIVERY_VETO_CEILING_S < DELIVERY_MAX_LIFETIME_S < IDLE_STALL_AGE_S
    assert DELIVERY_INJECT_BUDGET_S < DELIVERY_LEASE_S


def test_idle_stall_age_mirrors_the_legacy_constant_it_is_derived_from() -> None:
    """The one constant ``core`` cannot import, so drift is caught here instead.

    I3 and I4 both bound quantities against ``IDLE_STALL_AGE``, whose real
    definition is in ``services/inbox_service.py``.  ``core`` may not import
    legacy, so the value is mirrored — and a legacy retune that moved it without
    moving the mirror would leave two invariants passing against a number the
    server no longer uses.  This test is the whole defence against that.
    """
    from cli_agent_orchestrator.services.inbox_service import IDLE_STALL_AGE

    assert IDLE_STALL_AGE == IDLE_STALL_AGE_S


def test_the_dedup_window_mirrors_the_legacy_f475_window() -> None:
    """Same argument as the stall age: a silent divergence changes deliveries.

    D13 reproduces F475 as written, so the queue's window and legacy's must be
    one number.  If they part, the two paths deduplicate differently and the
    migration that claims to preserve behaviour does not.
    """
    from cli_agent_orchestrator.clients.database import _F475_CALLBACK_DEDUP_WINDOW_S

    assert _F475_CALLBACK_DEDUP_WINDOW_S == DELIVERY_DEDUP_WINDOW_S


def test_the_attempt_span_is_over_the_lease_and_includes_the_backoff() -> None:
    """The r2 and r4 defects, as a test rather than as a changelog row.

    r2 multiplied the TICK, which admitted a 600-second lease reaching 3000 s
    while still passing.  r4 omitted the backoff, so a delay the design itself
    introduced sat outside the invariant meant to bound it.  Both forms are
    computed here and only the shipped one is asserted to be the invariant's
    subject.
    """
    over_tick = DELIVERY_TICK_S * DELIVERY_MAX_ATTEMPTS
    without_backoff = DELIVERY_LEASE_S * DELIVERY_MAX_ATTEMPTS
    shipped = (DELIVERY_LEASE_S + DELIVERY_BACKOFF_S) * DELIVERY_MAX_ATTEMPTS

    assert shipped > without_backoff > over_tick
    assert shipped < IDLE_STALL_AGE_S


# ------------------------------------------------------------------ D9 guard


@pytest.mark.parametrize(
    ("requested", "occupied", "expected", "finding"),
    [
        (SwitchPosition.OFF, False, SwitchPosition.OFF, None),
        (SwitchPosition.OFF, True, SwitchPosition.DRAIN, FindingCode.DIAG_QUEUE_ORPHAN_GUARD),
        (SwitchPosition.SHADOW, False, SwitchPosition.SHADOW, None),
        (SwitchPosition.SHADOW, True, SwitchPosition.DRAIN, FindingCode.DIAG_QUEUE_ORPHAN_GUARD),
        (SwitchPosition.ON, False, SwitchPosition.ON, None),
        (SwitchPosition.ON, True, SwitchPosition.ON, None),
        (SwitchPosition.DRAIN, False, SwitchPosition.SHADOW, FindingCode.DIAG_QUEUE_ORPHAN_GUARD),
        (SwitchPosition.DRAIN, True, SwitchPosition.DRAIN, None),
    ],
)
def test_the_boot_guard_table_is_total_over_positions_and_conditions(
    requested: SwitchPosition,
    occupied: bool,
    expected: SwitchPosition,
    finding: FindingCode | None,
) -> None:
    """All eight cells of D9's table, enumerated.

    Written as a parametrised enumeration rather than as a handful of examples
    because the guard's defect class is a MISSING cell, not a wrong one: the
    blueprint went three rounds with the guard cited in three places and defined
    in none, and what closed it was making the function total.
    """
    outcome = resolve_switch(requested, QueueOccupancy(live_non_terminal=3 if occupied else 0))
    assert outcome.position is expected
    assert outcome.finding is finding


def test_the_table_covers_every_position_and_both_conditions() -> None:
    """No position is unhandled, whatever is added to the enum later."""
    for requested, occupied in itertools.product(SwitchPosition, (False, True)):
        outcome = resolve_switch(requested, QueueOccupancy(live_non_terminal=1 if occupied else 0))
        assert isinstance(outcome.position, SwitchPosition)


def test_a_finished_queue_does_not_pin_the_server_in_drain() -> None:
    """ "Non-empty" means non-TERMINAL rows, not merely rows.

    Counting any row would leave a deployment that had ever delivered anything
    pinned in ``drain`` for the rest of its life, with the delivery machinery
    running over a queue that has nothing left to deliver.
    """
    assert QueueOccupancy(live_non_terminal=0).occupied is False
    assert resolve_switch(SwitchPosition.OFF, QueueOccupancy(0)).position is SwitchPosition.OFF


def test_shadow_rows_are_not_occupancy_which_is_the_bounce_case() -> None:
    """B16, and AC-3a's second case, at the level the rule is written.

    A bounced sub-phase 3a deployment holds in-flight SHADOW rows.  If those
    counted as occupancy the guard would resolve ``shadow`` to ``drain``, whose
    tick would then inject copies of messages the legacy path already delivered
    — a second carrier over one id, which is #506 reproduced by the guard added
    to prevent loss, in the first sub-phase to ship.

    The occupancy value here is what a store computes over ``mode='live'`` rows
    only, so a queue holding nothing but shadow rows presents zero.
    """
    bounced_shadow_deployment = QueueOccupancy(live_non_terminal=0)
    outcome = resolve_switch(SwitchPosition.SHADOW, bounced_shadow_deployment)
    assert outcome.position is SwitchPosition.SHADOW
    assert outcome.finding is None
    assert outcome.demoted is False


def test_an_open_barrier_holds_the_flip_at_shadow() -> None:
    """D9's second predicate.

    D13 makes every barrier opened AFTER the flip associate normally, so this
    covers the one case association cannot: a barrier already open at the moment
    of the flip, whose members would otherwise be split across the legacy inbox
    and the queue.
    """
    outcome = resolve_switch(
        SwitchPosition.ON, QueueOccupancy(live_non_terminal=0, open_barrier_labels=("gate-r4",))
    )
    assert outcome.position is SwitchPosition.SHADOW
    assert outcome.finding is FindingCode.DIAG_BARRIER_OPEN_AT_FLIP
    assert "gate-r4" in outcome.detail


def test_the_barrier_predicate_only_guards_the_flip() -> None:
    """An open barrier is ordinary while the queue is not being served.

    Guarding ``off`` or ``shadow`` on a barrier would demote a deployment for a
    condition that cannot affect it: nothing is served from the queue there, so
    no member can be split across two tables.
    """
    occupancy = QueueOccupancy(live_non_terminal=0, open_barrier_labels=("gate-r4",))
    assert resolve_switch(SwitchPosition.OFF, occupancy).position is SwitchPosition.OFF
    assert resolve_switch(SwitchPosition.SHADOW, occupancy).position is SwitchPosition.SHADOW


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, SwitchPosition.OFF),
        ("", SwitchPosition.OFF),
        ("off", SwitchPosition.OFF),
        (" SHADOW ", SwitchPosition.SHADOW),
        ("Drain", SwitchPosition.DRAIN),
        ("on", SwitchPosition.ON),
        ("true", SwitchPosition.OFF),
        ("1", SwitchPosition.OFF),
        ("yes", SwitchPosition.OFF),
    ],
)
def test_an_unreadable_switch_value_means_off(raw: str | None, expected: SwitchPosition) -> None:
    """Unknown means ``off``, and the guard's finding is what tells the operator.

    Guessing at an intended position would be a worse failure than the default,
    because the default is the safe one.  ``"1"`` is in the table deliberately:
    it is the phase-1 switch's spelling, and an operator who copied that habit
    across must get ``off`` rather than a queue.
    """
    assert parse_switch(raw) is expected


# ---------------------------------------------------------------- D12 dead_by


def test_the_deadline_is_the_lifetime_from_creation() -> None:
    assert compute_dead_by(created_at=NOW, available_at=NOW) == NOW + timedelta(
        seconds=DELIVERY_MAX_LIFETIME_S
    )


def test_a_caller_expiry_can_only_bring_death_forward() -> None:
    """D8, and B22's defect in its killable form.

    Carrying only ``supersede_key`` and not ``expire_after_s`` would have made
    every expiring message non-expiring at the flip, delivered up to
    ``DELIVERY_MAX_LIFETIME_S`` late — which is #435's incident reintroduced by
    the phase that closes it.  Both directions are asserted: a short expiry
    shortens, and an absurdly long one does NOT extend.
    """
    short = compute_dead_by(created_at=NOW, available_at=NOW, expire_after_s=30)
    assert short == NOW + timedelta(seconds=30)

    longer_than_the_lifetime = compute_dead_by(
        created_at=NOW, available_at=NOW, expire_after_s=DELIVERY_MAX_LIFETIME_S * 10
    )
    assert longer_than_the_lifetime == NOW + timedelta(seconds=DELIVERY_MAX_LIFETIME_S)


def test_a_nonpositive_expiry_is_ignored_rather_than_killing_the_row_at_birth() -> None:
    """``expire_after_s=0`` reaching the column would dead-letter on arrival.

    The parameter is caller-supplied through a public MCP surface, so a zero or
    a negative is reachable from outside the server.  Treating it as "no expiry"
    is the reading that cannot lose a message; the alternative is a row that is
    already past its deadline before anything could deliver it.
    """
    for value in (0, -1):
        assert compute_dead_by(created_at=NOW, available_at=NOW, expire_after_s=value) == (
            NOW + timedelta(seconds=DELIVERY_MAX_LIFETIME_S)
        )


def test_the_max_term_is_defensive_and_never_shortens_the_life() -> None:
    """R1 holds ``available_at == created_at``, so the term is inert in phase 3.

    It is present so a FUTURE delayed enqueue could not kill a message before it
    is due.  The test states both halves: with the two equal the term does
    nothing, and with a delay it moves the origin forward rather than back.
    """
    delayed = NOW + timedelta(seconds=300)
    assert compute_dead_by(created_at=NOW, available_at=delayed) == delayed + timedelta(
        seconds=DELIVERY_MAX_LIFETIME_S
    )
    # And the property R1 exists to protect: at 300 s of delay the row would
    # outlive IDLE_STALL_AGE, which is why a phase introducing a delay must add
    # DELIVERY_MAX_ENQUEUE_DELAY_S and re-derive I4 before it ships.
    life_from_creation = (
        compute_dead_by(created_at=NOW, available_at=delayed) - NOW
    ).total_seconds()
    assert life_from_creation > IDLE_STALL_AGE_S


def test_recomputing_the_deadline_from_a_moved_availability_extends_it() -> None:
    """The named mutant, shown to be a real extension rather than a nicety.

    ``reclaim`` adds ``DELIVERY_BACKOFF_S`` to ``available_at`` on every
    re-offer.  A deadline recomputed from the current value therefore moves
    forward once per attempt, and a row that keeps failing never dies.  This
    test does not prove the store avoids it — that is
    ``test_queue_store.py::test_reclaim_never_moves_the_deadline`` — it proves
    the mutant is worth a test at all.
    """
    stamped = compute_dead_by(created_at=NOW, available_at=NOW)
    after_five_reoffers = compute_dead_by(
        created_at=NOW, available_at=NOW + timedelta(seconds=DELIVERY_BACKOFF_S * 5)
    )
    assert after_five_reoffers > stamped


# ------------------------------------------------------------- vocabularies


def test_the_terminal_set_is_i1s_three_states() -> None:
    assert TERMINAL_STATES == {MsgState.DELIVERED, MsgState.SUPERSEDED, MsgState.DEAD}
    assert MsgState.READY not in TERMINAL_STATES
    assert MsgState.LEASED not in TERMINAL_STATES


def test_a_caller_expiry_is_a_reason_not_a_fourth_state() -> None:
    """I1 fixes three terminal states; D8 adds a reason, not a state."""
    assert DeadReason.EXPIRED in set(DeadReason)
    assert len(TERMINAL_STATES) == 3


def test_legacy_other_is_the_only_shadow_only_attempt_outcome() -> None:
    """3b's vocabulary is D12's four; ``legacy_other`` never appears in it.

    Recorded as a test because the value's whole justification is that it is
    temporary: it exists so 3a can mirror a legacy outcome faithfully instead of
    forcing it onto ``veto_dialog`` and inventing a dialog gate that was never
    consulted.  A live row carrying it would mean the write-through path had
    started borrowing legacy's vocabulary.
    """
    live_vocabulary = set(AttemptOutcome) - {AttemptOutcome.LEGACY_OTHER}
    assert live_vocabulary == {
        AttemptOutcome.DELIVERED,
        AttemptOutcome.VETO_DIALOG,
        AttemptOutcome.VETO_UNVERIFIED,
        AttemptOutcome.PANE_ABSENT,
    }
