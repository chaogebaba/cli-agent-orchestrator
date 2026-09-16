"""AC-S1.13 and AC-S1.15 — D7.3's busy-paused lifetime clock.

Every arm asserts at BOTH predicate sites, which is AC-S1.15's own requirement
and not belt-and-braces: r3's bug extended the bound at one site only, and a row
that is claimable while already dead — or dead while still claimable — is worse
than either consistent answer.  The two sites are ``claim`` (is this row
deliverable?) and ``reclaim``'s death sweep (is this row over?).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS, FakeClock

import pytest

from cli_agent_orchestrator.adapters.store.busy_ledger import BusyLedger
from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
from cli_agent_orchestrator.core.delivery import DeadReason, EnqueueDraft, MsgKind, QueueMode
from cli_agent_orchestrator.core.timing import (
    BUSY_CREDIT_CAP_S,
    DELIVERY_MAX_LIFETIME_S,
    IDLE_STALL_AGE_S,
)

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[ConnectionPool]:
    result, pool = migrate(tmp_path / "busy.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    yield pool
    pool.close_all()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def queue(pool: ConnectionPool, clock: FakeClock) -> SqliteQueueStore:
    return SqliteQueueStore(pool, clock=clock)


@pytest.fixture
def ledger(pool: ConnectionPool) -> BusyLedger:
    return BusyLedger(pool)


def _enqueue(queue: SqliteQueueStore, *, key: str = "k1", expire_after_s: int | None = None):
    return queue.enqueue(
        EnqueueDraft(
            idempotency_key=key,
            receiver_id="term-a",
            sender_id="sender",
            kind=MsgKind.CALLBACK,
            payload="body",
            mode=QueueMode.LIVE,
            expire_after_s=expire_after_s,
        )
    )


def _claimable(queue: SqliteQueueStore, *, now: datetime) -> list[str]:
    return [m.msg_id for m in queue.claim(lease_owner="probe", now=now, limit=10)]


# ------------------------------------------------------------- AC-S1.13


def test_opening_an_episode_marks_it_and_a_second_open_is_a_no_op(
    queue: SqliteQueueStore, ledger: BusyLedger
) -> None:
    """Idempotence is the property, not tidiness.

    The tick re-offers a busy row every cycle.  An open that overwrote the marker
    would restart the episode's clock each time, so a row waiting ten minutes
    would end up with one tick of credit.
    """
    message = _enqueue(queue)
    assert ledger.open_episode(message.msg_id, now=T0) is True
    assert ledger.open_episode(message.msg_id, now=T0 + timedelta(seconds=30)) is False
    busy_since, _, _ = ledger.read(message.msg_id)
    assert busy_since == T0


def test_closing_an_episode_adds_its_elapsed_interval(
    queue: SqliteQueueStore, ledger: BusyLedger
) -> None:
    """r2's bug, as a test: the marker was cleared and the interval was DISCARDED."""
    message = _enqueue(queue)
    ledger.open_episode(message.msg_id, now=T0)
    total = ledger.close_episode(message.msg_id, now=T0 + timedelta(seconds=12))
    assert total == pytest.approx(12.0)
    busy_since, accumulated, _ = ledger.read(message.msg_id)
    assert busy_since is None
    assert accumulated == pytest.approx(12.0)


def test_two_episodes_accumulate(queue: SqliteQueueStore, ledger: BusyLedger) -> None:
    message = _enqueue(queue)
    ledger.open_episode(message.msg_id, now=T0)
    ledger.close_episode(message.msg_id, now=T0 + timedelta(seconds=10))
    ledger.open_episode(message.msg_id, now=T0 + timedelta(seconds=20))
    total = ledger.close_episode(message.msg_id, now=T0 + timedelta(seconds=25))
    assert total == pytest.approx(15.0)


def test_the_accumulator_is_capped(queue: SqliteQueueStore, ledger: BusyLedger) -> None:
    """An uncapped accumulator lets ONE ledger error extend a row indefinitely."""
    message = _enqueue(queue)
    ledger.open_episode(message.msg_id, now=T0)
    total = ledger.close_episode(message.msg_id, now=T0 + timedelta(seconds=BUSY_CREDIT_CAP_S * 10))
    assert total == pytest.approx(float(BUSY_CREDIT_CAP_S))


def test_a_lease_expiring_without_a_nudge_closes_the_episode(
    queue: SqliteQueueStore, ledger: BusyLedger
) -> None:
    """ "No row survives with an uncleared ``busy_since`` past its lease".

    A sweep rather than a timer, because the driver that opened the episode is
    exactly the component whose death leaves the marker behind.
    """
    message = _enqueue(queue)
    claimed = queue.claim(lease_owner="tick", now=T0, limit=1)
    assert claimed
    ledger.open_episode(message.msg_id, now=T0)
    far_future = T0 + timedelta(hours=2)
    closed = ledger.close_expired_episodes(now=far_future)
    assert message.msg_id in closed
    busy_since, accumulated, _ = ledger.read(message.msg_id)
    assert busy_since is None
    assert accumulated == pytest.approx(float(BUSY_CREDIT_CAP_S))


def test_closing_a_row_that_never_opened_is_harmless(
    queue: SqliteQueueStore, ledger: BusyLedger
) -> None:
    message = _enqueue(queue)
    assert ledger.close_episode(message.msg_id, now=T0) == pytest.approx(0.0)


# ------------------------------------------------------------- AC-S1.15


