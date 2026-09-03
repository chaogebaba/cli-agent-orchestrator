"""The delivery queue store (WP-ARCH phase 3a, F728 #584).

Against a real SQLite file, because what is being tested is the STATEMENTS.  A
fake store would pass every assertion here while the shipped ``UPDATE`` quietly
named ``dead_by``, or the shipped ``SELECT`` quietly dropped its ``mode``
conjunct — and those two are exactly the phase's easiest-to-lose properties, the
ones §14 records as having no second line of defence.

Four of the phase's named mutants are killed in this file:

* ``dead_by`` recomputed from the current ``available_at`` on re-offer (D12);
* ``claim``'s ``mode='live'`` filter moved out of the statement (D9/B20);
* the re-parent that rewrites ``receiver_id`` without the digest (§5 item 6);
* the boot occupancy predicate counting shadow rows (D9/B16).

The last one is measured through :meth:`SqliteQueueStore.occupancy`, which is
where a store can get it wrong; the pure half is in ``test/core/test_delivery.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS, FakeClock

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.adapters.store.queue import IdempotencyConflict, SqliteQueueStore
from cli_agent_orchestrator.core.delivery import (
    AttemptOutcome,
    DeadReason,
    DeliveryAttempt,
    EnqueueDraft,
    MsgState,
    QueueMode,
)
from cli_agent_orchestrator.core.timing import (
    DELIVERY_BACKOFF_S,
    DELIVERY_LEASE_S,
    DELIVERY_MAX_ATTEMPTS,
    DELIVERY_MAX_LIFETIME_S,
)


@pytest.fixture
def queue_pool(tmp_path: Path) -> Iterator[ConnectionPool]:
    result, pool = migrate(tmp_path / "queue.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok, result
    assert pool is not None
    yield pool
    pool.close_all()


@pytest.fixture
def queue(queue_pool: ConnectionPool, clock: FakeClock) -> SqliteQueueStore:
    return SqliteQueueStore(queue_pool, clock=clock)


def live(key: str, receiver: str = "mb_super", **extra: object) -> EnqueueDraft:
    return EnqueueDraft(
        idempotency_key=key, receiver_id=receiver, mode=QueueMode.LIVE, **extra  # type: ignore[arg-type]
    )


def shadow(key: str, receiver: str = "mb_super", **extra: object) -> EnqueueDraft:
    return EnqueueDraft(
        idempotency_key=key, receiver_id=receiver, mode=QueueMode.SHADOW, **extra  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------- schema


def test_the_phase_three_tables_are_created_by_the_phase_one_migrator(
    queue_pool: ConnectionPool,
) -> None:
    """D5: one migrator, not two.

    A second migrator or a second ULID factory is a review-stopping defect, and
    it is how one schema comes to have two authorities.  The assertion is on the
    tables rather than on the migrator's step list so it still holds if the steps
    are reordered.
    """
    names = {
        row["name"]
        for row in queue_pool.connection().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"delivery_msg", "delivery_attempt", "delivery_dead", "seat_digest"} <= names
    # And phase 1's are still there: the DDL is additive.
    assert {"worker_event", "worker_state_shadow", "finding"} <= names


# ------------------------------------------------------------------ enqueue


def test_enqueue_stamps_the_deadline_once_at_creation(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    row = queue.enqueue(live("k1"))
    assert row.state is MsgState.READY
    assert row.available_at == row.created_at  # R1, by construction
    assert (row.dead_by - row.created_at).total_seconds() == DELIVERY_MAX_LIFETIME_S


def test_a_replayed_enqueue_returns_the_existing_row(queue: SqliteQueueStore) -> None:
    """Replay-safety, which is what makes the mirror hook idempotent.

    The same legacy insert observed twice must not produce two shadow rows, or
    every doubly-observed message would read as a duplicate in the report the
    phase exists to make trustworthy.
    """
    first = queue.enqueue(shadow("legacy-inbox:41", payload="hello"))
    again = queue.enqueue(shadow("legacy-inbox:41", payload="hello"))
    assert again.msg_id == first.msg_id
    assert queue.count() == 1


def test_the_same_key_with_a_different_body_fails_loud(queue: SqliteQueueStore) -> None:
    """A changed payload under one key is a caller bug, not a replay.

    Silently returning the first message would hand the caller a success id for a
    message that was never enqueued — the silent-loss shape the phase exists to
    remove, arriving through the mechanism meant to prevent duplicates.
    """
    queue.enqueue(live("k1", payload="one"))
    with pytest.raises(IdempotencyConflict):
        queue.enqueue(live("k1", payload="two"))


def test_a_caller_expiry_shortens_the_stored_deadline(queue: SqliteQueueStore) -> None:
    row = queue.enqueue(live("k1", expire_after_s=45))
    assert (row.dead_by - row.created_at).total_seconds() == 45


# -------------------------------------------------------------------- claim


def test_claim_never_returns_a_shadow_row(queue: SqliteQueueStore, clock: FakeClock) -> None:
    """The mutant: the ``mode='live'`` conjunct removed from the statement.

    A surviving shadow row claimable at the flip means the tick injects a copy of
    a message the legacy path already delivered — a second carrier over one id.
    The row here is READY and DUE, so nothing but the mode conjunct excludes it.
    """
    queue.enqueue(shadow("legacy-inbox:1"))
    clock.advance(seconds=1)
    assert queue.claim(lease_owner="tick", now=clock.now(), limit=10) == []


def test_the_filter_is_in_the_statement_not_in_the_caller() -> None:
    """Asserted on the SQL text, because that is what the rule is ABOUT.

    D9 moved the conjunct into ``claim`` precisely so no future caller could
    forget it; a behavioural test cannot tell "the statement filters" from "this
    particular caller happened to filter".  Reading the source can.
    """
    import inspect

    from cli_agent_orchestrator.adapters.store import queue as queue_module

    source = inspect.getsource(queue_module.SqliteQueueStore.claim)
    assert "mode = 'live'" in source


def test_claim_leases_a_live_row_and_issues_a_fencing_token(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    row = queue.enqueue(live("k1"))
    clock.advance(seconds=1)
    claimed = queue.claim(lease_owner="tick", now=clock.now())
    assert [c.msg_id for c in claimed] == [row.msg_id]
    assert claimed[0].state is MsgState.LEASED
    assert claimed[0].claim_id == row.claim_id + 1
    assert claimed[0].lease_expires_at is not None


def test_claim_skips_a_row_whose_deadline_has_already_passed(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    """A row past ``dead_by`` is not deliverable, whatever its state says.

    Without this conjunct a row could be leased, injected and delivered after the
    deadline the caller asked for — which is the aged-message harm of #435
    arriving through the queue rather than through legacy.
    """
    queue.enqueue(live("k1", expire_after_s=10))
    clock.advance(seconds=60)
    assert queue.claim(lease_owner="tick", now=clock.now()) == []


# ---------------------------------------------------------------------- ack


def test_a_stale_fencing_token_cannot_ack(queue: SqliteQueueStore, clock: FakeClock) -> None:
    """A slow worker whose lease was stolen must not settle a newer delivery."""
    queue.enqueue(live("k1"))
    clock.advance(seconds=1)
    claimed = queue.claim(lease_owner="tick", now=clock.now())[0]

    assert queue.ack(claimed.msg_id, claimed.claim_id - 1, now=clock.now()) is False
    assert queue.ack(claimed.msg_id, claimed.claim_id, now=clock.now()) is True
    settled = queue.get(claimed.msg_id)
    assert settled is not None and settled.state is MsgState.DELIVERED


# ------------------------------------------------------------------ reclaim


def test_reclaim_returns_an_expired_lease_and_counts_the_attempt(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    row = queue.enqueue(live("k1"))
    clock.advance(seconds=1)
    queue.claim(lease_owner="tick", now=clock.now())
    clock.advance(seconds=DELIVERY_LEASE_S + 1)

    reclaimed, dead = queue.reclaim(now=clock.now())
    assert (reclaimed, dead) == (1, 0)
    after = queue.get(row.msg_id)
    assert after is not None
    assert after.state is MsgState.READY
    assert after.attempts == 1
    assert (after.available_at - clock.now()).total_seconds() == pytest.approx(
        DELIVERY_BACKOFF_S, abs=1
    )


def test_reclaim_never_moves_the_deadline(queue: SqliteQueueStore, clock: FakeClock) -> None:
    """The highest-value single test in the phase (D12, §14).

    ``reclaim`` rewrites ``available_at`` on every re-offer.  Recomputing
    ``dead_by`` from the current value would extend the deadline once per attempt
    and the row would never die — so the whole of I1, and the #568 non-overlap
    property with the legacy notice, rest on this column not moving.

    Driven through the FULL budget rather than one cycle, because a mutant that
    recomputed only on the first re-offer would survive a single-cycle test.
    """
    row = queue.enqueue(live("k1"))
    stamped = row.dead_by

    for _ in range(DELIVERY_MAX_ATTEMPTS - 1):
        clock.advance(seconds=DELIVERY_BACKOFF_S + 1)
        queue.claim(lease_owner="tick", now=clock.now())
        clock.advance(seconds=DELIVERY_LEASE_S + 1)
        queue.reclaim(now=clock.now())
        current = queue.get(row.msg_id)
        assert current is not None
        assert current.dead_by == stamped, "dead_by moved — the deadline was recomputed"

    assert current.attempts == DELIVERY_MAX_ATTEMPTS - 1
    assert current.available_at > stamped - timedelta(seconds=DELIVERY_MAX_LIFETIME_S)


def test_a_row_past_its_attempt_budget_reaches_the_dead_letter_table(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    """D3: the attempt budget is load-bearing, and dead is a separate table.

    A poisoned message that stayed in ``delivery_msg`` would occupy the reclaim
    loop and hide live rows, which is why the audit took honker's separate-table
    decision.
    """
    row = queue.enqueue(live("k1"))
    for _ in range(DELIVERY_MAX_ATTEMPTS):
        clock.advance(seconds=DELIVERY_BACKOFF_S + 1)
        queue.claim(lease_owner="tick", now=clock.now())
        clock.advance(seconds=DELIVERY_LEASE_S + 1)
        queue.reclaim(now=clock.now())

    final = queue.get(row.msg_id)
    assert final is not None and final.state is MsgState.DEAD
    dead = queue.dead_letter(row.msg_id)
    assert dead is not None
    assert dead.reason is DeadReason.MAX_ATTEMPTS
    assert dead.mode is QueueMode.LIVE


def test_a_row_whose_deadline_passes_dies_on_the_time_bound(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    """The outermost term ends the row even with attempts to spare (I1, D12)."""
    row = queue.enqueue(live("k1"))
    clock.advance(seconds=DELIVERY_MAX_LIFETIME_S + 1)
    _, dead_count = queue.reclaim(now=clock.now())
    assert dead_count == 1
    dead = queue.dead_letter(row.msg_id)
    assert dead is not None and dead.reason is DeadReason.MAX_LIFETIME


def test_an_expiring_row_dies_with_the_callers_reason(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    """D8's reason, so a caller expiry adds no fourth terminal state."""
    row = queue.enqueue(live("k1", expire_after_s=30))
    clock.advance(seconds=60)
    queue.reclaim(now=clock.now())
    dead = queue.dead_letter(row.msg_id)
    assert dead is not None and dead.reason is DeadReason.EXPIRED


