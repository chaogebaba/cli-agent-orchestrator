"""AC-2a's agreement report gets BOTH sides, end to end (WP-ARCH phase 2, A1).

This is the criterion the fold driver exists for, asserted against the real
composition root rather than against a fake.

AC-2a compares the projection against the legacy published status.  The
projection side of that comparison is `status.transition` rows, and those are
written by `Projector.project` and by nothing else in the tree.  At phase 1's
anchor the projector had no call site, so the report could only ever see the
legacy side — it would not error, it would report on one input and fail the
content floor as "no evidence".

The arm difference here is the whole point.  A green run with a folder proves
nothing on its own; the same session with `folder=None` is what shows the report
losing a side, which is exactly the mutant A1 names.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.store.event_log import SqliteEventStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.adapters.store.state import SqliteStateStore
from cli_agent_orchestrator.adapters.truth import wiring
from cli_agent_orchestrator.app.worker_truth.agreement import build_agreement_report
from cli_agent_orchestrator.app.worker_truth.projector import Projector, StaticSourceRegistry
from cli_agent_orchestrator.core.events import (
    Confidence,
    DecisionKind,
    EventDraft,
    EventKind,
    Producer,
)

TERMINAL = "t-fold"


class _Clock:
    """Advances a second per read, so ordering assertions are meaningful."""

    def __init__(self) -> None:
        self._now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


@pytest.fixture
def store_and_projector(tmp_path: Path) -> Iterator[tuple[SqliteEventStore, Projector]]:
    result, pool = migrate(tmp_path / "cao.db", busy_timeout_ms=5000)
    assert result.ok and pool is not None
    clock = _Clock()
    events = SqliteEventStore(pool, clock=clock)
    states = SqliteStateStore(pool)
    projector = Projector(events, states, clock, StaticSourceRegistry())
    yield events, projector
    wiring.reset_producers()
    pool.close_all()


def _turn(kind: EventKind) -> EventDraft:
    return EventDraft(
        terminal_id=TERMINAL,
        kind=kind,
        producer=Producer.JSONL,
        confidence=Confidence.AUTHORITATIVE,
        observed_at=datetime(2026, 9, 5, tzinfo=UTC),
    )


def _legacy(status: str) -> EventDraft:
    return EventDraft(
        terminal_id=TERMINAL,
        kind=EventKind.STATUS_LEGACY_PUBLISHED,
        producer=Producer.PANE,
        confidence=Confidence.DERIVED,
        observed_at=datetime(2026, 9, 5, tzinfo=UTC),
        payload={"latched_status": status, "origin": "incremental", "fed_by": "pane"},
    )


def _drive_a_session() -> None:
    """One worker going busy and idle, with the pane agreeing each time."""
    wiring.emit(_turn(EventKind.TURN_STARTED))
    wiring.emit(_legacy("processing"))
    wiring.emit(_turn(EventKind.TURN_ENDED))
    wiring.emit(_legacy("idle"))


def test_with_the_fold_driven_the_report_has_both_sides(
    store_and_projector: tuple[SqliteEventStore, Projector],
) -> None:
    """The criterion, against the real store and the real projector."""
    events, projector = store_and_projector
    wiring.install_producers(wiring.ProducerRuntime(store=events, clock=_Clock(), folder=projector))

    _drive_a_session()

    rows = events.read(TERMINAL)
    transitions = [r for r in rows if r.decision is DecisionKind.STATUS_TRANSITION]
    assert transitions, "the fold wrote no status.transition rows"

    report = build_agreement_report(rows)
    assert report.total_transitions > 0, "the projection side is empty"
    assert report.total_legacy_publishes > 0, "the legacy side is empty"
    assert report.total_comparisons > 0, "nothing was comparable"


def test_without_the_fold_the_projection_side_is_empty(
    store_and_projector: tuple[SqliteEventStore, Projector],
) -> None:
    """The arm difference, and the mutant A1 names.

    Identical session, identical rows appended, folder removed. The legacy side is
    untouched and the projection side vanishes — so the report has nothing to
    compare and AC-2a can produce no evidence at all. That the report does not
    RAISE here is the dangerous part: without the arm difference this failure
    reads as a quiet, well-behaved report of one input.
    """
    events, _ = store_and_projector
    wiring.install_producers(wiring.ProducerRuntime(store=events, clock=_Clock(), folder=None))

    _drive_a_session()

    rows = events.read(TERMINAL)
    assert [r for r in rows if r.decision is DecisionKind.STATUS_TRANSITION] == []

    report = build_agreement_report(rows)
    assert report.total_transitions == 0
    assert report.total_legacy_publishes > 0, "the legacy side should be unaffected"
    assert report.total_comparisons == 0


def test_the_fold_moves_the_durable_projection(
    store_and_projector: tuple[SqliteEventStore, Projector], tmp_path: Path
) -> None:
    """``worker_state_shadow`` is the row every later phase reads.

    The transition rows above are the report's input; this is the state the fleet
    will eventually be served from, and it is written by the same fold. A driver
    that produced decision rows without moving the projection would satisfy the
    report and leave phase 2's actual output empty.
    """
    events, projector = store_and_projector
    wiring.install_producers(wiring.ProducerRuntime(store=events, clock=_Clock(), folder=projector))

    wiring.emit(_turn(EventKind.TURN_STARTED))

    row = projector._states.get(TERMINAL)  # noqa: SLF001 — the durable side under test
    assert row is not None
    assert row.state.value == "busy"
    assert row.last_event_seq == 1


def test_a_producer_delivered_decision_row_moves_nothing(
    store_and_projector: tuple[SqliteEventStore, Projector],
) -> None:
    """The third mutant A1 names, end to end.

    ``server_decisions`` and ``legacy_egress`` push decision rows through ``emit``,
    so they reach the fold. The projector's ``decision_row`` branch is what stops
    them moving the projection — remove it and a ``delivery.attempt`` would.
    """
    events, projector = store_and_projector
    wiring.install_producers(wiring.ProducerRuntime(store=events, clock=_Clock(), folder=projector))
    wiring.emit(_turn(EventKind.TURN_STARTED))
    before = projector._states.get(TERMINAL)  # noqa: SLF001
    assert before is not None

    wiring.emit(
        EventDraft(
            terminal_id=TERMINAL,
            kind=DecisionKind.DELIVERY_ATTEMPT,
            producer=Producer.SERVER,
            confidence=Confidence.DERIVED,
            observed_at=datetime(2026, 9, 5, tzinfo=UTC),
            decision=DecisionKind.DELIVERY_ATTEMPT,
            evidence="e0",
            payload={"outcome": "confirmed"},
        )
    )

    after = projector._states.get(TERMINAL)  # noqa: SLF001
    assert after is not None
    assert after.state is before.state
    assert after.since == before.since


def test_a_decision_row_creates_no_projection_for_an_unseen_terminal(
    store_and_projector: tuple[SqliteEventStore, Projector],
) -> None:
    """What the ``decision_row`` branch UNIQUELY does, found by a surviving mutant.

    The obvious assertion — "a producer's ``delivery.attempt`` must not move the
    projection" — does not discriminate, and a mutation run is what showed it:
    with the branch removed the row still moves nothing, because
    ``implied_state`` returns ``None`` for every ``DecisionKind`` and the
    ``no_implied_state`` path leaves the state alone. Two independent guards, and
    the second one masks the first.

    What the branch alone prevents is the SIDE EFFECT on the way there. Without
    it, ``project`` loads the terminal's row — creating one at ``starting`` for a
    terminal the projector has never seen — and upserts it to advance
    ``last_event_seq``. So a server decision about a terminal that has produced no
    worker truth would conjure a projection row out of nothing, and the sweep's
    own guard against exactly that ("a terminal with NO events is skipped")
    reasons about the event log rather than about this row.

    Hence the discriminating case: a decision row for a terminal with no history
    must leave the projection with no row at all.
    """
    events, projector = store_and_projector
    wiring.install_producers(wiring.ProducerRuntime(store=events, clock=_Clock(), folder=projector))

    wiring.emit(
        EventDraft(
            terminal_id="t-never-seen",
            kind=DecisionKind.DELIVERY_ATTEMPT,
            producer=Producer.SERVER,
            confidence=Confidence.DERIVED,
            observed_at=datetime(2026, 9, 5, tzinfo=UTC),
            decision=DecisionKind.DELIVERY_ATTEMPT,
            evidence="e0",
            payload={"outcome": "confirmed"},
        )
    )

    assert events.read("t-never-seen"), "the decision row itself must still be stored"
    assert projector._states.get("t-never-seen") is None  # noqa: SLF001
