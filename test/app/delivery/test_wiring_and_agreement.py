"""The switch seam and AC-3a's report (WP-ARCH phase 3a, F728 #584).

Two things that look unrelated and are not.  The switch is what makes AC-3a's
off-arm criterion true by construction — zero ``delivery_msg`` rows, because no
code path from a hook to the queue exists — and the report is what makes the
shadow arm mean anything.  A criterion is a DIFFERENCE between arms, so both
halves have to hold or a green run with the switch off would read as a pass.
"""

from __future__ import annotations

from datetime import UTC, datetime
from test.app.delivery.conftest import FakeClock, InMemoryQueueStore

import pytest

from cli_agent_orchestrator.app.delivery import wiring
from cli_agent_orchestrator.app.delivery.agreement import (
    MIN_MESSAGES,
    MIN_RECEIVERS,
    MIN_TERMINAL_EACH_SIDE,
    build_delivery_agreement,
)
from cli_agent_orchestrator.app.delivery.facts import LegacyEnqueue, LegacyOutcome, LegacyVeto
from cli_agent_orchestrator.app.delivery.mirror import MirrorWriter
from cli_agent_orchestrator.core.delivery import (
    EnqueueDraft,
    MsgState,
    QueueMode,
    SwitchPosition,
)

AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def fact(legacy_id: int, status: str = "pending") -> LegacyEnqueue:
    return LegacyEnqueue(
        legacy_message_id=legacy_id,
        receiver_id="mb_supervisor",
        sender_id="t-worker",
        message="m",
        status=status,
        created_at=AT,
        orchestration_type="send_message",
    )


@pytest.fixture(autouse=True)
def _disarmed() -> object:
    """Every test starts and ends with the switch off.

    Module-global state deserves a fixture rather than trust: the seam is a
    process-wide install, so one test leaving it armed would make the next one's
    "the switch is off" assertion pass or fail for the wrong reason.
    """
    wiring.reset_delivery()
    yield
    wiring.reset_delivery()


def arm(store: InMemoryQueueStore, clock: FakeClock) -> None:
    wiring.install_delivery(
        wiring.DeliveryRuntime(
            store=store,
            clock=clock,
            position=SwitchPosition.SHADOW,
            mirror=MirrorWriter(store, clock),
        )
    )


# ------------------------------------------------------------------- the off arm


def test_with_the_switch_off_no_hook_reaches_the_queue(
    store: InMemoryQueueStore, clock: FakeClock
) -> None:
    """AC-3a's off-arm criterion, at the level it is guaranteed.

    Rows in the off arm are a FAILURE rather than a curiosity, and the guarantee
    is structural: with no runtime installed there is no path from a hook to a
    store.  "The switch was ignored" is therefore not expressible as a missing
    ``if`` in a hook — it would have to be a deleted install guard in the
    composition root, where the A/B suite sees it.
    """
    assert wiring.queue_enabled() is False

    wiring.record_enqueue(fact(1))
    wiring.record_outcome(LegacyOutcome(legacy_message_id=1, status="delivered"))
    wiring.record_veto(LegacyVeto(legacy_message_ids=(1,), reason="waiting_gate", at=AT))

    assert store.count() == 0
    assert store.attempts == {}


def test_arming_and_disarming_flip_the_only_guard(
    store: InMemoryQueueStore, clock: FakeClock
) -> None:
    arm(store, clock)
    assert wiring.queue_enabled() is True
    wiring.record_enqueue(fact(1))
    assert store.count(mode=QueueMode.SHADOW) == 1

    wiring.reset_delivery()
    wiring.record_enqueue(fact(2))
    assert store.count() == 1


def test_a_hook_never_raises_into_its_caller(store: InMemoryQueueStore, clock: FakeClock) -> None:
    """§7a: the enqueue sits behind the switch and does not raise into the caller.

    The stake is not the diagnostic.  These hooks run inside
    ``_create_inbox_message_unfenced`` and ``settle_delivery_attempt``, so a
    raise here becomes a message that was never inserted or a settlement that
    never committed — the failure class the phase exists to remove, arriving
    through the observation added to measure it.
    """
    arm(store, clock)
    store.raise_on = {"enqueue", "settle", "record_attempt", "get_by_legacy_id"}

    wiring.record_enqueue(fact(1))
    wiring.record_outcome(LegacyOutcome(legacy_message_id=1, status="delivered"))
    wiring.record_veto(LegacyVeto(legacy_message_ids=(1,), reason="waiting_gate", at=AT))


def test_the_hooks_return_nothing_so_a_caller_cannot_depend_on_a_queue_id(
    store: InMemoryQueueStore, clock: FakeClock
) -> None:
    """Sub-phase 3a's whole claim is that removing it changes nothing.

    A legacy caller holding a minted ``msg_id`` is a caller that can come to
    depend on one, and the claim would stop being true.
    """
    arm(store, clock)
    assert wiring.record_enqueue(fact(1)) is None


# ------------------------------------------------------------ the agreement report


def rows(store: InMemoryQueueStore) -> list[object]:
    return list(store.rows.values())


def populate(
    store: InMemoryQueueStore, clock: FakeClock, count: int, *, receivers: int = 2
) -> dict[int, str]:
    mirror = MirrorWriter(store, clock)
    legacy: dict[int, str] = {}
    for index in range(count):
        legacy_id = index + 1
        mirror.enqueue(
            LegacyEnqueue(
                legacy_message_id=legacy_id,
                receiver_id=f"mb_{legacy_id % receivers}",
                sender_id="t-worker",
                message="m",
                status="pending",
                created_at=AT,
                orchestration_type="send_message",
            )
        )
        mirror.observe(LegacyOutcome(legacy_message_id=legacy_id, status="delivered"))
        legacy[legacy_id] = "delivered"
    return legacy


