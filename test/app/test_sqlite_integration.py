"""The projector, checks and diag views over the REAL SQLite store.

The unit tests run against in-memory doubles, which is what makes them fast and
what makes their failures point at the fold rather than at a database.  This file
exists because a double can only prove the projector is self-consistent.  Three
things it structurally cannot prove, and each is asserted below:

* the projection SURVIVES — an enum, a nullable reason and a boolean all make
  the round trip through eleven TEXT/INTEGER columns and come back as themselves;
* the AC6 column list and lane A's ``worker_state_shadow`` DDL agree, so a column
  renamed on either side is a failure here rather than a silent ``None``;
* ``cao diag`` reads the LIVE database read-only while the writer holds it open,
  which is the concurrency claim AC7 makes and the one a fake cannot test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from test.app.fakes import FakeClock
from typing import Any

import pytest

from cli_agent_orchestrator.adapters.store.event_log import SqliteEventStore
from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.adapters.store.readonly import ReadOnlyPool
from cli_agent_orchestrator.adapters.store.state import SqliteStateStore
from cli_agent_orchestrator.app.diag.report import DiagSources, render_findings, render_timeline
from cli_agent_orchestrator.app.worker_truth.checks import (
    CheckRegistry,
    PaneDisagreementCheck,
    register_phase1_checks,
)
from cli_agent_orchestrator.app.worker_truth.projector import Projector, StaticSourceRegistry
from cli_agent_orchestrator.core.events import (
    AnyKind,
    Confidence,
    DecisionKind,
    EventDraft,
    EventKind,
    Producer,
    WorkerEvent,
)
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.ports import StateProjection
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S

TERMINAL = "term-sql"


class _Rig:
    def __init__(self, db_path: Path) -> None:
        result, pool = migrate(db_path, busy_timeout_ms=5000)
        assert result.ok, result.error
        assert pool is not None
        self.db_path = db_path
        self.pool = pool
        self.clock = FakeClock()
        self.findings = SqliteFindingStore(pool, clock=self.clock)
        self.registry = register_phase1_checks(CheckRegistry(self.findings))
        self.events = SqliteEventStore(pool, clock=self.clock, check_runner=self.registry)
        self.states = SqliteStateStore(pool)
        self.sources = StaticSourceRegistry()
        self.legacy_check = PaneDisagreementCheck(
            self.findings, self.events, self.states, self.clock
        )
        self.projector = Projector(
            self.events, self.states, self.clock, self.sources, legacy_check=self.legacy_check
        )

    def emit(
        self,
        kind: AnyKind,
        *,
        producer: Producer = Producer.JSONL,
        confidence: Confidence = Confidence.AUTHORITATIVE,
        **kw: Any,
    ) -> WorkerEvent:
        stored = self.events.append(
            EventDraft(
                terminal_id=kw.pop("terminal_id", TERMINAL),
                kind=kind,
                producer=producer,
                confidence=confidence,
                observed_at=self.clock.now(),
                **kw,
            )
        )
        self.projector.project(stored)
        return stored

    def legacy(self, status: str, terminal_id: str = TERMINAL) -> WorkerEvent:
        return self.emit(
            EventKind.STATUS_LEGACY_PUBLISHED,
            producer=Producer.PANE,
            confidence=Confidence.DERIVED,
            payload={"latched_status": status, "origin": "incremental"},
            terminal_id=terminal_id,
        )

    def row(self, terminal_id: str = TERMINAL) -> StateProjection:
        """The projection row, asserted present.

        Every caller below has just folded an event for this terminal, so a
        missing row means the fold did nothing — which is worth failing by name
        rather than as an ``AttributeError`` on ``None``.
        """
        projection = self.states.get(terminal_id)
        assert projection is not None, f"no projection row for {terminal_id}"
        return projection


@pytest.fixture
def rig(tmp_path: Path) -> _Rig:
    return _Rig(tmp_path / "cao.db")


def test_the_projection_survives_the_round_trip(rig: _Rig) -> None:
    """Every column AC6 names, written and read back as the value it was.

    An enum, a nullable reason, a nullable prior state, a boolean stored as an
    integer and two nullable timestamps: the five shapes a column mapping gets
    wrong.
    """
    rig.emit(EventKind.TURN_STARTED)
    rig.states.touch_probe(
        TERMINAL, probed_at=rig.clock.now(), pane_present=True, pane_pid=4242, miss_count=0
    )
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(EventKind.PANE_MISSING, producer=Producer.PANE, confidence=Confidence.DERIVED)

    row = rig.row()

    assert row is not None
    assert row.state is WorkerState.DEGRADED
    assert row.degraded_reason is DegradedReason.PANE_UNREADABLE
    assert row.prior_state is WorkerState.BUSY
    assert row.pane_present is True
    assert row.pane_pid == 4242
    assert row.miss_count == 0
    assert row.last_probe_at is not None
    assert row.last_source_probe_at is not None
    assert row.since == rig.clock.now()


def test_a_probe_may_arrive_before_the_projector_has_seen_the_terminal(rig: _Rig) -> None:
    """Otherwise the liveness columns are lost for exactly the terminals an
    operator is most likely to be asking about."""
    rig.states.touch_probe(
        "unseen", probed_at=rig.clock.now(), pane_present=False, pane_pid=None, miss_count=2
    )

    row = rig.states.get("unseen")

    assert row is not None
    assert row.state is WorkerState.STARTING
    assert row.miss_count == 2
    assert row.pane_present is False


def test_a_probe_never_moves_the_state(rig: _Rig) -> None:
    """Heartbeats are columns.  A probe that could change ``state`` would make
    the projector's rules unfalsifiable."""
    rig.emit(EventKind.TURN_STARTED)
    since_before = rig.row().since
    rig.clock.advance(5)

    rig.states.touch_probe(
        TERMINAL, probed_at=rig.clock.now(), pane_present=True, pane_pid=1, miss_count=0
    )
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())

    row = rig.row()
    assert row.state is WorkerState.BUSY
    assert row.since == since_before


