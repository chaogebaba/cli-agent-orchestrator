"""End-to-end subscription-gap projection (WP-HERDR H1 r2, B2).

The codex EMPIRICAL-GATE probes from
``/data/cao-scratch/adj-herdr-h1/test_gap_projection.py``, shipped into the
suite.  Unlike the shipped r1 adapter test (which only asserted the raw draft's
``payload["reason"] == "no_signal"``), these fold the gap event through the REAL
producer, store and projector together, which is what r1's B2 required and what
the r1 code failed:

* ``degraded_reason=no_signal`` must reach the projector so the terminal degrades
  to ``NO_SIGNAL``, not the ``PANE_MISSING`` kind-default ``PANE_UNREADABLE``;
* the gap must NOT be muted while the authoritative source's health timestamp is
  still fresh (a herdr pane keeps pane-probing, so that timestamp stays fresh).

The runtime source now emits the gap event at ``authoritative`` confidence with
``degraded_reason=no_signal`` in the payload, which satisfies both.
"""

from __future__ import annotations

from cli_agent_orchestrator.adapters.truth import herdr_runtime, wiring
from cli_agent_orchestrator.adapters.truth.herdr_runtime import HerdrRuntimeSource
from cli_agent_orchestrator.app.worker_truth.projector import Projector, StaticSourceRegistry
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState
from test.app.fakes import FakeClock, InMemoryEventStore, InMemoryStateStore


TERMINAL = "cao-terminal"


def _pane(status: str) -> dict[str, object]:
    return {
        "pane_id": "w1:p1",
        "terminal_id": TERMINAL,
        "agent": "pi",
        "agent_session": {
            "agent": "pi",
            "kind": "path",
            "source": "herdr:pi",
            "value": "/sessions/stable.jsonl",
        },
        "agent_status": status,
        "screen_detection_skipped": True,
    }


def _wired(*, source_healthy: bool) -> tuple[HerdrRuntimeSource, InMemoryStateStore]:
    clock = FakeClock()
    events = InMemoryEventStore(clock)
    states = InMemoryStateStore()
    sources = StaticSourceRegistry(frozenset({TERMINAL}))
    projector = Projector(events, states, clock, sources)
    wiring.install_producers(
        wiring.ProducerRuntime(store=events, clock=clock, state_store=states, folder=projector)
    )
    source = HerdrRuntimeSource(TERMINAL, socket_path="/unused")
    source._process_pane(_pane("working"))
    if source_healthy:
        states.touch_source_probe(TERMINAL, probed_at=clock.now())
    return source, states


def teardown_function() -> None:
    wiring.reset_producers()


def test_gap_projects_exactly_no_signal_when_no_health_touch_exists() -> None:
    source, states = _wired(source_healthy=False)
    source._emit_gap_degraded()
    row = states.get(TERMINAL)
    assert row is not None
    assert row.state is WorkerState.DEGRADED
    assert row.degraded_reason is DegradedReason.NO_SIGNAL


def test_gap_is_not_muted_while_authoritative_source_is_still_marked_healthy() -> None:
    source, states = _wired(source_healthy=True)
    source._emit_gap_degraded()
    row = states.get(TERMINAL)
    assert row is not None
    assert row.state is WorkerState.DEGRADED
    assert row.degraded_reason is DegradedReason.NO_SIGNAL
