"""The classification-site producer (WP-ARCH phase 2, D1c).

The decision this file defends is *where* the row is written, not *that* it is.
Phase 1 hooked the status path at the egress, which was right for phase 1; phase
2's D1 suppresses that publish for a source-healthy terminal, and one side of
D5's comparison would vanish for exactly the terminals the comparison is about.
So the reading is recorded at the classification site, where the classifier keeps
running whatever the publish does.
"""

from __future__ import annotations

from cli_agent_orchestrator.adapters.truth import legacy_egress, pane_classification, wiring
from cli_agent_orchestrator.core.events import Confidence, EventKind, Producer

from .conftest import FakeClock, FakeEventStore

TERMINAL = "t-1"


def _classify(status: str = "idle", *, outcome: str = "accepted", raw: object = None) -> None:
    pane_classification.record_pane_classification(
        TERMINAL, status, None, "incremental", outcome, raw
    )


def test_one_row_per_classification_edge_not_per_output_chunk(
    ingest_on: FakeEventStore,
) -> None:
    """Edge-triggered on the same ``(latched_status, origin)`` pair the legacy
    producer uses.

    The pane path fires per output chunk.  A row per chunk would dominate the log
    and put phase 2's write rate — the number §9 promises to measure — an order of
    magnitude above phase 1's.
    """
    for _ in range(50):
        _classify("idle")

    assert len(ingest_on.of_kind(EventKind.STATUS_PANE_CLASSIFIED, TERMINAL)) == 1


def test_a_changed_status_is_a_new_edge(ingest_on: FakeEventStore) -> None:
    _classify("idle")
    _classify("processing")
    _classify("processing")
    _classify("idle")

    rows = ingest_on.of_kind(EventKind.STATUS_PANE_CLASSIFIED, TERMINAL)
    assert [row.payload["latched_status"] for row in rows] == ["idle", "processing", "idle"]


def test_the_origin_defaulting_matches_the_legacy_producer_exactly(
    ingest_on: FakeEventStore,
) -> None:
    """Both producers must compute the pair with the SAME expression.

    A second copy would drift, and a drifted pair means one producer emits an edge
    the other does not — which D5's comparison would then report as a
    disagreement between the projection and the pane, when what actually
    disagreed was two renderings of one string.
    """
    assert pane_classification.record_pane_classification.__module__.endswith("pane_classification")
    _classify("idle", outcome="forced")
    assert ingest_on.rows[0].payload["origin"] == legacy_egress.effective_origin(None, "forced")


def test_the_payload_carries_the_three_fields_d1c_names(ingest_on: FakeEventStore) -> None:
    """``raw_classification``, the would-be ``latched_status`` and ``pass_outcome``.

    Both status fields, because the disagreement worth recording can live in
    either: a classifier that read the screen wrongly, or a latch that held a
    correct reading back.  Recording only the latched value would make the second
    kind invisible, which is #439's shape.
    """
    _classify("idle", outcome="accepted", raw="COMPLETED")
    payload = ingest_on.rows[0].payload

    assert payload["latched_status"] == "idle"
    assert payload["raw_classification"] == "COMPLETED"
    assert payload["pass_outcome"] == "accepted"
    assert payload["frame_source"] == "incremental"


def test_the_row_is_derived_and_produced_by_the_pane(ingest_on: FakeEventStore) -> None:
    """A reading is not a fact.

    ``derived`` is what lets the projector's source-level precedence mute it for a
    terminal whose authoritative source is healthy — which is the whole point of
    recording it separately from believing it.
    """
    _classify()
    row = ingest_on.rows[0]

    assert row.producer is Producer.PANE
    assert row.confidence is Confidence.DERIVED


def test_the_source_ref_carries_the_pane_scheme_and_a_distinct_seq(
    ingest_on: FakeEventStore,
) -> None:
    """§5: ``pane:<terminal_id>#<seq>``, and the seq must actually discriminate.

    The mutant this kills is a constant or a repeated discriminator: two edges
    sharing a ref would be a provenance field that cannot tell two observations
    apart, which is worse than a null because a reader believes it.
    """
    _classify("idle")
    _classify("processing")

    refs = [row.source_ref for row in ingest_on.of_kind(EventKind.STATUS_PANE_CLASSIFIED)]
    assert refs == ["pane:t-1#1", "pane:t-1#2"]


def test_forget_drops_one_terminals_edge_state(ingest_on: FakeEventStore) -> None:
    _classify("idle")
    pane_classification.forget(TERMINAL)
    _classify("idle")

    assert len(ingest_on.of_kind(EventKind.STATUS_PANE_CLASSIFIED, TERMINAL)) == 2


def test_with_ingestion_off_nothing_is_written(store: FakeEventStore, clock: FakeClock) -> None:
    """AC-2a's off arm, for this producer."""
    wiring.reset_producers()
    _classify("idle")

    assert store.rows == []


def test_a_failing_store_never_raises_into_the_locked_detection_path(
    ingest_on: FakeEventStore,
) -> None:
    """This runs inside ``_apply_detection``'s ``finally``, under the monitor's lock.

    A diagnostic that could raise there would turn a diagnosability feature into a
    status outage — the failure mode the whole work package exists to end.
    """
    ingest_on.fail_next = True
    _classify("idle")

    assert ingest_on.rows == []
