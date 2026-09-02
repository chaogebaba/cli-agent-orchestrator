"""AC8 — typed findings, counted rather than repeated.

The three phase-1 checks, plus the two properties that make findings usable at
all: a repeat increments one row instead of writing a second, and the FIRST
sample is the one kept.
"""

from __future__ import annotations

from test.app.conftest import Rig

from cli_agent_orchestrator.app.worker_truth.checks import (
    EVIDENCE_REQUIRING_DECISIONS,
    ghost_transition_check,
    record_migration_failure,
)
from cli_agent_orchestrator.core.events import (
    Confidence,
    DecisionKind,
    EventDraft,
    EventKind,
    Producer,
)
from cli_agent_orchestrator.core.findings import FindingCode, FindingState
from cli_agent_orchestrator.core.states import WorkerState
from cli_agent_orchestrator.core.timing import PANE_HEARTBEAT_S

TERMINAL = "term-c1"


def _ghost_draft(rig: Rig, decision: DecisionKind, evidence: str | None) -> None:
    rig.events.append(
        EventDraft(
            terminal_id=TERMINAL,
            kind=decision,
            producer=Producer.SERVER,
            confidence=Confidence.AUTHORITATIVE,
            observed_at=rig.clock.now(),
            payload={"from": "idle", "to": "busy", "rule": "handmade"},
            decision=decision,
            evidence=evidence,
        )
    )


# ------------------------------------------------------- DIAG-GHOST-TRANSITION


def test_a_transition_with_no_evidence_is_a_ghost(rig: Rig) -> None:
    """The mutant: ``evidence`` dropped from the decision row."""
    _ghost_draft(rig, DecisionKind.STATUS_TRANSITION, None)

    findings = rig.findings.list_findings(code=FindingCode.DIAG_GHOST_TRANSITION)
    assert len(findings) == 1
    assert findings[0].terminal_id == TERMINAL


def test_an_empty_string_is_as_much_a_ghost_as_a_null(rig: Rig) -> None:
    _ghost_draft(rig, DecisionKind.STATUS_TRANSITION, "")

    assert len(rig.findings.list_findings(code=FindingCode.DIAG_GHOST_TRANSITION)) == 1


def test_the_projector_never_produces_a_ghost(rig: Rig) -> None:
    """The check must be silent on correct behaviour, or nobody will read it."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    rig.pane(TERMINAL, EventKind.PANE_MISSING)
    rig.pane(TERMINAL, EventKind.PANE_RECOVERED)

    assert rig.findings.list_findings(code=FindingCode.DIAG_GHOST_TRANSITION) == []


def test_decisions_with_nothing_to_cite_are_exempt(rig: Rig) -> None:
    """``probe.failed`` touches no terminal and ``teardown.intended`` precedes
    any observation.  Demanding evidence from them would fire on every boot."""
    assert DecisionKind.PROBE_FAILED not in EVIDENCE_REQUIRING_DECISIONS
    assert DecisionKind.TEARDOWN_INTENDED not in EVIDENCE_REQUIRING_DECISIONS

    rig.events.append(
        EventDraft(
            terminal_id=TERMINAL,
            kind=DecisionKind.PROBE_FAILED,
            producer=Producer.SERVER,
            confidence=Confidence.AUTHORITATIVE,
            observed_at=rig.clock.now(),
            payload={"exit_code": 1},
            decision=DecisionKind.PROBE_FAILED,
        )
    )

    assert rig.findings.list_findings(code=FindingCode.DIAG_GHOST_TRANSITION) == []


def test_worker_truth_rows_are_never_ghosts(rig: Rig) -> None:
    """``EventDraft`` forbids evidence on worker truth, so the check can only
    ever fire on a decision.  Asserted so the exemption stays deliberate."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    assert rig.findings.list_findings(code=FindingCode.DIAG_GHOST_TRANSITION) == []


# --------------------------------------------------------- DIAG-BAD-TRANSITION


def test_the_check_reclassifies_rather_than_trusting_the_row(rig: Rig) -> None:
    """A projector that stamped the wrong classification is caught, not believed."""
    rig.events.append(
        EventDraft(
            terminal_id=TERMINAL,
            kind=DecisionKind.STATUS_TRANSITION,
            producer=Producer.SERVER,
            confidence=Confidence.AUTHORITATIVE,
            observed_at=rig.clock.now(),
            payload={"from": "exited", "to": "busy", "classification": "Allowed"},
            decision=DecisionKind.STATUS_TRANSITION,
            evidence="EVIDENCE",
        )
    )

    findings = rig.findings.list_findings(code=FindingCode.DIAG_BAD_TRANSITION)
    assert len(findings) == 1
    assert findings[0].dedupe_key == "exited->busy"


