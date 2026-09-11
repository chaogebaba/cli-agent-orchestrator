"""WP-HERDR §6(ii) — the certified cohort never silently reverts to the pane.

Source-level precedence alone says: mute derived events while the authoritative
source is HEALTHY, apply them while it is not, because the pane is a first-class
fallback.  That is right for the codex tailer and wrong for a certified herdr
cohort, and the difference is the whole of §6(ii).

For a certified terminal there IS no second lifecycle source.  A stale or
detached herdr source does not mean "fall back to the screen", it means "we have
no idea what this worker is doing" — and the honest projection of that is
``degraded(no_signal)``, which makes the terminal delivery-ineligible and
visible.  Reverting to scraped lifecycle would instead produce a plausible,
confident, unsourced answer, which is the failure mode the whole package exists
to end.

What must NOT change is AC6's carve-out: the kinds an authoritative source
cannot know keep applying from the pane on a certified terminal, because nothing
else produces them.
"""

from __future__ import annotations

from test.app.fakes import FakeClock, InMemoryEventStore, InMemoryStateStore

from cli_agent_orchestrator.app.worker_truth.projector import (
    DERIVED_ALWAYS_KINDS,
    Projector,
    StaticSourceRegistry,
)
from cli_agent_orchestrator.core.events import Confidence, EventDraft, EventKind, Producer
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S

CERTIFIED = "cao-certified"
PLAIN = "cao-plain"


def _wired() -> tuple[Projector, InMemoryEventStore, InMemoryStateStore, FakeClock]:
    clock = FakeClock()
    events = InMemoryEventStore(clock)
    states = InMemoryStateStore()
    sources = StaticSourceRegistry(frozenset({CERTIFIED, PLAIN}))
    sources.set_fallback_disabled(CERTIFIED)
    return Projector(events, states, clock, sources), events, states, clock


def _derived(terminal: str, kind: EventKind, clock: FakeClock) -> EventDraft:
    return EventDraft(
        terminal_id=terminal,
        kind=kind,
        producer=Producer.PANE,
        confidence=Confidence.DERIVED,
        observed_at=clock.now(),
        payload={},
    )


def _project(projector: Projector, events: InMemoryEventStore, draft: EventDraft):
    return projector.project(events.append(draft))


def _go_stale(states: InMemoryStateStore, terminal: str, clock: FakeClock) -> None:
    """A source that streamed and then went quiet past ``NO_SIGNAL_S``."""
    states.touch_source_probe(terminal, probed_at=clock.now())
    clock.advance(NO_SIGNAL_S * 2)


def test_derived_lifecycle_is_muted_on_a_certified_terminal_with_a_stale_source() -> None:
    projector, events, states, clock = _wired()
    _go_stale(states, CERTIFIED, clock)
    outcome = _project(projector, events, _derived(CERTIFIED, EventKind.TURN_STARTED, clock))
    assert outcome.applied is False
    row = states.get(CERTIFIED)
    assert row is None or row.state is not WorkerState.BUSY


def test_the_same_event_APPLIES_on_an_uncertified_terminal_with_a_stale_source() -> None:
    """The control, and the reason the mute is a real branch: identical event,
    identical source health, differing only in certification."""
    projector, events, states, clock = _wired()
    _go_stale(states, PLAIN, clock)
    outcome = _project(projector, events, _derived(PLAIN, EventKind.TURN_STARTED, clock))
    assert outcome.applied is True
    row = states.get(PLAIN)
    assert row is not None and row.state is WorkerState.BUSY


def test_derived_lifecycle_is_muted_even_with_no_source_probe_at_all() -> None:
    """A DETACHED source — the column was never written — is the harder case:
    ``_source_healthy`` reads NULL as unhealthy, so precedence alone would apply
    the pane event.  §6(ii) must still mute it."""
    projector, events, states, clock = _wired()
    outcome = _project(projector, events, _derived(CERTIFIED, EventKind.TURN_STARTED, clock))
    assert outcome.applied is False


def test_the_kinds_an_authoritative_source_cannot_know_still_apply() -> None:
    """AC6's carve-out survives certification, because nothing else produces
    these: a capped worker and an exited process must still be heard from the
    pane on a certified terminal."""
    projector, events, states, clock = _wired()
    _go_stale(states, CERTIFIED, clock)
    for kind in (EventKind.USAGE_CAPPED, EventKind.PROCESS_EXITED):
        assert kind in DERIVED_ALWAYS_KINDS
        outcome = _project(projector, events, _derived(CERTIFIED, kind, clock))
        assert outcome.applied is True, kind
    row = states.get(CERTIFIED)
    assert row is not None and row.state is WorkerState.EXITED


def test_an_authoritative_gap_event_still_degrades_a_certified_terminal() -> None:
    """The mute is about DERIVED events only; the source's own authoritative
    declaration of a gap is exactly what must get through (§6(i))."""
    projector, events, states, clock = _wired()
    draft = EventDraft(
        terminal_id=CERTIFIED,
        kind=EventKind.PANE_MISSING,
        producer=Producer.SERVER,
        confidence=Confidence.AUTHORITATIVE,
        observed_at=clock.now(),
        payload={"degraded_reason": DegradedReason.NO_SIGNAL.value},
    )
    _project(projector, events, draft)
    row = states.get(CERTIFIED)
    assert row is not None
    assert row.state is WorkerState.DEGRADED
    assert row.degraded_reason is DegradedReason.NO_SIGNAL


def test_clearing_the_flag_restores_the_pane_fallback() -> None:
    """Teardown, and only teardown, lets the terminal go back to the pane."""
    projector, events, states, clock = _wired()
    registry = projector._sources
    assert isinstance(registry, StaticSourceRegistry)
    registry.clear_fallback_disabled(CERTIFIED)
    _go_stale(states, CERTIFIED, clock)
    outcome = _project(projector, events, _derived(CERTIFIED, EventKind.TURN_STARTED, clock))
    assert outcome.applied is True
