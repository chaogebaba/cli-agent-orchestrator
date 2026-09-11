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


# ---------------------------------------------------------------------------
# WP-ARCH sub-phase 2b — the two DERIVED producers that moved to this site.
# ---------------------------------------------------------------------------


class _Monitor:
    """Stands in for the status monitor: one pure, re-entrant read."""

    def __init__(self, condition: str | None = None) -> None:
        self.condition = condition

    def get_condition(self, terminal_id: str) -> str | None:
        return self.condition


def _classify_with(monitor: object, status: str = "idle") -> None:
    pane_classification.record_pane_classification(
        TERMINAL, status, None, "incremental", "accepted", None, monitor=monitor
    )


def test_a_cap_appends_usage_capped_at_the_classification_site(
    ingest_on: FakeEventStore,
) -> None:
    """D1c's other half.  The rollout can never say this, and after D1 the egress
    will not be reached for the terminals that have one."""
    monitor = _Monitor(condition=legacy_egress.CAPPED_CONDITION_LABEL)

    _classify_with(monitor)
    _classify_with(monitor)  # same pair AND same condition: one row, not two

    rows = ingest_on.of_kind(EventKind.USAGE_CAPPED, TERMINAL)
    assert len(rows) == 1
    assert rows[0].producer is Producer.PANE
    assert rows[0].confidence is Confidence.DERIVED
    assert rows[0].payload["condition"] == legacy_egress.CAPPED_CONDITION_LABEL


def test_the_cap_edge_fires_while_the_classification_sits_still(
    ingest_on: FakeEventStore,
) -> None:
    """The reason the condition is tracked apart from the pair.

    A cap is detected by the condition classifier, not by the screen reader, so
    it routinely arrives with the latched status and origin unchanged.  Folding
    it into the pair would make this case produce nothing at all — and the
    capped-lane policy is the consumer that would silently lose its evidence.
    """
    monitor = _Monitor()
    _classify_with(monitor)
    assert ingest_on.of_kind(EventKind.USAGE_CAPPED, TERMINAL) == []

    monitor.condition = legacy_egress.CAPPED_CONDITION_LABEL
    _classify_with(monitor)

    assert len(ingest_on.of_kind(EventKind.USAGE_CAPPED, TERMINAL)) == 1
    # ...and the unchanged pair still produced no second classification row.
    assert len(ingest_on.of_kind(EventKind.STATUS_PANE_CLASSIFIED, TERMINAL)) == 1


def test_a_monitor_that_raises_never_breaks_the_classification_row(
    ingest_on: FakeEventStore,
) -> None:
    class _Hostile:
        def get_condition(self, terminal_id: str) -> str:
            raise RuntimeError("boom")

    _classify_with(_Hostile())

    assert len(ingest_on.of_kind(EventKind.STATUS_PANE_CLASSIFIED, TERMINAL)) == 1
    assert ingest_on.of_kind(EventKind.USAGE_CAPPED, TERMINAL) == []


def test_no_monitor_at_all_is_legal(ingest_on: FakeEventStore) -> None:
    """The pre-2b call shape stays valid; there is simply no condition to read."""
    _classify("idle")

    assert len(ingest_on.of_kind(EventKind.STATUS_PANE_CLASSIFIED, TERMINAL)) == 1
    assert ingest_on.of_kind(EventKind.USAGE_CAPPED, TERMINAL) == []


def test_a_dialog_edge_produces_prompt_awaiting(ingest_on: FakeEventStore) -> None:
    """D1f.  Codex has no dialog hook at all, so with the egress suppressed its
    AWAITING_INPUT would have no producer and #386's card would project busy."""
    _classify("processing")
    _classify(pane_classification.AWAITING_STATUS)

    rows = ingest_on.of_kind(EventKind.PROMPT_AWAITING, TERMINAL)
    assert len(rows) == 1
    assert rows[0].producer is Producer.PANE
    assert rows[0].confidence is Confidence.DERIVED
    assert rows[0].payload["prior_status"] == "processing"


