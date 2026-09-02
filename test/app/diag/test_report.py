"""AC7 — what an operator sees when a worker has stalled.

These tests assert on CONTENT, not on layout: the columns may be rearranged, but
a diag view that has stopped showing decision rows, evidence pointers, liveness
ages or the last legacy disagreement has stopped doing its job.  "``cao diag``
omits decision rows" is one of the phase-1 mutants, and it is killed here.
"""

from __future__ import annotations

import json
from test.app.conftest import Rig

from cli_agent_orchestrator.app.diag.report import (
    INGEST_OFF_NOTE,
    DiagSources,
    findings_payload,
    render_agreement,
    render_findings,
    render_timeline,
    render_why,
    timeline_payload,
    why_payload,
)
from cli_agent_orchestrator.app.worker_truth.agreement import build_agreement_report
from cli_agent_orchestrator.core.events import DecisionKind, EventKind
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S

TERMINAL = "term-d1"


def _sources(rig: Rig) -> DiagSources:
    return DiagSources(events=rig.events, states=rig.states, findings=rig.findings)


def _stalled(rig: Rig) -> DiagSources:
    """A worker that went busy, lost its pane, and fell silent.

    The scenario the command exists for, reused by most of the tests below: a
    codex terminal with an authoritative rollout, so the pane's contradicting
    "idle" is muted while the source is healthy, and the operator is left staring
    at a busy worker that then stops producing anything at all.
    """
    rig.sources.add(TERMINAL)
    rig.emit(TERMINAL, EventKind.SESSION_STARTED, source_ref="rollout:/tmp/r.jsonl#0")
    rig.clock.advance(2)
    rig.emit(TERMINAL, EventKind.SUBMISSION_CONFIRMED, msg_id="MSG42")
    rig.clock.advance(1)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.states.touch_probe(
        TERMINAL, probed_at=rig.clock.now(), pane_present=True, pane_pid=4242, miss_count=0
    )
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.clock.advance(30)
    rig.legacy(TERMINAL, "idle")
    rig.clock.advance(NO_SIGNAL_S + 10)
    rig.projector.sweep()
    return _sources(rig)


# ---------------------------------------------------------------------- header


def test_the_header_answers_the_four_stall_questions(rig: Rig) -> None:
    sources = _stalled(rig)

    text = render_timeline(sources, TERMINAL, now=rig.clock.now())

    assert "degraded(no_signal)" in text
    assert "pane_present=True" in text
    assert "pane_pid=4242" in text
    assert "last probe" in text
    assert "last source signal" in text
    assert "prior_state=busy" in text


def test_timestamps_are_rendered_as_ages(rig: Rig) -> None:
    """ "70s ago" is the number an operator reasons with."""
    sources = _stalled(rig)

    text = render_timeline(sources, TERMINAL, now=rig.clock.now())

    assert "ago" in text


def test_a_terminal_that_was_never_projected_says_so(rig: Rig) -> None:
    text = render_timeline(_sources(rig), "never-seen", now=rig.clock.now())

    assert "never been projected" in text


def test_the_ingest_switch_note_appears_only_when_it_is_off(rig: Rig) -> None:
    sources = _stalled(rig)

    off = render_timeline(sources, TERMINAL, now=rig.clock.now(), ingest_on=False)
    on = render_timeline(sources, TERMINAL, now=rig.clock.now(), ingest_on=True)

    assert INGEST_OFF_NOTE in off
    assert INGEST_OFF_NOTE not in on


# -------------------------------------------------------------------- timeline


def test_decision_rows_appear_in_the_timeline(rig: Rig) -> None:
    """The mutant: ``cao diag`` omits decision rows.

    They are in the same table with the same sequence precisely so one read
    reconstructs the story; a renderer that filtered them back out would undo the
    schema decision.
    """
    sources = _stalled(rig)

    text = render_timeline(sources, TERMINAL, now=rig.clock.now())

    assert DecisionKind.STATUS_TRANSITION.value in text
    assert "*" in text


def test_the_evidence_pointer_is_visible_on_decision_rows(rig: Rig) -> None:
    sources = _stalled(rig)
    transition = next(row for row in rig.events.read(TERMINAL) if row.decision is not None)

    text = render_timeline(sources, TERMINAL, now=rig.clock.now())

    assert f"<- {transition.evidence}" in text


def test_the_timeline_carries_a_gap_column(rig: Rig) -> None:
    """Finding where the log went quiet should be a scan, not arithmetic."""
    sources = _stalled(rig)

    text = render_timeline(sources, TERMINAL, now=rig.clock.now())

    assert "gap" in text
    assert "+ 30.00s" in text or "+30.00s" in text or "+  30.00s" in text


def test_correlation_ids_and_source_refs_are_shown(rig: Rig) -> None:
    sources = _stalled(rig)

    text = render_timeline(sources, TERMINAL, now=rig.clock.now())

    assert "msg=MSG42" in text
    assert "rollout:/tmp/r.jsonl#0" in text


def test_since_and_kind_filters_narrow_the_rows(rig: Rig) -> None:
    sources = _stalled(rig)
    all_rows = timeline_payload(sources, TERMINAL, now=rig.clock.now())

    filtered = timeline_payload(
        sources,
        TERMINAL,
        now=rig.clock.now(),
        kinds=frozenset({EventKind.TURN_STARTED}),
    )

    assert filtered["shown"] == 1
    assert filtered["total"] == all_rows["total"]
    assert filtered["events"][0]["kind"] == EventKind.TURN_STARTED.value