def test_all_terminals_feeds_the_sweep(rig: _Rig) -> None:
    for index in range(3):
        rig.emit(EventKind.TURN_STARTED, terminal_id=f"t{index}")

    assert [row.terminal_id for row in rig.states.all_terminals()] == ["t0", "t1", "t2"]

    rig.clock.advance(NO_SIGNAL_S + 1)
    outcomes = rig.projector.sweep()

    assert sorted(o.terminal_id for o in outcomes) == ["t0", "t1", "t2"]
    assert all(
        row.degraded_reason is DegradedReason.NO_SIGNAL for row in rig.states.all_terminals()
    )


def test_findings_and_transitions_land_in_the_real_tables(rig: _Rig) -> None:
    rig.emit(EventKind.USAGE_CAPPED, producer=Producer.PANE, confidence=Confidence.DERIVED)
    rig.emit(EventKind.TURN_STARTED)

    transitions = [
        row for row in rig.events.read(TERMINAL) if row.decision is DecisionKind.STATUS_TRANSITION
    ]
    findings = rig.findings.list_findings(code=FindingCode.DIAG_BAD_TRANSITION)

    assert len(transitions) == 2
    assert transitions[-1].evidence is not None
    assert len(findings) == 1
    assert findings[0].dedupe_key == "capped->busy"


def test_sequences_stay_contiguous_across_the_projector_s_own_writes(rig: _Rig) -> None:
    """B7: a consumer may rely on ``seq + 1``.

    Worth asserting here specifically because the projector appends INTO the same
    log it is reading from, so its decision rows share the terminal's sequence.
    """
    for _ in range(6):
        rig.emit(EventKind.TURN_STARTED)
        rig.emit(EventKind.TURN_ENDED)

    seqs = [row.seq for row in rig.events.read(TERMINAL)]

    assert seqs == list(range(1, len(seqs) + 1))