def test_a_repainting_card_is_one_awaiting_row(ingest_on: FakeEventStore) -> None:
    """The #386 shape: the pane repaints continuously while the card is up."""
    for _ in range(20):
        _classify(pane_classification.AWAITING_STATUS)

    assert len(ingest_on.of_kind(EventKind.PROMPT_AWAITING, TERMINAL)) == 1


def test_leaving_the_card_produces_prompt_answered(ingest_on: FakeEventStore) -> None:
    _classify(pane_classification.AWAITING_STATUS)
    _classify("processing")

    assert len(ingest_on.of_kind(EventKind.PROMPT_ANSWERED, TERMINAL)) == 1
    assert len(ingest_on.of_kind(EventKind.PROMPT_AWAITING, TERMINAL)) == 1


def test_a_terminal_first_seen_already_waiting_still_reports_the_card(
    ingest_on: FakeEventStore,
) -> None:
    """A server restart is exactly when a worker has been sitting on a card."""
    _classify(pane_classification.AWAITING_STATUS)

    assert len(ingest_on.of_kind(EventKind.PROMPT_AWAITING, TERMINAL)) == 1


def test_the_dialog_row_and_the_classification_row_share_one_source_ref(
    ingest_on: FakeEventStore,
) -> None:
    """Two readings of ONE pane observation.

    Without the join, "what did the screen say when the card appeared" is
    unanswerable from the log, which is the question ``cao diag`` exists for.
    """
    _classify(pane_classification.AWAITING_STATUS)

    awaiting = ingest_on.of_kind(EventKind.PROMPT_AWAITING, TERMINAL)[0]
    classified = ingest_on.of_kind(EventKind.STATUS_PANE_CLASSIFIED, TERMINAL)[0]
    assert awaiting.source_ref == classified.source_ref


def test_an_origin_only_edge_is_not_a_dialog_edge(ingest_on: FakeEventStore) -> None:
    """The pair changed, the status did not — there is no card news here."""
    pane_classification.record_pane_classification(
        TERMINAL, pane_classification.AWAITING_STATUS, "incremental", "incremental", "accepted"
    )
    pane_classification.record_pane_classification(
        TERMINAL, pane_classification.AWAITING_STATUS, "probe", "incremental", "accepted"
    )

    assert len(ingest_on.of_kind(EventKind.STATUS_PANE_CLASSIFIED, TERMINAL)) == 2
    assert len(ingest_on.of_kind(EventKind.PROMPT_AWAITING, TERMINAL)) == 1


def test_a_status_that_could_not_be_read_is_recorded_as_unknown(
    ingest_on: FakeEventStore,
) -> None:
    """The ``''`` artefact, fixed.

    ``legacy_state('')`` is ``None`` and ``DIAG-PANE-DISAGREE`` SKIPS a row it
    cannot read — so an empty latched status did not produce a disagreement, it
    produced silence, and a box round counting disagreements by raw string
    comparison read those rows as real.  ``unknown`` is in the legacy vocabulary
    and maps to ``degraded``, so the check can compare it and complain.
    """
    pane_classification.record_pane_classification(TERMINAL, None, None, "incremental", "accepted")

    row = ingest_on.of_kind(EventKind.STATUS_PANE_CLASSIFIED, TERMINAL)[0]
    assert row.payload["latched_status"] == pane_classification.UNCLASSIFIED_STATUS


def test_the_awaiting_spelling_matches_the_real_legacy_enum() -> None:
    """The pin that keeps a hand-spelled legacy constant honest.

    ``adapters`` may not import ``models``; this test lives on the legacy side of
    that fence, where it can.
    """
    from cli_agent_orchestrator.models.terminal import TerminalStatus

    assert pane_classification.AWAITING_STATUS == TerminalStatus.WAITING_USER_ANSWER.value
    assert pane_classification.UNCLASSIFIED_STATUS == TerminalStatus.UNKNOWN.value
