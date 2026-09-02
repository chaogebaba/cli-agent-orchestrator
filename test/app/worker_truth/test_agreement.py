"""AC10 — the report phase 1 exits on.

Two things have to be true of it, and each has its own mutant:

* the classifier must distinguish lag from real disagreement (the mutant returns
  ``genuine`` for everything, which would make the report unreadable and the
  phase unexitable);
* an empty or thin comparison must be INVALID, never "perfect agreement" (the
  most dangerous number this report could print).
"""

from __future__ import annotations

from test.app.conftest import Rig

from cli_agent_orchestrator.app.worker_truth.agreement import (
    MIN_EVENTS,
    MIN_LEGACY_PUBLISHES,
    MIN_TERMINALS,
    MIN_TRANSITIONS,
    TerminalFacts,
    build_agreement_report,
)
from cli_agent_orchestrator.core.events import EventKind, Producer
from cli_agent_orchestrator.core.states import WorkerState

TERMINAL = "term-g1"


def _report(rig: Rig, **kwargs):
    return build_agreement_report(rig.events.read(), **kwargs)


def _classifications(rig: Rig, **kwargs) -> list[str]:
    return [d.classification for d in _report(rig, **kwargs).disagreements]


# ------------------------------------------------------------- the classifier


def test_the_projection_getting_there_first_is_lag_not_disagreement(rig: Rig) -> None:
    """The projection moves to busy, legacy catches up: ``projection_early``."""
    rig.legacy(TERMINAL, "idle")
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    rig.clock.advance(1)

    rig.emit(TERMINAL, EventKind.TURN_STARTED)  # projection -> busy
    rig.clock.advance(2)
    rig.legacy(TERMINAL, "processing")  # legacy follows

    assert _classifications(rig) == ["projection_early"]


def test_legacy_getting_there_first_is_also_lag(rig: Rig) -> None:
    """The mirror case, which the mutant "always genuine" also fails."""
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    rig.legacy(TERMINAL, "idle")
    rig.clock.advance(1)

    rig.legacy(TERMINAL, "processing")  # legacy -> busy first
    rig.clock.advance(2)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)  # projection follows

    assert _classifications(rig) == ["legacy_early"]