def test_a_recovery_into_an_anomalous_cell_is_also_flagged(rig: Rig) -> None:
    """``status.recovered`` carries a from/to pair too, so it is classified.

    A terminal that degraded out of ``starting`` recovers back into it, and
    ``degraded -> starting`` is the cell the table reserves for spotting a
    mis-attributed launch.  Checking only ``status.transition`` would lose it.
    """
    rig.pane(TERMINAL, EventKind.PANE_MISSING)
    assert rig.states.get(TERMINAL).prior_state is WorkerState.STARTING

    rig.pane(TERMINAL, EventKind.PANE_RECOVERED)

    assert rig.state_of(TERMINAL) is WorkerState.STARTING
    findings = rig.findings.list_findings(code=FindingCode.DIAG_BAD_TRANSITION)
    assert [f.dedupe_key for f in findings] == ["degraded->starting"]


def test_an_unparseable_cell_is_ignored_rather_than_crashing(rig: Rig) -> None:
    rig.events.append(
        EventDraft(
            terminal_id=TERMINAL,
            kind=DecisionKind.STATUS_TRANSITION,
            producer=Producer.SERVER,
            confidence=Confidence.AUTHORITATIVE,
            observed_at=rig.clock.now(),
            payload={"from": "nonsense", "to": 17},
            decision=DecisionKind.STATUS_TRANSITION,
            evidence="EVIDENCE",
        )
    )

    assert rig.findings.list_findings(code=FindingCode.DIAG_BAD_TRANSITION) == []


# -------------------------------------------------------- DIAG-LEGACY-DISAGREE


def test_a_disagreement_younger_than_one_heartbeat_is_lag_not_a_finding(
    rig: Rig,
) -> None:
    """The two sides are fed by producers on different clocks.

    Firing on ordinary lag would bury the disagreements the agreement report is
    trying to count.
    """
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "idle")

    assert rig.findings.list_findings(code=FindingCode.DIAG_LEGACY_DISAGREE) == []


def test_a_disagreement_older_than_one_heartbeat_fires(rig: Rig) -> None:
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "idle")

    rig.clock.advance(PANE_HEARTBEAT_S + 1)
    assert rig.checks(TERMINAL) is True

    findings = rig.findings.list_findings(code=FindingCode.DIAG_LEGACY_DISAGREE)
    assert len(findings) == 1
    assert findings[0].dedupe_key == "busy|idle"


def test_agreement_produces_no_finding_however_long_it_lasts(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "processing")

    rig.clock.advance(PANE_HEARTBEAT_S * 100)

    assert rig.checks(TERMINAL) is False


def test_the_sweep_measures_a_terminal_that_stopped_producing_events(rig: Rig) -> None:
    """The durational check cannot ride on append alone: a stalled worker is
    exactly the case where no further row arrives to trigger it."""
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "idle")

    rig.clock.advance(PANE_HEARTBEAT_S + 1)
    rig.states.touch_probe(
        TERMINAL, probed_at=rig.clock.now(), pane_present=True, pane_pid=9, miss_count=0
    )
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.projector.sweep()

    assert len(rig.findings.list_findings(code=FindingCode.DIAG_LEGACY_DISAGREE)) == 1


def test_an_unknown_legacy_status_is_no_opinion_not_a_disagreement(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "a_status_this_map_has_never_seen")

    rig.clock.advance(PANE_HEARTBEAT_S * 5)

    assert rig.checks(TERMINAL) is False


# ------------------------------------------------------- dedup and containment


def test_repeats_increment_and_keep_the_first_sample(rig: Rig) -> None:
    """A projector that saw the same impossible cell four hundred times must
    write one row whose count is four hundred, keeping the earliest sample —
    the first occurrence is the one whose timeline still explains anything."""
    first = None
    for index in range(4):
        rig.pane(TERMINAL, EventKind.USAGE_CAPPED)
        rig.emit(TERMINAL, EventKind.TURN_STARTED)
        if first is None:
            first = rig.findings.list_findings(code=FindingCode.DIAG_BAD_TRANSITION)[0]

    findings = rig.findings.list_findings(code=FindingCode.DIAG_BAD_TRANSITION)
    assert len(findings) == 1
    assert findings[0].count == 4
    assert findings[0].sample_event_id == first.sample_event_id
    assert findings[0].first_seen_at == first.first_seen_at


def test_a_check_that_raises_never_breaks_the_append(rig: Rig) -> None:
    """``CheckRunner`` says implementations must not raise, and the store calls
    this from the happy path of every producer in the system."""

    def explode(_: object) -> None:
        raise RuntimeError("check exploded")

    rig.registry.register(FindingCode.DIAG_GHOST_TRANSITION, explode)

    stored = rig.emit(TERMINAL, EventKind.TURN_STARTED)

    assert rig.events.get(stored.event_id) is not None
    assert rig.state_of(TERMINAL) is WorkerState.BUSY


def test_migration_failure_dedupes_on_the_step(rig: Rig) -> None:
    """A server rebooting ten times into the same broken migration leaves one
    row with a count of ten, not ten rows."""
    for _ in range(10):
        record_migration_failure(rig.findings, "worker_state_shadow", "no such column")

    findings = rig.findings.list_findings(code=FindingCode.DIAG_MIGRATION_FAILED)
    assert len(findings) == 1
    assert findings[0].count == 10
    assert findings[0].terminal_id == ""
    assert findings[0].state is FindingState.OPEN
