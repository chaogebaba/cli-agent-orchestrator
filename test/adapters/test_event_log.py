"""AC3 — the append-only event log (WP-ARCH phase 1, F725 #581).

The tests that matter here are the last three: contiguity under concurrency,
contiguity across an injected crash between the two statements, and the
partial-index reads.  Everything above them is the shape of a row.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.event_log import SqliteEventStore
from cli_agent_orchestrator.core.events import (
    Confidence,
    DecisionKind,
    EventKind,
    Producer,
    WorkerEvent,
)

from .conftest import FakeClock, draft


def test_append_returns_a_stored_row(store: SqliteEventStore, clock: FakeClock) -> None:
    event = store.append(draft())
    assert event.seq == 1
    assert event.ingested_at == clock.now()
    assert len(event.event_id) == 26
    assert isinstance(event, WorkerEvent)


def test_seq_starts_at_one_and_increments_per_terminal(store: SqliteEventStore) -> None:
    """Sequences are PER TERMINAL — two terminals do not share a counter."""
    assert [store.append(draft("t-1")).seq for _ in range(3)] == [1, 2, 3]
    assert [store.append(draft("t-2")).seq for _ in range(2)] == [1, 2]
    assert store.high_water("t-1") == 3
    assert store.high_water("t-2") == 2
    assert store.high_water("never-seen") == 0


def test_round_trip_preserves_every_column(store: SqliteEventStore) -> None:
    """Including the four the audit spells out: run_id, msg_id, decision, evidence."""
    truth = store.append(
        draft(
            payload={"turn": 4, "nested": {"a": [1, 2]}},
            source_ref="rollout:/tmp/r.jsonl#4096",
            run_id="01J00000000000000000000RUN",
            msg_id="01J00000000000000000000MSG",
        )
    )
    decision = store.append(
        draft(
            kind=DecisionKind.DELIVERY_ATTEMPT,
            decision=DecisionKind.DELIVERY_ATTEMPT,
            producer=Producer.SERVER,
            confidence=Confidence.AUTHORITATIVE,
            evidence=truth.event_id,
            msg_id="01J00000000000000000000MSG",
        )
    )

    stored_truth = store.get(truth.event_id)
    assert stored_truth is not None
    assert stored_truth.payload == {"turn": 4, "nested": {"a": [1, 2]}}
    assert stored_truth.source_ref == "rollout:/tmp/r.jsonl#4096"
    assert stored_truth.run_id == "01J00000000000000000000RUN"
    assert stored_truth.decision is None
    assert stored_truth.evidence is None

    stored_decision = store.get(decision.event_id)
    assert stored_decision is not None
    assert stored_decision.decision is DecisionKind.DELIVERY_ATTEMPT
    assert stored_decision.evidence == truth.event_id


def test_get_returns_none_for_an_unknown_id(store: SqliteEventStore) -> None:
    assert store.get("01J0000000000000000000MISS") is None


def test_timestamps_survive_the_round_trip_as_aware_utc(store: SqliteEventStore) -> None:
    observed = datetime(2026, 9, 2, 11, 59, 30, 123456, tzinfo=UTC)
    event = store.append(draft(observed_at=observed))
    stored = store.get(event.event_id)
    assert stored is not None
    assert stored.observed_at == observed
    assert stored.observed_at.tzinfo is not None


def test_read_orders_by_seq_within_a_terminal(store: SqliteEventStore) -> None:
    for _ in range(5):
        store.append(draft("t-1"))
    assert [event.seq for event in store.read("t-1")] == [1, 2, 3, 4, 5]
    assert [event.seq for event in store.read("t-1", since_seq=3)] == [4, 5]
    assert len(store.read("t-1", limit=2)) == 2


def test_read_filters_by_kind(store: SqliteEventStore) -> None:
    store.append(draft("t-1", kind=EventKind.TURN_STARTED))
    store.append(draft("t-1", kind=EventKind.TURN_ENDED))
    store.append(draft("t-1", kind=EventKind.TOOL_CALLED))
    kinds = frozenset({EventKind.TURN_STARTED, EventKind.TOOL_CALLED})
    assert [event.kind for event in store.read("t-1", kinds=kinds)] == [
        EventKind.TURN_STARTED,
        EventKind.TOOL_CALLED,
    ]


def test_fleet_wide_read_orders_by_ingestion(store: SqliteEventStore, clock: FakeClock) -> None:
    """A fleet read has no single sequence, so it orders by ``ingested_at`` (AC10)."""
    store.append(draft("t-1"))
    clock.advance(seconds=1)
    store.append(draft("t-2"))
    clock.advance(seconds=1)
    store.append(draft("t-1"))
    assert [event.terminal_id for event in store.read()] == ["t-1", "t-2", "t-1"]


def test_read_since_filters_by_ingestion_time(store: SqliteEventStore, clock: FakeClock) -> None:
    store.append(draft("t-1"))
    clock.advance(hours=1)
    cutoff = clock.now()
    store.append(draft("t-1"))
    assert [event.seq for event in store.read("t-1", since=cutoff)] == [2]


def test_payload_is_stored_as_json_text(store: SqliteEventStore, pool: ConnectionPool) -> None:
    """The DDL says ``payload TEXT NOT NULL`` — assert it really is JSON text."""
    event = store.append(draft(payload={"b": 1, "a": 2}))
    raw = (
        pool.connection()
        .execute("SELECT payload FROM worker_event WHERE event_id = ?", (event.event_id,))
        .fetchone()["payload"]
    )
    assert json.loads(raw) == {"a": 2, "b": 1}
    # Keys are sorted on write so two equal payloads produce identical text.
    assert raw == '{"a": 2, "b": 1}'


@pytest.mark.integration
def test_sequences_are_contiguous_under_concurrent_appenders(
    pool: ConnectionPool, clock: FakeClock
) -> None:
    """Two threads appending to the SAME terminal produce 1..N with no gaps or ties.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so the two appenders
    serialise at the transaction rather than racing between the read of the
    high-water mark and its write.
    """
    store = SqliteEventStore(pool, clock=clock)
    appended = 60
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(appended):
                store.append(draft("t-shared"))
        except BaseException as exc:  # noqa: BLE001 — re-raised via the collected list
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    seqs = [event.seq for event in store.read("t-shared")]
    assert seqs == list(range(1, appended * 2 + 1))
    assert len(set(seqs)) == len(seqs)
    assert store.high_water("t-shared") == appended * 2


@pytest.mark.integration
def test_crash_between_the_two_statements_leaves_no_gap(
    pool: ConnectionPool, clock: FakeClock
) -> None:
    """The named phase-1 mutant, killed.

    A crash injected AFTER the high-water bump and BEFORE the insert must roll
    both back: the next append reuses the sequence, and the log stays contiguous.
    Split the bump and the insert into two transactions and this test fails with
    a gap at 3 — which is the whole reason it exists.
    """
    store = SqliteEventStore(pool, clock=clock)
    store.append(draft("t-1"))
    store.append(draft("t-1"))

    def explode() -> None:
        raise RuntimeError("simulated crash between the bump and the insert")

    store._after_seq_bump = explode
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.append(draft("t-1"))

    # The bump rolled back with the insert: nothing was consumed.
    assert store.high_water("t-1") == 2

    store._after_seq_bump = None
    resumed = store.append(draft("t-1"))
    assert resumed.seq == 3
    assert [event.seq for event in store.read("t-1")] == [1, 2, 3]


@pytest.mark.integration
def test_a_failed_append_is_invisible_to_other_terminals(
    pool: ConnectionPool, clock: FakeClock
) -> None:
    """A rollback on one terminal must not disturb another's sequence."""
    store = SqliteEventStore(pool, clock=clock)
    store.append(draft("t-2"))

    store._after_seq_bump = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        store.append(draft("t-1"))
    store._after_seq_bump = None

    assert store.append(draft("t-2")).seq == 2
    assert store.append(draft("t-1")).seq == 1