def test_cao_diag_reads_the_live_database_while_the_writer_holds_it(rig: _Rig) -> None:
    """AC7's concurrency claim, and the one a fake cannot make.

    The writing pool is still open when the read-only pool queries: WAL is what
    permits it, and a reader that needed the writer to let go would be useless
    for diagnosing a stall, which is by definition something happening now.
    """
    rig.emit(EventKind.SESSION_STARTED, source_ref="rollout:/tmp/r.jsonl#0")
    rig.emit(EventKind.TURN_STARTED, msg_id="MSG1")
    rig.clock.advance(NO_SIGNAL_S + 1)
    rig.projector.sweep()

    reader = ReadOnlyPool(rig.db_path, busy_timeout_ms=5000)
    sources = DiagSources(
        events=SqliteEventStore(reader, clock=rig.clock),
        states=SqliteStateStore(reader),
        findings=SqliteFindingStore(reader, clock=rig.clock),
    )
    text = render_timeline(sources, TERMINAL, now=rig.clock.now())
    reader.close_all()

    assert "degraded(no_signal)" in text
    assert EventKind.TURN_STARTED.value in text
    assert DecisionKind.STATUS_TRANSITION.value in text
    assert "msg=MSG1" in text
    assert "rollout:/tmp/r.jsonl#0" in text


def test_the_read_only_pool_refuses_a_write(rig: _Rig) -> None:
    """SQLite enforces it, not a convention this code could forget."""
    import sqlite3

    reader = ReadOnlyPool(rig.db_path, busy_timeout_ms=5000)
    try:
        with pytest.raises(sqlite3.OperationalError):
            reader.connection().execute("DELETE FROM worker_event")
    finally:
        reader.close_all()


def test_a_bulk_read_returns_both_sides_from_the_real_store(rig: _Rig) -> None:
    """Both producers' rows come back from SQLite, in order, at volume.

    This used to assert the AC10 agreement report over the same rows; the report
    went with shadow-live mode (#738). What it was really exercising survives and
    is what is asserted now: a reader asking the REAL store for the whole log
    gets every projected-side and every legacy-side row, rather than a fake's
    tidy list. A store that dropped one side would have made the old report
    silently one-sided, and would make any future bulk reader wrong the same way.
    """
    for index in range(3):
        terminal = f"t{index}"
        for _ in range(30):
            rig.emit(EventKind.TURN_STARTED, terminal_id=terminal)
            rig.legacy("processing", terminal_id=terminal)
            rig.clock.advance(1)
            rig.emit(EventKind.TURN_ENDED, terminal_id=terminal)
            rig.legacy("idle", terminal_id=terminal)
            rig.clock.advance(1)

    rows = rig.events.read()

    legacy = [row for row in rows if row.kind is EventKind.STATUS_LEGACY_PUBLISHED]
    projected = [row for row in rows if row.decision is DecisionKind.STATUS_TRANSITION]

    # Both sides, at volume, for every terminal. Counted per side rather than as
    # one total: the rig also folds, so the log carries transition rows the loop
    # never appends, and a total would silently absorb one side going missing.
    assert len(legacy) == 3 * 30 * 2
    assert len(projected) == 3 * 30 * 2
    assert {row.terminal_id for row in legacy} == {"t0", "t1", "t2"}
    assert {row.terminal_id for row in projected} == {"t0", "t1", "t2"}
    # A fleet-wide read orders by ``ingested_at``, not by ``seq`` — ``seq`` is
    # per terminal, so the fleet view interleaves three sequences. Monotonicity
    # is therefore asserted WITHIN a terminal, which is where it is promised.
    assert [row.ingested_at for row in rows] == sorted(row.ingested_at for row in rows)
    for terminal in ("t0", "t1", "t2"):
        seqs = [row.seq for row in rows if row.terminal_id == terminal]
        assert seqs == sorted(seqs)


def test_findings_render_from_the_real_store(rig: _Rig) -> None:
    rig.emit(EventKind.USAGE_CAPPED, producer=Producer.PANE, confidence=Confidence.DERIVED)
    rig.emit(EventKind.TURN_STARTED)

    sources = DiagSources(events=rig.events, states=rig.states, findings=rig.findings)
    text = render_findings(sources, now=rig.clock.now())

    assert FindingCode.DIAG_BAD_TRANSITION.value in text
    assert "capped -> busy" in text


