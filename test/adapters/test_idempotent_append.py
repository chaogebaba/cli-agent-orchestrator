"""AC-2a, the retried hook — the idempotent fold against real SQLite (D4).

The criterion has TWO halves and the blueprint insists on both: *"The same POST
replayed with the same ``idempotency_key`` appends one row; replayed with a fresh
key it appends two.  Both halves are required: the first proves the index, the
second proves the key is caller-supplied rather than derived from content."*

A fake store cannot carry this.  The guarantee lives in a partial unique index
over a nullable column, so it is asserted here against the real migrator and the
real store, which is where a mutant can actually break it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.store.event_log import SqliteEventStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.core.events import (
    Confidence,
    EventDraft,
    EventKind,
    Producer,
    SourceRefScheme,
    source_ref,
)

TEST_BUSY_TIMEOUT_MS = 2000
TERMINAL = "t-1"


class _Clock:
    def __init__(self) -> None:
        self._now = datetime(2026, 9, 5, tzinfo=UTC)

    def now(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


@pytest.fixture
def store(tmp_path: Path) -> SqliteEventStore:
    result, pool = migrate(tmp_path / "cao.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    return SqliteEventStore(pool, clock=_Clock())


def _marker(key: str | None, *, event: str = "PreToolUse") -> EventDraft:
    return EventDraft(
        terminal_id=TERMINAL,
        kind=EventKind.PROMPT_AWAITING,
        producer=Producer.HOOK,
        confidence=Confidence.AUTHORITATIVE,
        observed_at=datetime(2026, 9, 5, tzinfo=UTC),
        source_ref=source_ref(SourceRefScheme.HOOK, event, key or "none"),
        idempotency_key=key,
        payload={"marker_kind": "question_open"},
    )


def test_the_same_key_appends_one_row(store: SqliteEventStore) -> None:
    """Half one: the index."""
    first = store.append(_marker("k-1"))
    second = store.append(_marker("k-1"))

    assert store.read(TERMINAL).__len__() == 1
    assert second.event_id == first.event_id
    assert second.seq == first.seq


def test_a_fresh_key_appends_a_second_row(store: SqliteEventStore) -> None:
    """Half two: the key is CALLER-supplied, not derived from content.

    The two drafts here are identical in every field but the key.  A store that
    deduplicated by content hash would collapse them — and would then also collapse
    two genuinely distinct turns that happened to look alike, and hide the producer
    disagreement the agreement report exists to measure.
    """
    store.append(_marker("k-1"))
    store.append(_marker("k-2"))

    assert len(store.read(TERMINAL)) == 2


def test_a_duplicate_consumes_no_sequence_number(store: SqliteEventStore) -> None:
    """B7 makes gaps illegal, and an early return AFTER the high-water bump would
    open one on every retry.

    Given ``seq``, the next event is ``seq + 1`` and a missing row means "not yet",
    never "lost" — the property the projector, a replay consumer and phase 7's
    replay-from-seq all lean on.  This is the ordering mutant: move the
    idempotency lookup below the bump and the rows stay correct while the
    sequence quietly grows holes.
    """
    store.append(_marker("k-1"))
    store.append(_marker("k-1"))
    store.append(_marker("k-2"))

    assert [row.seq for row in store.read(TERMINAL)] == [1, 2]
    assert store.high_water(TERMINAL) == 2


def test_rows_without_a_key_are_never_deduplicated(store: SqliteEventStore) -> None:
    """The index is PARTIAL, and it has to be.

    Almost no row carries a key — only the hook route can be retried by a
    transport this server does not control.  SQLite treats NULLs as distinct in a
    unique index, so a full index would be vacuous where it was not expensive; the
    partial one says the same thing explicitly.
    """
    for _ in range(3):
        store.append(_marker(None))

    assert len(store.read(TERMINAL)) == 3


def test_the_key_survives_the_round_trip(store: SqliteEventStore) -> None:
    stored = store.append(_marker("k-1"))
    assert store.get(stored.event_id).idempotency_key == "k-1"


def test_the_partial_unique_index_exists_and_is_partial(tmp_path: Path) -> None:
    """Asserted on the SCHEMA, not only through behaviour.

    A store that happened to deduplicate in Python would pass every case above and
    leave the database with no constraint at all — so a second writer, or a repair
    tool, could still double a row.
    """
    result, pool = migrate(tmp_path / "cao.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    row = (
        pool.connection()
        .execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("ux_worker_event_idempotency",),
        )
        .fetchone()
    )
    assert row is not None, "the D4 index is missing"
    sql = row["sql"].lower()
    assert "unique" in sql
    assert "where idempotency_key is not null" in sql
    pool.close_all()


def test_the_migration_is_additive_and_survives_a_second_boot(tmp_path: Path) -> None:
    """The migrator runs at EVERY boot, and ``ALTER TABLE ADD COLUMN`` has no
    ``IF NOT EXISTS`` in SQLite.

    A bare ALTER would succeed once and fail forever after — and a failed step
    aborts the whole migration and disables ingestion for the process, so the
    SECOND boot would silently turn phase 1 off.  That is the mutant: it passes
    every fresh-database test there is.
    """
    db_path = tmp_path / "cao.db"
    first, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert first.ok and pool is not None
    pool.close_all()

    second, pool2 = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert second.ok, second.error
    assert pool2 is not None
    pool2.close_all()


def test_the_column_is_added_to_a_database_created_before_phase_2(tmp_path: Path) -> None:
    """The upgrade path, which a fresh-database test cannot reach.

    A phase-1 database has the fourteen-column ``worker_event`` and
    ``CREATE TABLE IF NOT EXISTS`` will not add the fifteenth to it, so without
    the ALTER step every hook append would fail on a server that has been running
    since before this phase — which is every server that matters.
    """
    db_path = tmp_path / "cao.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE worker_event ("
        "event_id TEXT PRIMARY KEY, terminal_id TEXT NOT NULL, seq INTEGER NOT NULL, "
        "kind TEXT NOT NULL, producer TEXT NOT NULL, confidence TEXT NOT NULL, "
        "observed_at TEXT NOT NULL, ingested_at TEXT NOT NULL, payload TEXT NOT NULL, "
        "source_ref TEXT, run_id TEXT, msg_id TEXT, decision TEXT, evidence TEXT, "
        "UNIQUE(terminal_id, seq))"
    )
    conn.commit()
    conn.close()

    result, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)

    assert result.ok, result.error
    assert pool is not None
    columns = {row[1] for row in pool.connection().execute("PRAGMA table_info(worker_event)")}
    assert "idempotency_key" in columns
    pool.close_all()