def test_a_faithful_mirror_agrees_on_every_id(store: InMemoryQueueStore, clock: FakeClock) -> None:
    legacy = populate(store, clock, MIN_MESSAGES)
    report = build_delivery_agreement(store.rows.values(), legacy)

    assert report.valid, report.invalid_reasons
    assert report.agreement_rate == 1.0
    assert report.classification_counts() == {"queue_early": 0, "legacy_early": 0, "genuine": 0}


def test_a_missed_outcome_is_counted_as_legacy_early_not_hidden(
    store: InMemoryQueueStore, clock: FakeClock
) -> None:
    """§7a's missed-outcome case, which is expected rather than exceptional.

    A server restarted mid-flight leaves the shadow row ``ready``.  The report
    must show that; a mirror that looked perfect by declining to notice would be
    worse than one that disagreed loudly.
    """
    legacy = populate(store, clock, MIN_MESSAGES)
    mirror = MirrorWriter(store, clock)
    mirror.enqueue(fact(999))
    legacy[999] = "delivered"

    report = build_delivery_agreement(store.rows.values(), legacy)
    assert report.classification_counts()["legacy_early"] == 1


def test_a_wrong_mapping_shows_up_as_genuine(store: InMemoryQueueStore, clock: FakeClock) -> None:
    """The only class that says the mirror or the mapping is wrong."""
    legacy = populate(store, clock, MIN_MESSAGES)
    mirror = MirrorWriter(store, clock)
    row = mirror.enqueue(fact(999))
    store.settle(row.msg_id, state=MsgState.SUPERSEDED, now=clock.now())
    legacy[999] = "delivered"

    report = build_delivery_agreement(store.rows.values(), legacy)
    assert report.classification_counts()["genuine"] == 1


def test_a_queue_that_ended_first_is_queue_early(
    store: InMemoryQueueStore, clock: FakeClock
) -> None:
    legacy = populate(store, clock, MIN_MESSAGES)
    mirror = MirrorWriter(store, clock)
    row = mirror.enqueue(fact(999))
    store.settle(row.msg_id, state=MsgState.DELIVERED, now=clock.now())
    legacy[999] = "pending"

    report = build_delivery_agreement(store.rows.values(), legacy)
    assert report.classification_counts()["queue_early"] == 1


def test_an_empty_run_is_invalid_rather_than_perfect() -> None:
    """The off arm cannot read as a pass.

    A report that averaged a fabricated 1.0 over zero comparisons would make the
    emptiest possible session — the one where the feature never ran — look like
    the strongest possible result.
    """
    report = build_delivery_agreement([], {})
    assert report.valid is False
    assert report.agreement_rate is None
    assert len(report.invalid_reasons) >= 3


def test_a_queue_side_that_never_ends_anything_fails_the_floor(
    store: InMemoryQueueStore, clock: FakeClock
) -> None:
    """Both terminal counts are floored, and this is why.

    A run where legacy ended plenty of rows and the queue ended none would
    otherwise pass and report a perfect legacy-early rate, which reads as a
    finding about timing when it is actually the mirror writer never having run.
    """
    mirror = MirrorWriter(store, clock)
    legacy = {}
    for index in range(MIN_MESSAGES):
        mirror.enqueue(
            LegacyEnqueue(
                legacy_message_id=index + 1,
                receiver_id=f"mb_{index % MIN_RECEIVERS}",
                sender_id="t",
                message="m",
                status="pending",
                created_at=AT,
                orchestration_type="send_message",
            )
        )
        legacy[index + 1] = "delivered"

    report = build_delivery_agreement(store.rows.values(), legacy)
    assert report.valid is False
    assert any("terminal queue row" in reason for reason in report.invalid_reasons)


def test_a_single_lane_session_is_not_a_multi_lane_session(
    store: InMemoryQueueStore, clock: FakeClock
) -> None:
    legacy = populate(store, clock, MIN_MESSAGES, receivers=1)
    report = build_delivery_agreement(store.rows.values(), legacy)
    assert report.valid is False
    assert any("receiver" in reason for reason in report.invalid_reasons)


def test_live_rows_are_not_compared(store: InMemoryQueueStore, clock: FakeClock) -> None:
    """A live row has no legacy counterpart, so including it would fabricate one.

    Post-flip the legacy inbox is read-only, so a missing legacy status for a
    live row is correct rather than a disagreement.
    """
    legacy = populate(store, clock, MIN_MESSAGES)
    store.enqueue(EnqueueDraft(idempotency_key="live-1", receiver_id="mb_0", mode=QueueMode.LIVE))

    report = build_delivery_agreement(store.rows.values(), legacy)
    assert report.total_messages == MIN_MESSAGES
    assert report.valid


def test_an_in_flight_pair_is_not_counted_as_an_agreement(
    store: InMemoryQueueStore, clock: FakeClock
) -> None:
    """Neither side has ended, so there is nothing to compare yet.

    Counting it as agreement would let a session of messages nothing had finished
    delivering report a perfect rate.
    """
    mirror = MirrorWriter(store, clock)
    mirror.enqueue(fact(1))
    report = build_delivery_agreement(store.rows.values(), {1: "pending"})
    assert report.total_agreements == 0
    assert report.total_comparable == 0
    assert MIN_TERMINAL_EACH_SIDE > 0 and report.valid is False
