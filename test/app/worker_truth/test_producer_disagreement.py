"""``DIAG-PRODUCER-DISAGREE`` — plan slice-2 case 15, blueprint D9b.

The other two disagreement checks compare the projection against a RECORD, and
they are durational because a record sits there while the clocks drift apart.
This one is instantaneous and structural: source-level precedence has just MUTED
a derived event, which means the projector held two readings of one terminal and
chose between them.  If the reading it discarded asserted a different state, two
producers disagree — and nothing downstream will ever say so, because the muted
event is in the log and applied to nothing, which is exactly how it should be and
exactly why the disagreement is invisible without a finding.
"""

from __future__ import annotations

from test.app.conftest import Rig

from cli_agent_orchestrator.core.events import Confidence, EventKind, Producer
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.states import WorkerState

TERMINAL = "term-p1"


def _sourced(rig: Rig) -> None:
    """A codex-shaped lane: a registered tailer that has just stat-ed its file."""
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())


def _findings(rig: Rig) -> list[object]:
    return rig.findings.list_findings(code=FindingCode.DIAG_PRODUCER_DISAGREE)


def test_a_muted_reading_that_contradicts_the_projection_is_a_finding(rig: Rig) -> None:
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)  # the source says idle
    assert rig.state_of(TERMINAL) is WorkerState.IDLE

    rig.legacy(TERMINAL, "processing")  # the pane says busy, and is muted

    findings = _findings(rig)
    assert len(findings) == 1
    assert findings[0].terminal_id == TERMINAL
    assert findings[0].dedupe_key == "idle|busy"
    assert "muted" in findings[0].detail


def test_a_muted_reading_that_agrees_is_silent(rig: Rig) -> None:
    """Agreement is the normal case and must cost nothing.

    A check that fired on every muted event would report the precedence rule
    working as designed, thousands of times an hour.
    """
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    rig.legacy(TERMINAL, "processing")

    assert _findings(rig) == []


def test_a_standing_disagreement_is_one_finding_with_a_count(rig: Rig) -> None:
    """Deduped per ``(projected, asserted)`` pair.

    A pane that reads ``processing`` against an idle rollout for an hour is one
    finding, not one every time the pane repaints.
    """
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    for _ in range(30):
        rig.legacy(TERMINAL, "processing")

    assert len(_findings(rig)) == 1


def test_an_unsourced_terminal_never_reports_a_disagreement(rig: Rig) -> None:
    """Nothing is muted for it, so there is no second reading to disagree with.

    I7 again: for an unsourced lane the pane IS the producer of record, and a
    finding here would be the check complaining that the only producer spoke.
    """
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    rig.legacy(TERMINAL, "processing")

    assert _findings(rig) == []
    assert rig.state_of(TERMINAL) is WorkerState.BUSY


def test_a_dead_source_stops_the_finding_with_the_muting(rig: Rig) -> None:
    """Precedence and the check are the same predicate, read twice.

    Once the source goes quiet the pane applies in full, so there is no discarded
    reading — and a check that kept firing would be reporting a fallback that is
    working.
    """
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    rig.clock.advance(10_000)

    rig.legacy(TERMINAL, "processing")

    assert _findings(rig) == []
    assert rig.state_of(TERMINAL) is WorkerState.BUSY


def test_a_kind_that_asserts_nothing_cannot_disagree(rig: Rig) -> None:
    """``status.pane_classified`` records a reading; it never asserts a state."""
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    rig.classified(TERMINAL, "processing")

    assert _findings(rig) == []


def test_a_muted_state_asserting_kind_disagrees_by_its_kind(rig: Rig) -> None:
    """Not only the legacy publish: any muted kind that names a state counts."""
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    rig.pane(TERMINAL, EventKind.TOOL_CALLED)  # derived, muted, asserts BUSY

    findings = _findings(rig)
    assert len(findings) == 1
    assert findings[0].dedupe_key == "idle|busy"


def test_the_sample_names_the_muted_event(rig: Rig) -> None:
    """``cao diag --why`` has to land on the row that was discarded."""
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    muted = rig.legacy(TERMINAL, "processing")

    assert _findings(rig)[0].sample_event_id == muted.event_id


def test_a_check_that_explodes_never_breaks_the_fold(rig: Rig) -> None:
    class _Hostile:
        def record(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("finding store unavailable")

        def list_findings(self, **kwargs: object) -> list[object]:
            return []

        def resolve(self, finding_id: str) -> bool:
            return False

    from cli_agent_orchestrator.app.worker_truth.checks import ProducerDisagreementCheck

    rig.projector._producer_check = ProducerDisagreementCheck(_Hostile())  # type: ignore[attr-defined]
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    rig.legacy(TERMINAL, "processing")  # must not raise

    assert rig.state_of(TERMINAL) is WorkerState.IDLE


def test_the_derived_always_kinds_are_applied_and_so_never_disagree(rig: Rig) -> None:
    """A kind the source cannot know is not muted, so it moves the projection
    rather than contradicting it — D1f's whole point."""
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    rig.pane(TERMINAL, EventKind.PROMPT_AWAITING)

    assert rig.state_of(TERMINAL) is WorkerState.AWAITING_INPUT
    assert _findings(rig) == []


def test_an_authoritative_event_is_never_read_as_a_disagreement(rig: Rig) -> None:
    """Only a DERIVED event can be muted by precedence, so an authoritative one
    reaching the check would mean the rule changed shape underneath it."""
    from cli_agent_orchestrator.app.worker_truth.checks import ProducerDisagreementCheck
    from cli_agent_orchestrator.app.worker_truth.projector import ProjectedState

    check = ProducerDisagreementCheck(rig.findings)
    _sourced(rig)
    event = rig.events.append(
        __import__("cli_agent_orchestrator.core.events", fromlist=["EventDraft"]).EventDraft(
            terminal_id=TERMINAL,
            kind=EventKind.TURN_STARTED,
            producer=Producer.JSONL,
            confidence=Confidence.AUTHORITATIVE,
            observed_at=rig.clock.now(),
        )
    )

    assert check(event, ProjectedState(terminal_id=TERMINAL, since=rig.clock.now())) is False
