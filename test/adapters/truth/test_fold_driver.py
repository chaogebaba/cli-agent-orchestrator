"""The fold driver (WP-ARCH phase 2, amendment A1).

At phase 1's anchor `Projector.project` had **zero call sites**. The composition
root built the projector and then handed the producer runtime the store, the
clock, the state store and the finding store — everything except the projector —
so the local was dropped and no appended event was ever folded.

That is not a cosmetic gap. The fold is what writes `status.transition`, and
AC-2a's agreement report compares exactly those rows against
`status.legacy_published`. Without a driver the report has one side, which is the
same shape D5's check had before D1c, arriving through the other half of the
projector.

A1 puts the driver at `emit` — phase 1's single path from a hook to the store —
and reaches the projector through a `core.ports` Protocol rather than by calling
it, because `adapters/` may not import `app/`. The port is the fix; a field typed
on the concrete class would restore the forbidden import while looking like a
port.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cli_agent_orchestrator.adapters.truth import wiring
from cli_agent_orchestrator.core.events import (
    Confidence,
    DecisionKind,
    EventDraft,
    EventKind,
    Producer,
    WorkerEvent,
)
from cli_agent_orchestrator.core.ports import StateFolder

from .conftest import FakeClock, FakeEventStore

TERMINAL = "t-1"


class RecordingFolder:
    """A `StateFolder` that records what it was asked to fold."""

    def __init__(self) -> None:
        self.folded: list[WorkerEvent] = []

    def project(self, event: WorkerEvent) -> object:
        self.folded.append(event)
        return None


class ExplodingFolder:
    """A projector with a bug, which must never reach the caller."""

    def __init__(self) -> None:
        self.calls = 0

    def project(self, event: WorkerEvent) -> object:
        self.calls += 1
        raise RuntimeError("projector exploded")


def _draft(kind: Any = EventKind.TURN_STARTED, **overrides: Any) -> EventDraft:
    fields: dict[str, Any] = {
        "terminal_id": TERMINAL,
        "kind": kind,
        "producer": Producer.JSONL,
        "confidence": Confidence.AUTHORITATIVE,
        "observed_at": datetime(2026, 9, 5, tzinfo=UTC),
    }
    fields.update(overrides)
    return EventDraft(**fields)


def _install(store: FakeEventStore, folder: object | None) -> None:
    wiring.install_producers(
        wiring.ProducerRuntime(store=store, clock=FakeClock(), folder=folder)  # type: ignore[arg-type]
    )


# -- the driver itself -------------------------------------------------------


def test_every_emitted_event_is_folded(store: FakeEventStore) -> None:
    """The mutant A1 names: remove the fold call.

    With it gone the projection never moves, so AC-2a's agreement report loses
    its projection side entirely and reports on one input — which the content
    floor then fails as "no evidence", the honest outcome but not the one the
    sub-phase is meant to produce.
    """
    folder = RecordingFolder()
    _install(store, folder)

    wiring.emit(_draft())
    wiring.emit(_draft(EventKind.TURN_ENDED))

    assert [event.kind for event in folder.folded] == [
        EventKind.TURN_STARTED,
        EventKind.TURN_ENDED,
    ]


def test_the_fold_receives_the_STORED_event_not_the_draft(store: FakeEventStore) -> None:
    """The projector needs ``seq``, ``event_id`` and ``ingested_at``.

    All three are minted by the store inside its transaction and none exists on a
    draft, so folding the draft would hand the projector a row it cannot order,
    cannot cite as evidence, and cannot record as ``last_event_seq``.
    """
    folder = RecordingFolder()
    _install(store, folder)

    returned = wiring.emit(_draft())

    assert returned is not None
    folded = folder.folded[0]
    assert folded.event_id == returned.event_id
    assert folded.seq == returned.seq == 1
    assert folded.ingested_at == returned.ingested_at


def test_the_fold_runs_after_the_append_returns(store: FakeEventStore) -> None:
    """Ordering, which at this seam is free rather than disciplined.

    The projector's own ``_append_decision`` calls the store, so a fold placed
    inside an open transaction would nest one and fail every transition. Here the
    row is already in the store by the time the fold sees it, and this asserts
    that rather than assuming it.
    """
    seen: list[int] = []

    class _Observer:
        def project(self, event: WorkerEvent) -> object:
            seen.append(len(store.rows))
            return None

    _install(store, _Observer())
    wiring.emit(_draft())

    assert seen == [1], "the fold ran before the append landed"


def test_a_folder_that_raises_never_reaches_the_caller(store: FakeEventStore) -> None:
    """The mutant A1 names: let an exception out of the fold.

    ``emit`` swallows every ``Exception`` by design, and the fold inherits that
    promise because it now rides the one path every producer takes. A projector
    bug that escaped would reach the status publish path, which is precisely
    AC11's no-behaviour-change claim.
    """
    folder = ExplodingFolder()
    _install(store, folder)

    wiring.emit(_draft())  # must not raise

    assert folder.calls == 1


def test_a_failing_fold_still_returns_the_stored_event(store: FakeEventStore) -> None:
    """The append succeeded, so its ``event_id`` is a caller's evidence.

    ``legacy_egress`` keeps the returned id as the observation a later
    ``fleet.override`` cites, and ``server_decisions`` reads it the same way.
    Returning ``None`` because a diagnostic raised would turn a projector bug into
    a missing evidence chain — and ``DIAG-GHOST-TRANSITION`` would then correctly
    complain about a decision citing nothing.
    """
    _install(store, ExplodingFolder())

    stored = wiring.emit(_draft())

    assert stored is not None
    assert len(store.rows) == 1
    assert stored.event_id == store.rows[0].event_id


def test_a_failing_APPEND_is_never_folded(store: FakeEventStore) -> None:
    """There is no event to fold, and inventing one would corrupt the projection."""
    folder = RecordingFolder()
    _install(store, folder)
    store.fail_next = True

    assert wiring.emit(_draft()) is None
    assert folder.folded == []


def test_the_runtime_works_without_a_folder(store: FakeEventStore) -> None:
    """Optional, the shape the store's own ``CheckRunner`` already has.

    A lane bringing producers up without a projector still appends — strictly
    less information, never wrong information.
    """
    _install(store, None)

    assert wiring.emit(_draft()) is not None
    assert len(store.rows) == 1


def test_with_ingestion_off_nothing_is_appended_or_folded(store: FakeEventStore) -> None:
    folder = RecordingFolder()
    _install(store, folder)
    wiring.reset_producers()

    assert wiring.emit(_draft()) is None
    assert store.rows == []
    assert folder.folded == []


# -- the port is a port ------------------------------------------------------


def test_the_runtime_field_is_typed_on_the_protocol_not_the_projector() -> None:
    """The mutant A1 names first, and the one that looks harmless.

    A field annotated ``Projector`` would restore the ``adapters -> app`` import
    while still reading like a port, and ``test_adapters_never_import_app`` is
    what fails on it. Asserting the ANNOTATION rather than the behaviour is the
    point: both typings behave identically at runtime, so only the annotation
    distinguishes a real port from a decorative one.
    """
    import typing

    hints = typing.get_type_hints(wiring.ProducerRuntime)
    assert hints["folder"] == (StateFolder | None)


def test_the_projector_satisfies_the_port_structurally_not_by_inheritance() -> None:
    """Structural satisfaction is the whole point of using a Protocol here.

    ``issubclass`` against a runtime-checkable Protocol answers "does it have the
    method", so it is True and SHOULD be — that is the structural check. What must
    NOT be true is inheritance: if ``Projector`` had to subclass the port, ``app``
    would import ``core.ports`` for its own shape and the composition root would
    no longer be the place the two are reconciled. The distinction is visible only
    in the MRO, which is what this reads.
    """
    from cli_agent_orchestrator.app.worker_truth.projector import Projector

    assert issubclass(Projector, StateFolder), "the projector no longer fits the port"
    assert StateFolder not in Projector.__mro__, "the port became a base class"
    assert Projector.__bases__ == (object,)


def test_the_wiring_module_never_imports_app() -> None:
    """The contract read off the module's own imports, by AST.

    Deliberately not a substring search for ``Projector``: this module's comments
    have to SAY "typed on the Protocol, never as ``Projector``" to explain why the
    field is annotated the way it is, and a text search would forbid the
    explanation along with the import. What matters is the dependency arrow, so
    that is what is read — the same thing ``test_adapters_never_import_app``
    asserts across the whole package, narrowed to the module the fold added.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(wiring))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "cli_agent_orchestrator" and len(parts) > 1:
                roots.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "cli_agent_orchestrator" and len(parts) > 1:
                    roots.add(parts[1])

    assert "app" not in roots
    assert roots <= {"core"}