def test_a_disagreement_that_never_resolves_is_genuine(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    rig.legacy(TERMINAL, "idle")
    rig.clock.advance(1)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    disagreements = _report(rig).disagreements
    assert [d.classification for d in disagreements] == ["genuine"]
    assert disagreements[0].ended_at is None
    assert disagreements[0].duration_s is None


def test_closing_on_a_third_value_is_genuine(rig: Rig) -> None:
    """Neither side was early: they were both wrong on the way somewhere else."""
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    rig.legacy(TERMINAL, "idle")
    rig.clock.advance(1)

    rig.emit(TERMINAL, EventKind.TURN_STARTED)  # projection busy, legacy idle
    rig.clock.advance(1)
    rig.pane(TERMINAL, EventKind.PROMPT_AWAITING)  # projection awaiting_input
    rig.clock.advance(1)
    rig.legacy(TERMINAL, "waiting_user_answer")  # both awaiting_input

    assert _classifications(rig) == ["genuine"]


def test_a_disagreement_records_the_states_in_force_when_it_ended(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    rig.legacy(TERMINAL, "idle")
    rig.clock.advance(1)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    disagreement = _report(rig).disagreements[0]
    assert disagreement.projected is WorkerState.BUSY
    assert disagreement.legacy is WorkerState.IDLE
    assert disagreement.opened_by == "projection"
    assert disagreement.terminal_id == TERMINAL


def test_two_sides_that_only_ever_lag_produce_no_genuine_disagreement(rig: Rig) -> None:
    """The shape a HEALTHY session actually has, and it is not zero intervals.

    The two sides are written by different producers, so they can never move in
    the same instant: every real state change opens a one-step interval that
    closes as soon as the other side catches up.  That is lag, and the classifier
    exists precisely to keep it out of the count that matters.  A test asserting
    zero intervals would be asserting something no live session can produce, and
    would quietly push the implementation towards hiding real disagreements too.
    """
    for _ in range(5):
        rig.emit(TERMINAL, EventKind.TURN_STARTED)
        rig.legacy(TERMINAL, "processing")
        rig.clock.advance(1)
        rig.emit(TERMINAL, EventKind.TURN_ENDED)
        rig.legacy(TERMINAL, "idle")
        rig.clock.advance(1)

    report = _report(rig)
    counts = report.classification_counts()
    assert counts["genuine"] == 0
    assert counts["projection_early"] > 0
    assert all(d.ended_at is not None for d in report.disagreements)


# ---------------------------------------------------------------- comparisons


def test_one_side_alone_is_never_counted_as_agreement(rig: Rig) -> None:
    """Counting silence as agreement is how an empty session looks perfect."""
    for _ in range(4):
        rig.emit(TERMINAL, EventKind.TURN_STARTED)
        rig.emit(TERMINAL, EventKind.TURN_ENDED)

    report = _report(rig)
    assert report.total_comparisons == 0
    assert report.fleet_agreement_rate is None
    assert report.terminals[0].agreement_rate is None


def test_status_recovered_counts_as_a_projection_move(rig: Rig) -> None:
    """It is a state change with a different name; omitting it would credit the
    projection with a stall it did not have."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.pane(TERMINAL, EventKind.PANE_MISSING)
    rig.legacy(TERMINAL, "unknown")
    rig.clock.advance(1)
    rig.pane(TERMINAL, EventKind.PANE_RECOVERED)

    report = _report(rig)
    assert report.total_transitions >= 3
    assert report.terminals[0].comparisons >= 2


# --------------------------------------------------------------- content floor


def test_an_empty_log_is_invalid_and_says_no_evidence(rig: Rig) -> None:
    report = _report(rig)

    assert report.valid is False
    assert report.fleet_agreement_rate is None
    assert any("no evidence" in reason for reason in report.invalid_reasons)


def test_a_thin_session_names_every_floor_it_missed(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "processing")

    report = _report(rig)

    assert report.valid is False
    joined = " ".join(report.invalid_reasons)
    assert f"need {MIN_TERMINALS}" in joined
    assert f"need {MIN_EVENTS}" in joined
    assert f"need {MIN_TRANSITIONS}" in joined
    assert f"need {MIN_LEGACY_PUBLISHES}" in joined


def test_a_session_with_no_codex_terminal_is_invalid(rig: Rig) -> None:
    """AC10's floor names a codex terminal specifically: the rollout producer is
    the only authoritative source phase 1 has, so a run without one has not
    exercised source-level precedence at all."""
    for index in range(3):
        terminal = f"pane-only-{index}"
        for _ in range(40):
            rig.pane(terminal, EventKind.TURN_STARTED)
            rig.legacy(terminal, "processing")
            rig.pane(terminal, EventKind.TURN_ENDED)
            rig.legacy(terminal, "idle")

    report = _report(rig)

    assert report.codex_terminals == 0
    assert any("codex" in reason for reason in report.invalid_reasons)
    assert report.valid is False


def test_a_full_session_is_valid(rig: Rig) -> None:
    """The shape AC10 requires: three terminals, one of them fed by JSONL."""
    for index in range(3):
        terminal = f"term-{index}"
        producer = Producer.JSONL if index == 0 else Producer.PANE
        for _ in range(30):
            rig.emit(terminal, EventKind.TURN_STARTED, producer=producer)
            rig.legacy(terminal, "processing")
            rig.clock.advance(1)
            rig.emit(terminal, EventKind.TURN_ENDED, producer=producer)
            rig.legacy(terminal, "idle")
            rig.clock.advance(1)

    report = _report(rig)

    assert report.invalid_reasons == []
    assert report.valid is True
    assert report.codex_terminals == 1
    assert report.total_events >= MIN_EVENTS


# ---------------------------------------------------------------------- scope


def test_a_session_filter_uses_the_caller_supplied_scope(rig: Rig) -> None:
    rig.emit("in-scope", EventKind.TURN_STARTED)
    rig.legacy("in-scope", "processing")
    rig.emit("out-of-scope", EventKind.TURN_STARTED)
    rig.legacy("out-of-scope", "processing")

    scope = {
        "in-scope": TerminalFacts(session="alpha", provider="codex"),
        "out-of-scope": TerminalFacts(session="beta", provider="codex"),
    }
    report = _report(rig, scope=scope, session="alpha")

    assert [t.terminal_id for t in report.terminals] == ["in-scope"]


def test_an_unknown_session_reports_no_evidence_rather_than_the_whole_fleet(
    rig: Rig,
) -> None:
    """A report that quietly widened its own scope would be worse than none."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "processing")

    report = _report(rig, scope={TERMINAL: TerminalFacts(session="alpha")}, session="ghost")

    assert report.terminals == []
    assert report.valid is False
    assert any("no evidence" in reason for reason in report.invalid_reasons)


def test_the_declared_provider_beats_the_jsonl_heuristic(rig: Rig) -> None:
    rig.pane(TERMINAL, EventKind.TURN_STARTED)
    rig.legacy(TERMINAL, "processing")

    report = _report(rig, scope={TERMINAL: TerminalFacts(provider="codex")})

    assert report.terminals[0].is_codex is True