# -------------------------------------------------------------- occupancy


def test_occupancy_counts_live_non_terminal_rows_only(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    """B16 at the store level: the number the boot guard is handed.

    Shadow rows and terminal rows both present as zero, and for different
    reasons: a shadow row is an observational copy the queue must never serve,
    and a terminal row is finished.  Counting either would demote a deployment
    that has nothing outstanding.
    """
    queue.enqueue(shadow("legacy-inbox:1"))
    queue.enqueue(shadow("legacy-inbox:2"))
    assert queue.occupancy().live_non_terminal == 0

    row = queue.enqueue(live("k1"))
    assert queue.occupancy().live_non_terminal == 1

    queue.settle(row.msg_id, state=MsgState.DELIVERED, now=clock.now())
    assert queue.occupancy().live_non_terminal == 0


def test_occupancy_survives_a_database_with_no_barrier_table(
    queue: SqliteQueueStore,
) -> None:
    """A missing legacy table must not stop the boot resolving its own switch.

    "No barrier is open" is the honest reading when the barrier schema is not
    there; the alternative is a server that cannot boot because a table it does
    not own is absent.
    """
    assert queue.occupancy().open_barrier_labels == ()


# ----------------------------------------------------------------- settling


def test_a_terminal_row_is_not_re_settled(queue: SqliteQueueStore, clock: FakeClock) -> None:
    """First terminal observation wins.

    The mirror writer observes several legacy edges per message and they do not
    arrive in a guaranteed order, so a late edge must not be able to rewrite a
    recorded outcome — otherwise the agreement report measures arrival order.
    """
    row = queue.enqueue(shadow("legacy-inbox:1"))
    assert queue.settle(row.msg_id, state=MsgState.DELIVERED, now=clock.now()) is True
    assert queue.settle(row.msg_id, state=MsgState.SUPERSEDED, now=clock.now()) is False
    final = queue.get(row.msg_id)
    assert final is not None and final.state is MsgState.DELIVERED


def test_settling_dead_without_a_reason_is_refused(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    """I1 says the reason distinguishes the four deaths, so it is not optional."""
    row = queue.enqueue(live("k1"))
    with pytest.raises(ValueError):
        queue.settle(row.msg_id, state=MsgState.DEAD, now=clock.now())


def test_settling_to_a_non_terminal_state_is_refused(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    row = queue.enqueue(live("k1"))
    with pytest.raises(ValueError):
        queue.settle(row.msg_id, state=MsgState.READY, now=clock.now())


def test_an_attempt_row_is_written_once_per_key(queue: SqliteQueueStore, clock: FakeClock) -> None:
    """Re-observing one legacy attempt is a no-op, not a second row."""
    row = queue.enqueue(shadow("legacy-inbox:1"))
    attempt = DeliveryAttempt(
        msg_id=row.msg_id,
        claim_id=1,
        carrier="legacy",
        started_at=clock.now(),
        outcome=AttemptOutcome.PANE_ABSENT,
        detail="first",
    )
    queue.record_attempt(attempt)
    queue.record_attempt(attempt.model_copy(update={"detail": "second"}))

    stored = queue.attempts_for(row.msg_id)
    assert len(stored) == 1
    assert stored[0].detail == "first"


# ----------------------------------------------------------------- reparent


def test_reparent_moves_the_row_and_both_digests_together(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    """§5 item 6, the third property with no second line of defence.

    Without the digest half the moved id stays listed in an epoch it no longer
    belongs to, never reaches a terminal state there, and holds that epoch open
    forever while the tick re-wakes it once per lease — an open digest that
    cannot close, which I6 forbids.
    """
    row = queue.enqueue(live("k1", receiver="mb_worker"))
    other = queue.enqueue(live("k2", receiver="mb_worker"))
    queue.build_digest("mb_worker", (row.msg_id, other.msg_id), now=clock.now())

    assert queue.reparent(
        row.msg_id, new_receiver_id="mb_caller", now=clock.now(), message_prefix="[released] "
    )

    moved = queue.get(row.msg_id)
    assert moved is not None
    assert moved.receiver_id == "mb_caller"
    assert moved.payload.startswith("[released] ")

    old = queue.open_digest("mb_worker")
    assert old is not None and old.msg_ids == (other.msg_id,)

    new = queue.open_digest("mb_caller")
    assert new is not None and new.msg_ids == (row.msg_id,)


def test_an_epoch_emptied_by_a_reparent_closes_abandoned_at_once(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    """Not ``cancelled``, and not left for a tick.

    The trigger is a reap, so the old receiver has no live incarnation, and an
    empty set satisfies D10's every-message-terminal test vacuously — which makes
    ``abandoned`` the correct value and immediate closure the correct timing.
    """
    row = queue.enqueue(live("k1", receiver="mb_worker"))
    digest = queue.build_digest("mb_worker", (row.msg_id,), now=clock.now())

    queue.reparent(row.msg_id, new_receiver_id="mb_caller", now=clock.now())

    closed = queue.digest_at("mb_worker", digest.epoch)
    assert closed is not None
    assert closed.open is False
    assert closed.consumed_via == "abandoned"
    assert closed.msg_ids == ()


def test_the_payload_digest_follows_the_reparented_payload(
    queue: SqliteQueueStore, clock: FakeClock
) -> None:
    """The prefix changes the body, so the digest must change with it.

    A stale digest would make a later idempotent re-enqueue of the same key read
    as a conflict, which is the loudest possible failure for a purely cosmetic
    prefix.
    """
    row = queue.enqueue(live("k1", receiver="mb_worker", payload="steer"))
    queue.reparent(row.msg_id, new_receiver_id="mb_caller", now=clock.now(), message_prefix="[r] ")
    moved = queue.get(row.msg_id)
    assert moved is not None and moved.payload_digest != row.payload_digest


def test_reparent_declines_a_terminal_row(queue: SqliteQueueStore, clock: FakeClock) -> None:
    """A delivered message has nothing owed, so there is nothing to release."""
    row = queue.enqueue(live("k1", receiver="mb_worker"))
    queue.settle(row.msg_id, state=MsgState.DELIVERED, now=clock.now())
    assert queue.reparent(row.msg_id, new_receiver_id="mb_caller", now=clock.now()) is False


def test_a_consumed_epoch_is_never_reopened(queue: SqliteQueueStore, clock: FakeClock) -> None:
    """§5 item 7: a later arrival opens a NEW epoch.

    That is what makes #568 unreachable rather than filtered — the fallback
    cannot re-announce an acked id because there is no epoch left to announce.
    """
    first = queue.enqueue(live("k1", receiver="mb_worker"))
    epoch_one = queue.build_digest("mb_worker", (first.msg_id,), now=clock.now())
    queue.reparent(first.msg_id, new_receiver_id="mb_other", now=clock.now())
    assert queue.digest_at("mb_worker", epoch_one.epoch) is not None

    clock.advance(seconds=5)
    second = queue.enqueue(live("k2", receiver="mb_worker"))
    epoch_two = queue.build_digest("mb_worker", (second.msg_id,), now=clock.now())
    assert epoch_two.epoch == epoch_one.epoch + 1


# --------------------------------------------------------------- timestamps


def test_stored_timestamps_order_as_strings(
    queue: SqliteQueueStore, clock: FakeClock, queue_pool: ConnectionPool
) -> None:
    """``claim`` and ``reclaim`` compare TEXT columns, so the width must be fixed.

    A rendering that dropped trailing zeros would make the ordering depend on
    whether a microsecond happened to be zero, and a row enqueued on a whole
    second would sort wrongly against one enqueued a moment later.
    """
    clock.value = datetime(2026, 9, 3, 12, 0, 0, 0, tzinfo=UTC)
    queue.enqueue(live("k1"))
    clock.value = datetime(2026, 9, 3, 12, 0, 0, 500, tzinfo=UTC)
    queue.enqueue(live("k2"))

    rendered = [
        row["available_at"]
        for row in queue_pool.connection().execute(
            "SELECT available_at FROM delivery_msg ORDER BY idempotency_key"
        )
    ]
    assert len({len(value) for value in rendered}) == 1
    assert rendered == sorted(rendered)