# -- what the decision_row branch actually guards ----------------------------


@pytest.mark.parametrize(
    "decision",
    [DecisionKind.DELIVERY_ATTEMPT, DecisionKind.TEARDOWN_DECIDED, DecisionKind.FLEET_OVERRIDE],
)
def test_producer_delivered_decision_rows_reach_the_fold(
    store: FakeEventStore, decision: DecisionKind
) -> None:
    """The third mutant A1 names, and the case r5 got wrong in the other direction.

    A ``status.transition`` the fold produces cannot re-enter the fold, because
    the projector writes it through the event-store port directly rather than
    through ``emit``. So the ``decision_row`` branch is not what bounds recursion.
    What it does guard is real and reaches here every day: ``server_decisions``
    emits ``delivery.attempt`` and ``teardown.decided`` through ``emit``, and
    ``legacy_egress`` emits ``fleet.override`` the same way. Remove the branch and
    those move the projection.

    This test asserts they ARRIVE; that they move nothing is the projector's own
    contract, asserted in ``test/app/worker_truth/test_projector.py``.
    """
    folder = RecordingFolder()
    _install(store, folder)

    wiring.emit(
        _draft(
            decision,
            producer=Producer.SERVER,
            confidence=Confidence.DERIVED,
            decision=decision,
            evidence="e0",
        )
    )

    assert len(folder.folded) == 1
    assert folder.folded[0].decision is decision