def test_the_footer_names_the_last_legacy_disagreement(rig: Rig) -> None:
    sources = _stalled(rig)

    text = render_timeline(sources, TERMINAL, now=rig.clock.now())
    data = timeline_payload(sources, TERMINAL, now=rig.clock.now())

    assert data["last_legacy_disagreement"] is not None
    assert data["last_legacy_disagreement"]["legacy_raw"] == "idle"
    assert "last legacy disagreement" in text
    assert data["last_legacy_disagreement"]["event_id"] in text


def test_no_disagreement_says_none_rather_than_nothing(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "processing")

    text = render_timeline(_sources(rig), TERMINAL, now=rig.clock.now())

    assert "last legacy disagreement: none" in text


def test_the_json_view_is_serialisable_and_matches_the_text_view(rig: Rig) -> None:
    """One builder feeds both, so they cannot drift."""
    sources = _stalled(rig)
    now = rig.clock.now()

    data = timeline_payload(sources, TERMINAL, now=now)
    text = render_timeline(sources, TERMINAL, now=now)

    json.dumps(data)
    assert data["header"]["state"] in text
    assert f"{data['shown']} of {data['total']} rows shown" in text


# ------------------------------------------------------------------ why chains


def test_the_why_chain_walks_from_a_decision_to_its_cause(rig: Rig) -> None:
    cause = rig.emit(TERMINAL, EventKind.TURN_STARTED)
    transition = next(row for row in rig.events.read(TERMINAL) if row.decision is not None)

    data = why_payload(_sources(rig), transition.event_id)

    assert [link["event_id"] for link in data["chain"]] == [
        transition.event_id,
        cause.event_id,
    ]
    assert data["chain"][0]["decision"] == DecisionKind.STATUS_TRANSITION.value


def test_the_why_chain_renders_the_causing_kind(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    transition = next(row for row in rig.events.read(TERMINAL) if row.decision is not None)

    text = render_why(_sources(rig), transition.event_id)

    assert DecisionKind.STATUS_TRANSITION.value in text
    assert EventKind.TURN_STARTED.value in text


def test_a_missing_event_is_reported_not_raised(rig: Rig) -> None:
    data = why_payload(_sources(rig), "NOSUCHEVENT")

    assert data["chain"][0]["error"] == "not found"


def test_a_root_event_says_the_chain_begins_here(rig: Rig) -> None:
    stored = rig.emit(TERMINAL, EventKind.TURN_STARTED)

    text = render_why(_sources(rig), stored.event_id)

    assert "the chain begins" in text


# -------------------------------------------------------------------- findings


def test_findings_are_listed_loudest_first(rig: Rig) -> None:
    """A code seen four hundred times is the one to look at."""
    for _ in range(3):
        rig.pane(TERMINAL, EventKind.USAGE_CAPPED)
        rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.findings.record(
        FindingCode.DIAG_GHOST_TRANSITION, terminal_id="other", dedupe_key="x", detail="once"
    )

    text = render_findings(_sources(rig), now=rig.clock.now())
    lines = [line for line in text.splitlines() if line.startswith("DIAG-")]

    assert lines[0].startswith(FindingCode.DIAG_BAD_TRANSITION.value)
    assert "3" in lines[0]


def test_findings_carry_the_sample_event_a_filed_issue_needs(rig: Rig) -> None:
    rig.pane(TERMINAL, EventKind.USAGE_CAPPED)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    payload = findings_payload(_sources(rig))
    text = render_findings(_sources(rig), now=rig.clock.now())

    assert payload[0]["sample_event_id"]
    assert payload[0]["sample_event_id"] in text


def test_no_findings_says_so(rig: Rig) -> None:
    assert render_findings(_sources(rig), now=rig.clock.now()) == "no findings"


# ------------------------------------------------------------------- agreement


def test_an_invalid_report_leads_with_its_invalidity(rig: Rig) -> None:
    """It cannot be quoted out of context as a result."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "idle")

    text = render_agreement(build_agreement_report(rig.events.read()))

    assert text.splitlines()[0].startswith("AGREEMENT REPORT — INVALID")
    assert "no conclusion" in text


def test_a_valid_report_says_the_floor_was_met(rig: Rig) -> None:
    for index in range(3):
        terminal = f"term-{index}"
        for _ in range(30):
            rig.emit(terminal, EventKind.TURN_STARTED)
            rig.legacy(terminal, "processing")
            rig.clock.advance(1)
            rig.emit(terminal, EventKind.TURN_ENDED)
            rig.legacy(terminal, "idle")
            rig.clock.advance(1)

    text = render_agreement(build_agreement_report(rig.events.read()))

    assert text.splitlines()[0].startswith("AGREEMENT REPORT — VALID")
    assert "genuine=" in text


def test_the_rate_is_labelled_so_lag_is_not_read_as_breakage(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "processing")

    text = render_agreement(build_agreement_report(rig.events.read()))

    assert "lag counts against it" in text
    assert "the number that matters is genuine" in text


def test_genuine_disagreements_are_listed_individually(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    rig.legacy(TERMINAL, "idle")
    rig.clock.advance(1)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    text = render_agreement(build_agreement_report(rig.events.read()))

    assert "genuine disagreements" in text
    assert "unresolved" in text