def test_the_bound_extends_by_the_accumulated_busy_time(
    queue: SqliteQueueStore, ledger: BusyLedger
) -> None:
    """*Positive arm*: a row held busy for a FRACTION of the cap.

    The constant is re-derived from the stage-1 cap (review N6): the old "2x"
    form asserted the very state that breaks I4.  So the arm holds the row busy
    for a third of the cap and asserts the extension EQUALS the accumulated busy
    time.
    """
    message = _enqueue(queue)
    raw_dead_by = message.dead_by
    held = BUSY_CREDIT_CAP_S // 3
    ledger.open_episode(message.msg_id, now=T0)
    ledger.close_episode(message.msg_id, now=T0 + timedelta(seconds=held))
    _, accumulated, effective = ledger.read(message.msg_id)
    assert accumulated == pytest.approx(float(held))
    assert effective == raw_dead_by + timedelta(seconds=held)


def test_the_extended_row_is_still_claimable_after_its_raw_deadline(
    queue: SqliteQueueStore, ledger: BusyLedger
) -> None:
    """PREDICATE SITE 1 — ``claim``.  Alive past ``dead_by``, on credit."""
    message = _enqueue(queue)
    held = BUSY_CREDIT_CAP_S // 2
    ledger.open_episode(message.msg_id, now=T0)
    ledger.close_episode(message.msg_id, now=T0 + timedelta(seconds=held))

    just_past_raw = message.dead_by + timedelta(seconds=1)
    assert message.msg_id in _claimable(queue, now=just_past_raw)


def test_the_extended_row_is_not_swept_before_its_effective_deadline(
    queue: SqliteQueueStore, ledger: BusyLedger
) -> None:
    """PREDICATE SITE 2 — the death sweep.  Both, or the two disagree."""
    message = _enqueue(queue)
    held = BUSY_CREDIT_CAP_S // 2
    ledger.open_episode(message.msg_id, now=T0)
    ledger.close_episode(message.msg_id, now=T0 + timedelta(seconds=held))

    result = queue.reclaim(now=message.dead_by + timedelta(seconds=1))
    dead_ids = {row.msg_id for row in result.dead}
    assert message.msg_id not in dead_ids


def test_the_row_dies_at_the_cap_not_later(queue: SqliteQueueStore, ledger: BusyLedger) -> None:
    """*Cap arm*: busy past the cap — the row dies AT the cap, with a typed reason."""
    message = _enqueue(queue)
    ledger.open_episode(message.msg_id, now=T0)
    ledger.close_episode(message.msg_id, now=T0 + timedelta(seconds=BUSY_CREDIT_CAP_S * 5))

    capped_deadline = message.dead_by + timedelta(seconds=BUSY_CREDIT_CAP_S)
    assert message.msg_id not in _claimable(queue, now=capped_deadline + timedelta(seconds=1))
    result = queue.reclaim(now=capped_deadline + timedelta(seconds=1))
    dead = {row.msg_id: row for row in result.dead}
    assert message.msg_id in dead
    assert dead[message.msg_id].reason is DeadReason.MAX_LIFETIME


def test_a_frozen_ledger_dies_on_schedule_at_the_raw_bound(
    queue: SqliteQueueStore, ledger: BusyLedger
) -> None:
    """*Negative arm*: no episode is ever opened, so nothing extends.

    Without this the positive arm would pass against an implementation that
    extended every row unconditionally.
    """
    message = _enqueue(queue)
    result = queue.reclaim(now=message.dead_by + timedelta(seconds=1))
    assert message.msg_id in {row.msg_id for row in result.dead}


def test_a_caller_deadline_fires_on_wall_clock_regardless_of_busy_credit(
    queue: SqliteQueueStore, ledger: BusyLedger
) -> None:
    """*B2 regression arm*: r3's bug was a caller deadline swallowed by the credit.

    ``expire_after_s`` means the SENDER time-boxed the message.  Busy credit is
    the system's own accounting and may not spend someone else's box.
    """
    message = _enqueue(queue, key="boxed", expire_after_s=60)
    ledger.open_episode(message.msg_id, now=T0)
    ledger.close_episode(message.msg_id, now=T0 + timedelta(seconds=BUSY_CREDIT_CAP_S))

    _, accumulated, effective = ledger.read(message.msg_id)
    assert accumulated == pytest.approx(float(BUSY_CREDIT_CAP_S))
    assert effective == message.dead_by, "a caller-set deadline is never extended"

    result = queue.reclaim(now=message.dead_by + timedelta(seconds=1))
    dead = {row.msg_id: row for row in result.dead}
    assert message.msg_id in dead
    assert dead[message.msg_id].reason is DeadReason.EXPIRED


# ------------------------------------------------------------- AC-S1.17 (3)


def test_a_fully_credited_row_still_dies_inside_the_legacy_stall_age(
    queue: SqliteQueueStore, ledger: BusyLedger
) -> None:
    """Clause 3 — the PREDICATE-level one, over a real row's wall-clock age.

    The constants-only clauses in ``check_delivery_orderings`` cannot catch a
    refactor of HOW the credit is applied; this can.  The row's age at death is
    measured from ``created_at``, which is the quantity the legacy notice runs on.
    """
    message = _enqueue(queue)
    ledger.open_episode(message.msg_id, now=T0)
    ledger.close_episode(message.msg_id, now=T0 + timedelta(seconds=BUSY_CREDIT_CAP_S * 5))
    _, _, effective = ledger.read(message.msg_id)
    assert effective is not None

    age_at_death = (effective - message.created_at).total_seconds()
    assert age_at_death <= IDLE_STALL_AGE_S
    assert age_at_death == pytest.approx(DELIVERY_MAX_LIFETIME_S + BUSY_CREDIT_CAP_S)