def test_timestamps_come_back_as_aware_utc(rig: _Rig) -> None:
    """A naive stamp would sort correctly against other naive ones and wrongly
    against everything else — the class of bug AC10 must not have to explain."""
    rig.emit(EventKind.TURN_STARTED)

    row = rig.row()
    event = rig.events.read(TERMINAL)[0]

    assert row.since.tzinfo is not None
    assert row.since.utcoffset() == timedelta(0)
    assert event.ingested_at.tzinfo is not None
    assert event.ingested_at.astimezone(UTC) == event.ingested_at


def test_a_projection_written_before_a_restart_is_still_there(rig: _Rig) -> None:
    """The projected state is durable, not a cache.  Phase 2's ``status_monitor`` will
    read it after a bounce, and the agreement session spans one."""
    rig.emit(EventKind.TURN_STARTED)
    rig.pool.close_all()

    reopened = SqliteStateStore(ReadOnlyPool(rig.db_path, busy_timeout_ms=5000))
    row = reopened.get(TERMINAL)

    assert row is not None
    assert row.state is WorkerState.BUSY


def test_the_epoch_sentinel_is_never_reached_in_practice(rig: _Rig) -> None:
    """Every projector path sets ``since``; the sentinel exists only so a write
    can never raise.  Asserted so a future path that forgets is visible."""
    rig.emit(EventKind.TURN_STARTED)
    rig.emit(EventKind.TURN_ENDED)
    rig.clock.advance(NO_SIGNAL_S + 1)
    rig.projector.sweep()

    assert rig.row().since > datetime(2000, 1, 1, tzinfo=UTC)


def test_a_row_read_from_the_table_is_frozen(rig: _Rig) -> None:
    """A row read out of the table is a snapshot of what was stored.

    Nothing should be able to edit it into something the database never held —
    least of all the read-only ``cao diag`` path, where a mutated row would
    render as fact.
    """
    import dataclasses

    rig.emit(EventKind.TURN_STARTED)
    row = rig.row()

    with pytest.raises(dataclasses.FrozenInstanceError):
        row.state = WorkerState.EXITED  # type: ignore[misc]


def test_upsert_writes_the_projection_half_and_leaves_the_liveness_half(rig: _Rig) -> None:
    """WP-ARCH 2b (R2): the projector is not a writer of the liveness columns.

    ``touch_probe`` and ``touch_source_probe`` are owned by the liveness probe
    and the rollout tailer, and neither takes the projector's lock — neither has
    any business waiting on a fold.  So a full-row ``upsert`` would silently
    overwrite a heartbeat that landed between ``_load`` and the write with a
    value seconds old, and the two readers of those stamps make that expensive:
    ``_last_signal`` would judge a live terminal silent and degrade it, and
    ``_source_healthy`` would flip ``is_projected`` off for a healthy lane.

    The stale row written below is exactly what a fold holds after a heartbeat
    lands inside its critical section.  Against a full-row write every liveness
    assertion here fails.
    """
    from cli_agent_orchestrator.app.worker_truth.projector import ProjectedState

    rig.emit(EventKind.TURN_STARTED)
    probed_at = rig.clock.now()
    rig.states.touch_probe(
        TERMINAL, probed_at=probed_at, pane_present=True, pane_pid=4242, miss_count=3
    )
    rig.states.touch_source_probe(TERMINAL, probed_at=probed_at)

    rig.states.upsert(
        ProjectedState(
            terminal_id=TERMINAL,
            state=WorkerState.IDLE,
            since=probed_at,
            last_event_seq=99,
            last_probe_at=None,
            last_source_probe_at=None,
            pane_pid=None,
            pane_present=False,
            miss_count=0,
        )
    )

    row = rig.row()
    assert row is not None
    # The projection half applied...
    assert row.state is WorkerState.IDLE
    assert row.last_event_seq == 99
    # ...and the liveness half is untouched.
    assert row.last_probe_at == probed_at
    assert row.last_source_probe_at == probed_at
    assert row.pane_pid == 4242
    assert row.pane_present is True
    assert row.miss_count == 3
