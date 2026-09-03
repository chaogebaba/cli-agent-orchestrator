"""The mirror writer's decisions (WP-ARCH phase 3a, F728 #584).

Every mapping cell is a judgement, and a wrong one does not fail — it quietly
makes AC-3a's agreement rate mean something else.  So each cell is asserted here
with the reason it was chosen, and the two DELIBERATE absences get tests of their
own, because an absence is exactly what a later reader is most likely to
"fix" without knowing why it was left out.

Two of the phase's named mutants are killed here: the enqueue raising into its
caller (§7a's wiring rule), and a veto being dropped rather than recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from test.app.delivery.conftest import FakeClock, InMemoryQueueStore

import pytest

from cli_agent_orchestrator.app.delivery.facts import (
    LegacyAttempt,
    LegacyEnqueue,
    LegacyOutcome,
    LegacyVeto,
)
from cli_agent_orchestrator.app.delivery.mirror import (
    LEGACY_TERMINAL_MAP,
    MirrorWriter,
    shadow_idempotency_key,
)
from cli_agent_orchestrator.core.delivery import (
    AttemptOutcome,
    DeadReason,
    MsgKind,
    MsgState,
    QueueMode,
)

AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def mirror(store: InMemoryQueueStore, clock: FakeClock) -> MirrorWriter:
    return MirrorWriter(store, clock)


def enqueue_fact(legacy_id: int = 41, status: str = "pending", **extra: object) -> LegacyEnqueue:
    fields: dict[str, object] = {
        "legacy_message_id": legacy_id,
        "receiver_id": "mb_supervisor",
        "sender_id": "t-worker",
        "message": "a steer",
        "status": status,
        "created_at": AT,
        "orchestration_type": "send_message",
    }
    fields.update(extra)
    return LegacyEnqueue(**fields)  # type: ignore[arg-type]


# ------------------------------------------------------------------ enqueue


def test_the_shadow_row_carries_mode_shadow_and_the_legacy_id(
    mirror: MirrorWriter, store: InMemoryQueueStore
) -> None:
    row = mirror.enqueue(enqueue_fact())
    assert row.mode is QueueMode.SHADOW
    assert row.legacy_message_id == 41
    assert row.idempotency_key == shadow_idempotency_key(41)
    assert row.state is MsgState.READY


def test_the_key_namespace_cannot_collide_with_a_live_callers_key(
    mirror: MirrorWriter,
) -> None:
    """3b's keys are caller-supplied; a prefix keeps the two disjoint.

    Without it a caller who happened to send ``"41"`` as an idempotency key after
    the flip would collide with a shadow row from 3a and receive that row's id
    for a message that was never enqueued.
    """
    assert shadow_idempotency_key(41).startswith("legacy-inbox:")
    assert shadow_idempotency_key(41) != "41"


def test_observing_one_insert_twice_writes_one_row(
    mirror: MirrorWriter, store: InMemoryQueueStore
) -> None:
    mirror.enqueue(enqueue_fact())
    mirror.enqueue(enqueue_fact())
    assert store.count() == 1


def test_a_row_born_superseded_is_settled_at_enqueue(
    mirror: MirrorWriter, store: InMemoryQueueStore
) -> None:
    """F578's supersession runs inside the same insert the hook observes.

    Waiting for a later edge would leave that row ``ready`` forever, since
    nothing else will ever touch it, and it would read as a ``legacy_early``
    disagreement for the life of the queue.
    """
    row = mirror.enqueue(enqueue_fact(status="superseded"))
    stored = store.get(row.msg_id)
    assert stored is not None and stored.state is MsgState.SUPERSEDED


def test_the_kind_follows_the_facts_callback_flag(mirror: MirrorWriter) -> None:
    """The mirror maps the flag; deciding it is the collector's job.

    Which rows are callbacks is a legacy question — it turns on
    ``callback_dedup_key``, which legacy sets only on the F475-eligible path —
    so it is decided in ``services/delivery_mirror.py`` and asserted there.  Here
    the only claim is that the flag reaches the stored row's ``kind``.
    """
    assert mirror.enqueue(enqueue_fact(42, is_callback=True)).kind is MsgKind.CALLBACK
    assert mirror.enqueue(enqueue_fact(43)).kind is MsgKind.NOTE


def test_the_callers_expiry_reaches_the_deadline(mirror: MirrorWriter) -> None:
    row = mirror.enqueue(enqueue_fact(expire_after_s=45))
    assert (row.dead_by - row.created_at).total_seconds() == 45


# ------------------------------------------------------ the terminal mapping


@pytest.mark.parametrize(
    ("legacy_status", "expected_state", "expected_reason"),
    [
        ("delivered", MsgState.DELIVERED, None),
        ("digested", MsgState.DELIVERED, None),
        ("superseded", MsgState.SUPERSEDED, None),
        ("cancelled", MsgState.SUPERSEDED, None),
        ("expired", MsgState.DEAD, DeadReason.EXPIRED),
        ("failed", MsgState.DEAD, DeadReason.MAX_ATTEMPTS),
    ],
)
def test_every_terminal_legacy_status_maps_to_one_of_i1s_three_states(
    mirror: MirrorWriter,
    store: InMemoryQueueStore,
    legacy_status: str,
    expected_state: MsgState,
    expected_reason: DeadReason | None,
) -> None:
    row = mirror.enqueue(enqueue_fact(1))
    mirror.observe(LegacyOutcome(legacy_message_id=1, status=legacy_status))

    stored = store.get(row.msg_id)
    assert stored is not None and stored.state is expected_state
    if expected_reason is not None:
        dead = store.dead_letter(row.msg_id)
        assert dead is not None and dead.reason is expected_reason


@pytest.mark.parametrize(
    "in_flight", ["pending", "held", "delivering", "parked", "delivery_failed"]
)
def test_an_in_flight_legacy_status_leaves_the_shadow_row_ready(
    mirror: MirrorWriter, store: InMemoryQueueStore, in_flight: str
) -> None:
    """``delivery_failed`` is the one that matters, and it is why this is a test.

    Legacy RETRIES a ``delivery_failed`` row, so despite the word it is an
    in-flight state.  Treating it as terminal would report every redelivery as a
    loss and would put rows in the dead-letter table that legacy went on to
    deliver.
    """
    row = mirror.enqueue(enqueue_fact(1))
    mirror.observe(LegacyOutcome(legacy_message_id=1, status=in_flight))
    stored = store.get(row.msg_id)
    assert stored is not None and stored.state is MsgState.READY
    assert in_flight not in LEGACY_TERMINAL_MAP


def test_a_cancel_reason_is_preserved_even_though_the_state_is_superseded(
    mirror: MirrorWriter, store: InMemoryQueueStore
) -> None:
    """The queue's four dead reasons are closed, so legacy's rides in an attempt.

    Widening ``DeadReason`` to hold ``terminal_reaped_no_surviving_ancestor``
    would put a legacy concept in the shipped enum, and the enum is what 3b's
    dead-letter table means.
    """
    row = mirror.enqueue(enqueue_fact(1))
    mirror.observe(
        LegacyOutcome(
            legacy_message_id=1,
            status="cancelled",
            failure_reason="terminal_reaped_no_surviving_ancestor",
        )
    )
    details = " ".join(a.detail for a in store.attempts_for(row.msg_id))
    assert "terminal_reaped_no_surviving_ancestor" in details


def test_the_first_terminal_observation_wins(
    mirror: MirrorWriter, store: InMemoryQueueStore
) -> None:
    """Several legacy writers can end a row and they do not fire in order.

    A late edge rewriting a recorded outcome would make the agreement report
    measure arrival order instead of agreement.
    """
    row = mirror.enqueue(enqueue_fact(1))
    mirror.observe(LegacyOutcome(legacy_message_id=1, status="delivered"))
    mirror.observe(LegacyOutcome(legacy_message_id=1, status="expired"))
    stored = store.get(row.msg_id)
    assert stored is not None and stored.state is MsgState.DELIVERED


# ------------------------------------------------------- the attempt mapping


def test_a_confirmed_attempt_is_a_delivery(mirror: MirrorWriter, store: InMemoryQueueStore) -> None:
    row = mirror.enqueue(enqueue_fact(1))
    mirror.observe(
        LegacyOutcome(
            legacy_message_id=1,
            status="delivered",
            attempts=(LegacyAttempt(ordinal=1, outcome="confirmed", started_at=AT),),
        )
    )
    stored = store.attempts_for(row.msg_id)
    assert stored[0].outcome is AttemptOutcome.DELIVERED
    assert stored[0].claim_id == 1


@pytest.mark.parametrize("reason", ["pane_unresolvable", "proven_absent", "receiver_metadata_gone"])
def test_an_interrupted_attempt_with_a_pane_reason_is_pane_absent(
    mirror: MirrorWriter, store: InMemoryQueueStore, reason: str
) -> None:
    """D12's ``pane_absent``, which is the outcome that spends the attempt budget.

    Getting this cell wrong in either direction is expensive: too broad and a
    dialog hold spends a budget it should not, too narrow and a receiver that is
    genuinely gone never reaches ``delivery_dead``.
    """
    row = mirror.enqueue(enqueue_fact(1))
    mirror.observe(
        LegacyOutcome(
            legacy_message_id=1,
            status="pending",
            attempts=(
                LegacyAttempt(ordinal=1, outcome="interrupted", started_at=AT, reason=reason),
            ),
        )
    )
    assert store.attempts_for(row.msg_id)[0].outcome is AttemptOutcome.PANE_ABSENT


@pytest.mark.parametrize("outcome", ["deferred", "ambiguous", "unresolved", "programming_error"])
def test_an_unmappable_legacy_outcome_is_recorded_verbatim_not_forced(
    mirror: MirrorWriter, store: InMemoryQueueStore, outcome: str
) -> None:
    """The alternative would corrupt the comparison 3a exists to make.

    Mapping ``deferred`` onto ``veto_dialog`` invents a dialog gate that was
    never consulted; mapping it onto ``pane_absent`` invents a missing pane.
    Recording it as ``legacy_other`` with the strings intact is the only reading
    that neither loses the fact nor fabricates one.
    """
    row = mirror.enqueue(enqueue_fact(1))
    mirror.observe(
        LegacyOutcome(
            legacy_message_id=1,
            status="pending",
            attempts=(
                LegacyAttempt(ordinal=1, outcome=outcome, started_at=AT, reason="something"),
            ),
        )
    )
    stored = store.attempts_for(row.msg_id)[0]
    assert stored.outcome is AttemptOutcome.LEGACY_OTHER
    assert outcome in stored.detail
    assert "something" in stored.detail


def test_the_ordinal_becomes_the_claim_id_so_re_observation_is_a_no_op(
    mirror: MirrorWriter, store: InMemoryQueueStore
) -> None:
    row = mirror.enqueue(enqueue_fact(1))
    attempts = (
        LegacyAttempt(ordinal=1, outcome="interrupted", started_at=AT),
        LegacyAttempt(ordinal=2, outcome="confirmed", started_at=AT),
    )
    outcome = LegacyOutcome(legacy_message_id=1, status="pending", attempts=attempts)
    mirror.observe(outcome)
    mirror.observe(outcome)
    assert [a.claim_id for a in store.attempts_for(row.msg_id)] == [1, 2]


# ------------------------------------------------------------------- vetoes


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("waiting_gate", AttemptOutcome.VETO_DIALOG),
        ("dialog_hazard", AttemptOutcome.VETO_DIALOG),
        ("safety_unverified", AttemptOutcome.VETO_UNVERIFIED),
        ("identity_unverified", AttemptOutcome.VETO_UNVERIFIED),
    ],
)
def test_a_veto_is_recorded_with_the_outcome_its_bound_belongs_to(
    mirror: MirrorWriter, store: InMemoryQueueStore, reason: str, expected: AttemptOutcome
) -> None:
    """The split is D12's, and it is the split that keeps the budgets separate.

    A dialog veto is bounded by a DURATION because a worker behind an unknown
    dialog is waiting on a human; an unverified probe is a failing delivery and
    spends the attempt budget.  Collapsing the two would dead-letter a
    dialog-blocked worker's valid steers in five minutes.
    """
    row = mirror.enqueue(enqueue_fact(1))
    mirror.observe_veto(LegacyVeto(legacy_message_ids=(1,), reason=reason, at=AT))
    stored = store.attempts_for(row.msg_id)
    assert len(stored) == 1
    assert stored[0].outcome is expected
    assert reason in stored[0].detail


def test_waiting_status_has_no_queue_equivalent(
    mirror: MirrorWriter, store: InMemoryQueueStore
) -> None:
    """The deliberate absence, and the reason it is deliberate.

    ``waiting_status`` is the status precondition D1 REMOVES from delivery: the
    queue selects on ``state`` and ``available_at`` and has no status term at
    all.  Recording it as one of D12's three would put a mechanism the phase
    deletes into the vocabulary the phase ships.
    """
    row = mirror.enqueue(enqueue_fact(1))
    mirror.observe_veto(LegacyVeto(legacy_message_ids=(1,), reason="waiting_status", at=AT))
    assert store.attempts_for(row.msg_id)[0].outcome is AttemptOutcome.LEGACY_OTHER


def test_a_veto_does_not_end_the_row(mirror: MirrorWriter, store: InMemoryQueueStore) -> None:
    """A veto is not an ending; in shadow mode it lives only in the attempt row.

    §7a: ``available_at`` does not move in shadow mode, no scheduler reading it
    there, so the veto's timing lives in the attempt row and nowhere else.
    """
    row = mirror.enqueue(enqueue_fact(1))
    before = store.get(row.msg_id)
    mirror.observe_veto(LegacyVeto(legacy_message_ids=(1,), reason="waiting_gate", at=AT))
    after = store.get(row.msg_id)
    assert after is not None and before is not None
    assert after.state is MsgState.READY
    assert after.available_at == before.available_at


def test_a_veto_against_an_already_ended_row_is_ignored(
    mirror: MirrorWriter, store: InMemoryQueueStore
) -> None:
    row = mirror.enqueue(enqueue_fact(1))
    mirror.observe(LegacyOutcome(legacy_message_id=1, status="delivered"))
    mirror.observe_veto(LegacyVeto(legacy_message_ids=(1,), reason="waiting_gate", at=AT))
    assert not [
        a for a in store.attempts_for(row.msg_id) if a.outcome is AttemptOutcome.VETO_DIALOG
    ]


def test_a_veto_for_an_unmirrored_message_is_silent(mirror: MirrorWriter) -> None:
    """No row is ordinary: the switch may have come on mid-session."""
    mirror.observe_veto(LegacyVeto(legacy_message_ids=(999,), reason="waiting_gate", at=AT))


# -------------------------------------------------- never breaks what it sees


@pytest.mark.parametrize("failing", ["record_attempt", "settle", "get_by_legacy_id"])
def test_a_broken_store_never_reaches_the_caller(
    mirror: MirrorWriter, store: InMemoryQueueStore, failing: str
) -> None:
    """The mutant: removing the swallow, so a shadow write can raise into delivery.

    ``send_message`` runs through the hook that calls this.  A raise here becomes
    a message that was never inserted, which is the failure class the phase
    exists to remove arriving through the diagnostic added to observe it.
    """
    mirror.enqueue(enqueue_fact(1))
    store.raise_on = {failing}

    mirror.observe(
        LegacyOutcome(
            legacy_message_id=1,
            status="delivered",
            attempts=(LegacyAttempt(ordinal=1, outcome="confirmed", started_at=AT),),
        )
    )
    mirror.observe_veto(LegacyVeto(legacy_message_ids=(1,), reason="waiting_gate", at=AT))
