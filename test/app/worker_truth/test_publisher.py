"""The projection's publisher (WP-ARCH phase 2, D1 — slice 3).

The publisher is four lines of logic and two decisions, and the tests are about
the decisions: WHEN it publishes (the same predicate that suppresses the pane,
never a second one) and WHAT it publishes (the forward map, with the causing
event named).
"""

from __future__ import annotations

from datetime import UTC, datetime
from test.app.conftest import Rig

from cli_agent_orchestrator.app.worker_truth.publisher import StatusPublisher
from cli_agent_orchestrator.core.events import EventKind
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S

TERMINAL = "term-pub"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class _Egress:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, str | None, str]] = []

    def publish(self, terminal_id, status, *, event_id, worker_state, since):
        self.published.append((terminal_id, status, event_id, worker_state))


class _View:
    def __init__(self, projected: bool) -> None:
        self.projected = projected

    def is_projected(self, terminal_id: str) -> bool:
        return self.projected


def _publisher(projected: bool = True) -> tuple[StatusPublisher, _Egress]:
    egress = _Egress()
    return StatusPublisher(egress, _View(projected)), egress


def test_it_publishes_the_forward_map_with_the_causing_event() -> None:
    publisher, egress = _publisher()

    published = publisher(
        TERMINAL,
        WorkerState.IDLE,
        causing_kind=EventKind.TURN_ENDED,
        degraded_reason=None,
        event_id="01CAUSE",
        since=NOW,
    )

    assert published is True
    assert egress.published == [(TERMINAL, "completed", "01CAUSE", "idle")]


def test_an_unprojected_terminal_is_never_published_for() -> None:
    """The same predicate the suppression uses, and that is the whole design.

    Publishing on a different question than the one that suppresses the pane
    would produce one of the two failure modes this phase exists to end: two
    writers, or none.
    """
    publisher, egress = _publisher(projected=False)

    published = publisher(
        TERMINAL,
        WorkerState.BUSY,
        causing_kind=EventKind.TURN_STARTED,
        degraded_reason=None,
        event_id="01CAUSE",
        since=NOW,
    )

    assert published is False
    assert egress.published == []


def test_an_egress_that_raises_never_breaks_the_fold() -> None:
    class _Hostile:
        def publish(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("monitor unavailable")

    publisher = StatusPublisher(_Hostile(), _View(True))

    assert (
        publisher(
            TERMINAL,
            WorkerState.BUSY,
            causing_kind=None,
            degraded_reason=None,
            event_id=None,
            since=NOW,
        )
        is False
    )


# ------------------------------------------------- driven by the real fold


def _wire(rig: Rig, projected: bool = True) -> _Egress:
    publisher, egress = _publisher(projected)
    rig.projector._publisher = publisher  # type: ignore[attr-defined]
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    return egress


def test_the_fold_publishes_one_status_per_applied_transition(rig: Rig) -> None:
    egress = _wire(rig)

    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.emit(TERMINAL, EventKind.TOOL_CALLED)  # the diagonal: same state
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    assert [row[1] for row in egress.published] == ["processing", "completed"]


def test_a_muted_event_publishes_nothing(rig: Rig) -> None:
    """Source precedence decided the pane's reading does not apply; publishing it
    would apply it through the back door."""
    egress = _wire(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    egress.published.clear()

    rig.legacy(TERMINAL, "processing")

    assert egress.published == []


def test_every_publish_names_a_real_transition_row(rig: Rig) -> None:
    """AC-2b case 8: the id has to RESOLVE, or it is not a chain."""
    egress = _wire(rig)

    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    event_id = egress.published[0][2]
    assert event_id is not None
    assert rig.events.get(event_id) is not None


def test_the_no_signal_sweep_hands_back_rather_than_publishing(rig: Rig) -> None:
    """AC-2b case 7.  A source that died is exactly when the pane must resume.

    The sweep's own mark has already lowered ``is_projected`` by the time the
    publisher is offered the degradation, so this is one predicate refusing a
    publish rather than two rules agreeing — which is why the pane path is
    already publishing for the terminal again.
    """
    real_view = rig.health
    publisher = StatusPublisher(_Egress(), real_view)
    rig.projector._publisher = publisher  # type: ignore[attr-defined]
    egress = publisher._egress  # type: ignore[attr-defined]
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    assert egress.published  # it WAS publishing while the source was alive
    egress.published.clear()

    rig.clock.advance(NO_SIGNAL_S + 1)
    rig.projector.sweep()

    assert rig.state_of(TERMINAL) is WorkerState.DEGRADED
    assert rig.states.get(TERMINAL).degraded_reason is DegradedReason.NO_SIGNAL
    assert egress.published == []
    assert rig.health.is_projected(TERMINAL) is False
