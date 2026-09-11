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

    for index in range(30):
        _edge(rig, "processing", origin=f"origin-{index}")

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


# ----------------------------------------------- the write rate (R3)


class _CountingFindings:
    """Counts WRITES, which is the cost the episode guard is about."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.writes = 0

    def record(self, *args: object, **kwargs: object) -> object:
        self.writes += 1
        return self._inner.record(*args, **kwargs)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _edge(rig: Rig, latched: str, origin: str = "incremental") -> None:
    """One status edge, exactly as production emits it.

    ``_apply_detection``'s ``finally`` block appends ``status.pane_classified``
    and then ``status.legacy_published`` for the SAME ``(latched_status, origin)``
    pair, one after the other, inside one lock.  Both are derived and neither is
    in ``DERIVED_ALWAYS_KINDS``, so for a projected terminal both are muted and
    both reach this check.

    Driving the check with the publish ALONE is what made the r3 episode guard
    look like it worked: the classification row asserts no state, and a guard
    that closed the episode on it re-opened it on every edge.
    """
    rig.classified(TERMINAL, latched, origin)
    rig.legacy(TERMINAL, latched, origin)


def _counting(rig: Rig) -> _CountingFindings:
    from cli_agent_orchestrator.app.worker_truth.checks import ProducerDisagreementCheck

    counter = _CountingFindings(rig.findings)
    rig.projector._producer_check = ProducerDisagreementCheck(counter)  # type: ignore[attr-defined]
    return counter


def test_a_standing_disagreement_writes_once_not_once_per_edge(rig: Rig) -> None:
    """Every write opens a ``BEGIN IMMEDIATE`` and takes SQLite's write lock —
    from the status monitor's locked publish path, on every status edge.

    The table could never flood (the store's record updates the open row), but
    the write RATE could, and a standing disagreement is the common shape: the
    pane reads ``processing`` off a spinner while the rollout has already ended
    the turn.
    """
    counter = _counting(rig)
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    for index in range(20):
        _edge(rig, "processing", origin=f"origin-{index}")

    assert counter.writes == 1


def test_agreement_closes_the_episode_so_a_recurrence_is_recorded(rig: Rig) -> None:
    """The guard must not swallow a NEW disagreement after the two sides
    re-converge — that is a second episode, and it is news."""
    counter = _counting(rig)
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    _edge(rig, "processing")
    _edge(rig, "idle")  # agreement: the episode closes
    _edge(rig, "processing")  # a new one

    assert counter.writes == 2


def test_a_different_pair_is_a_different_episode(rig: Rig) -> None:
    counter = _counting(rig)
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    _edge(rig, "processing")
    _edge(rig, "waiting_user_answer")

    assert counter.writes == 2


def test_the_episode_does_not_survive_the_terminal(rig: Rig) -> None:
    """A recycled id must not have its first real disagreement swallowed."""
    from cli_agent_orchestrator.app.worker_truth.checks import ProducerDisagreementCheck

    counter = _CountingFindings(rig.findings)
    check = ProducerDisagreementCheck(counter)
    rig.projector._producer_check = check  # type: ignore[attr-defined]
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    _edge(rig, "processing")

    check.forget(TERMINAL)
    _edge(rig, "processing", origin="probe")

    assert counter.writes == 2


def test_a_classification_row_does_not_re_open_the_episode(rig: Rig) -> None:
    """The r3 guard's defeat, pinned so it cannot come back.

    ``status.pane_classified`` asserts no state, and production emits one
    immediately before every ``status.legacy_published`` on the same edge.  A
    guard that treated "asserts nothing" as "the disagreement is over" therefore
    re-armed on every single edge and suppressed nothing — while every test that
    drove the publish alone still passed.

    Asserted as the INTERLEAVING rather than as a count, so the failure mode is
    named: classified, published, classified, published.
    """
    counter = _counting(rig)
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    rig.classified(TERMINAL, "processing")
    rig.legacy(TERMINAL, "processing")
    rig.classified(TERMINAL, "processing", origin="probe")
    rig.legacy(TERMINAL, "processing", origin="probe")

    assert counter.writes == 1


def test_only_agreement_closes_the_episode(rig: Rig) -> None:
    """Stated as the rule rather than as one of its consequences.

    Every muted event is one of three things: it agrees (the episode is over), it
    contradicts (the episode continues, or a new one starts), or it asserts
    nothing at all (it is not evidence about the episode either way).
    """
    counter = _counting(rig)
    _sourced(rig)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    _edge(rig, "processing")  # contradiction: one write
    rig.classified(TERMINAL, "processing", origin="probe")  # asserts nothing
    rig.pane(TERMINAL, EventKind.STATUS_PANE_CLASSIFIED)  # likewise, no payload
    _edge(rig, "processing", origin="native")  # still the same episode

    assert counter.writes == 1
