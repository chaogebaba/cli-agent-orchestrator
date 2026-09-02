"""AC6 — the projector's five named rules, each with its own failing mutant.

Every test here is written so that a specific way of getting the projector wrong
makes it fail.  The blueprint's mutant list is the index:

* source-health ignored (derived ``busy`` applied while the rollout is healthy)
* an anomalous event dropped instead of applied
* the transition classifier loosened
* the probe appending a row per tick rather than updating columns
* recovery not restoring ``prior_state``
"""

from __future__ import annotations

from datetime import UTC, datetime
from test.app.conftest import Rig

import pytest

from cli_agent_orchestrator.core.events import Confidence, DecisionKind, EventKind, Producer
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.states import DegradedReason, TransitionClass, WorkerState
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S, PANE_HEARTBEAT_S

TERMINAL = "term-a1"


def _transitions(rig: Rig, terminal_id: str = TERMINAL):
    return [
        row
        for row in rig.events.read(terminal_id)
        if row.decision is DecisionKind.STATUS_TRANSITION
    ]


def _decisions(rig: Rig, kind: DecisionKind, terminal_id: str = TERMINAL):
    return [row for row in rig.events.read(terminal_id) if row.decision is kind]


# ------------------------------------------------------------------ basic fold


def test_first_event_creates_the_projection_from_starting(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    assert rig.state_of(TERMINAL) is WorkerState.BUSY
    rows = _transitions(rig)
    assert len(rows) == 1
    assert rows[0].payload["from"] == "starting"
    assert rows[0].payload["to"] == "busy"


def test_transition_row_names_the_event_that_caused_it(rig: Rig) -> None:
    """The join that makes "why did this flip" answerable (audit §4.2).

    Dropping ``evidence`` from the decision row is a phase-1 mutant, and it is
    killed here and again by the ghost-transition check in test_checks.py.
    """
    cause = rig.emit(TERMINAL, EventKind.TURN_STARTED)

    row = _transitions(rig)[0]
    assert row.evidence == cause.event_id
    assert row.producer is Producer.SERVER
    assert row.decision is DecisionKind.STATUS_TRANSITION


def test_correlation_ids_survive_the_hop_to_the_decision_row(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.SUBMISSION_CONFIRMED, msg_id="M1", run_id="R1")

    row = _transitions(rig)[0]
    assert (row.msg_id, row.run_id) == ("M1", "R1")


def test_turn_ended_returns_the_terminal_to_idle(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    assert rig.state_of(TERMINAL) is WorkerState.IDLE
    assert len(_transitions(rig)) == 2


def test_a_decision_row_never_moves_the_projection(rig: Rig) -> None:
    """Otherwise the projector's own transition rows would feed back into it."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    before = rig.state_of(TERMINAL)

    outcome = rig.projector.project(_transitions(rig)[0])

    assert outcome.rule == "decision_row"
    assert outcome.applied is False
    assert rig.state_of(TERMINAL) is before


# ------------------------------------------------------- rule (a): the diagonal


def test_a_hundred_identical_publishes_write_one_transition_row(rig: Rig) -> None:
    """Rule (a).  The mutant is a projector that writes a row per arrival.

    This is also the projection-side half of AC4b's edge-triggering test: even if
    a producer did publish repeatedly, the fold must not turn that into a
    hundred rows in the log the operator has to read.
    """
    for _ in range(100):
        rig.legacy(TERMINAL, "processing")

    assert rig.state_of(TERMINAL) is WorkerState.BUSY
    assert len(_transitions(rig)) == 1


def test_the_diagonal_keeps_since_and_advances_the_sequence(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    since_before = rig.states.get(TERMINAL).since
    rig.clock.advance(30)

    last = rig.emit(TERMINAL, EventKind.TOOL_CALLED)

    row = rig.states.get(TERMINAL)
    assert row.since == since_before
    assert row.last_event_seq == last.seq


def test_degraded_to_degraded_appends_reason_changed_only_when_the_reason_rises(
    rig: Rig,
) -> None:
    """Rule (a)'s exception, in both directions.

    ``no_signal`` is the lowest rank, so a terminal already degraded for
    ``producer_error`` must not be relabelled by a later, weaker reason.  A
    projector that took the newest reason unconditionally passes the first half
    of this test and fails the second.
    """
    rig.pane(
        TERMINAL,
        EventKind.PANE_MISSING,
    )
    assert rig.states.get(TERMINAL).degraded_reason is DegradedReason.PANE_UNREADABLE

    rig.pane(
        TERMINAL,
        EventKind.STATUS_LEGACY_PUBLISHED,
        payload={"latched_status": "unknown"},
    )
    # render_uncertain ranks BELOW pane_unreadable: no change, no row.
    assert rig.states.get(TERMINAL).degraded_reason is DegradedReason.PANE_UNREADABLE
    assert _decisions(rig, DecisionKind.STATUS_REASON_CHANGED) == []

    rig.pane(
        TERMINAL,
        EventKind.PANE_MISSING,
        payload={"degraded_reason": DegradedReason.PRODUCER_ERROR.value},
    )
    assert rig.states.get(TERMINAL).degraded_reason is DegradedReason.PRODUCER_ERROR
    rows = _decisions(rig, DecisionKind.STATUS_REASON_CHANGED)
    assert len(rows) == 1
    assert rows[0].payload["from_reason"] == DegradedReason.PANE_UNREADABLE.value
    assert rows[0].payload["to_reason"] == DegradedReason.PRODUCER_ERROR.value


# -------------------------------------------------- source-level precedence (r9)


def test_derived_busy_is_ignored_while_the_authoritative_source_is_healthy(rig: Rig) -> None:
    """The mutant this kills: source-health ignored.

    A codex terminal whose rollout is being tailed is IDLE between turns.  A pane
    classifier that thinks it is processing must not be able to say so.
    """
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    assert rig.state_of(TERMINAL) is WorkerState.IDLE

    outcome = rig.legacy(TERMINAL, "processing")

    assert outcome is not None
    assert rig.state_of(TERMINAL) is WorkerState.IDLE
    assert len(_transitions(rig)) == 1


def test_the_muted_event_is_still_logged_and_still_advances_the_sequence(rig: Rig) -> None:
    """ "Logged but not applied" is the contract, not "dropped"."""
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    muted = rig.legacy(TERMINAL, "processing")

    assert rig.events.get(muted.event_id) is not None
    assert rig.states.get(TERMINAL).last_event_seq == muted.seq


def test_derived_events_apply_once_the_source_has_gone_silent(rig: Rig) -> None:
    """The fallback is legitimate, and it writes no finding when it takes over."""
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    rig.clock.advance(NO_SIGNAL_S + 1)
    rig.legacy(TERMINAL, "processing")

    assert rig.state_of(TERMINAL) is WorkerState.BUSY
    assert rig.findings.list_findings(state="open") == []


def test_a_terminal_with_no_authoritative_source_applies_derived_events(rig: Rig) -> None:
    rig.legacy(TERMINAL, "processing")

    assert rig.state_of(TERMINAL) is WorkerState.BUSY


def test_a_source_that_never_probed_is_not_healthy(rig: Rig) -> None:
    """ "No probe yet" must not mute the fallback.

    A tailer that failed to start leaves ``last_source_probe_at`` NULL forever.
    Reading that as healthy would silence the pane at exactly the moment the pane
    is the only thing left.
    """
    rig.sources.add(TERMINAL)

    rig.legacy(TERMINAL, "processing")

    assert rig.state_of(TERMINAL) is WorkerState.BUSY


@pytest.mark.parametrize(
    "kind,expected",
    [
        (EventKind.PROMPT_AWAITING, WorkerState.AWAITING_INPUT),
        (EventKind.USAGE_CAPPED, WorkerState.CAPPED),
        (EventKind.PROCESS_EXITED, WorkerState.EXITED),
    ],
)
def test_kinds_the_source_cannot_know_apply_even_while_it_is_healthy(
    rig: Rig, kind: EventKind, expected: WorkerState
) -> None:
    """The dialog cards (#386 family), the cap banner, and the exit.

    None of the three appears in a codex rollout, so muting them behind source
    health would make a healthy source hide the events it structurally cannot
    produce.
    """
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    rig.pane(TERMINAL, kind)

    assert rig.state_of(TERMINAL) is expected


def test_pane_missing_does_not_degrade_a_terminal_whose_source_is_healthy(rig: Rig) -> None:
    """The deliberate asymmetry, asserted so it cannot be "fixed" by accident.

    A rollout that is still being written proves the agent is alive.  A vanished
    tmux pane is then a rendering problem, and degrading a demonstrably working
    worker would put it in front of the capped-lane policy as ``Unknown``.  If it
    really died, ``process.exited`` says so two probe ticks later.
    """
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    rig.pane(TERMINAL, EventKind.PANE_MISSING)

    assert rig.state_of(TERMINAL) is WorkerState.BUSY


# ------------------------------------------------------ rule (b): recovery path


def test_pane_recovered_restores_prior_state(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.pane(TERMINAL, EventKind.PANE_MISSING)
    assert rig.state_of(TERMINAL) is WorkerState.DEGRADED
    assert rig.states.get(TERMINAL).prior_state is WorkerState.BUSY

    rig.pane(TERMINAL, EventKind.PANE_RECOVERED)

    row = rig.states.get(TERMINAL)
    assert row.state is WorkerState.BUSY
    assert row.degraded_reason is None
    assert row.prior_state is None
    recovered = _decisions(rig, DecisionKind.STATUS_RECOVERED)
    assert len(recovered) == 1
    assert recovered[0].payload["to"] == "busy"


def test_pane_recovered_on_a_healthy_terminal_changes_nothing(rig: Rig) -> None:
    """The probe fires ``pane.recovered`` for panes it had merely not confirmed.

    Treating that as a state change would let a routine probe tick knock a busy
    worker back to idle.
    """
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    outcome = rig.pane(TERMINAL, EventKind.PANE_RECOVERED)

    assert outcome is not None
    assert rig.state_of(TERMINAL) is WorkerState.BUSY
    assert _decisions(rig, DecisionKind.STATUS_RECOVERED) == []


def test_recovery_is_never_muted_by_a_healthy_source(rig: Rig) -> None:
    """Otherwise a terminal that degraded while its source was down sticks there.

    AC4b is explicit that an idle terminal must never stick in degraded, and this
    is the path that would do it: degrade while the source is silent, source
    comes back, recovery arrives muted, degraded forever.
    """
    rig.sources.add(TERMINAL)
    rig.emit(TERMINAL, EventKind.TURN_ENDED)
    rig.pane(TERMINAL, EventKind.PANE_MISSING)
    assert rig.state_of(TERMINAL) is WorkerState.DEGRADED

    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.pane(TERMINAL, EventKind.PANE_RECOVERED)

    assert rig.state_of(TERMINAL) is WorkerState.IDLE


# ------------------------------------------------ rule (d): anomalous is applied


def test_capped_to_busy_is_applied_and_flagged(rig: Rig) -> None:
    """The blueprint's named AC6 case, and two mutants at once.

    Dropping the anomalous event leaves the state at ``capped`` — a supervisor
    would keep the lane parked while it was working.  Loosening the table to call
    the cell ``Allowed`` loses the finding.  Only applying AND flagging passes.
    """
    rig.pane(TERMINAL, EventKind.USAGE_CAPPED)
    assert rig.state_of(TERMINAL) is WorkerState.CAPPED

    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    assert rig.state_of(TERMINAL) is WorkerState.BUSY
    findings = rig.findings.list_findings(code=FindingCode.DIAG_BAD_TRANSITION)
    assert len(findings) == 1
    assert findings[0].dedupe_key == "capped->busy"
    assert _transitions(rig)[-1].payload["classification"] == TransitionClass.ANOMALOUS.value


def test_repeated_anomalous_cells_increment_one_finding(rig: Rig) -> None:
    for _ in range(5):
        rig.pane(TERMINAL, EventKind.USAGE_CAPPED)
        rig.emit(TERMINAL, EventKind.TURN_STARTED)

    findings = rig.findings.list_findings(code=FindingCode.DIAG_BAD_TRANSITION)
    assert len(findings) == 1
    assert findings[0].count == 5


# ------------------------------------------------------------------- the sweep


def test_silence_degrades_only_after_the_sweep_runs(rig: Rig) -> None:
    """A projector cannot notice silence by itself (r8 N5)."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.states.touch_probe(
        TERMINAL, probed_at=rig.clock.now(), pane_present=True, pane_pid=1234, miss_count=0
    )

    rig.clock.advance(NO_SIGNAL_S + 5)
    assert rig.state_of(TERMINAL) is WorkerState.BUSY

    outcomes = rig.projector.sweep()

    assert [o.rule for o in outcomes] == ["no_signal_sweep"]
    row = rig.states.get(TERMINAL)
    assert row.state is WorkerState.DEGRADED
    assert row.degraded_reason is DegradedReason.NO_SIGNAL
    assert row.prior_state is WorkerState.BUSY


def test_a_killed_tailer_degrades_and_never_exits(rig: Rig) -> None:
    """``process.exited`` has exactly one owner, the probe (AC6 rule (c))."""
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    rig.clock.advance(NO_SIGNAL_S * 3)
    rig.projector.sweep()

    assert rig.state_of(TERMINAL) is WorkerState.DEGRADED
    assert rig.state_of(TERMINAL) is not WorkerState.EXITED


def test_one_missed_probe_never_degrades(rig: Rig) -> None:
    """``NO_SIGNAL_S > PANE_HEARTBEAT_S * 2`` expressed as behaviour."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.states.touch_probe(
        TERMINAL, probed_at=rig.clock.now(), pane_present=True, pane_pid=1, miss_count=0
    )

    rig.clock.advance(PANE_HEARTBEAT_S * 2)
    assert rig.projector.sweep() == []
    assert rig.state_of(TERMINAL) is WorkerState.BUSY


def test_a_fresh_probe_keeps_a_quiet_terminal_out_of_degraded(rig: Rig) -> None:
    """A terminal that is merely idle is idle, however long it stays that way."""
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    for _ in range(20):
        rig.clock.advance(PANE_HEARTBEAT_S)
        rig.states.touch_probe(
            TERMINAL, probed_at=rig.clock.now(), pane_present=True, pane_pid=1, miss_count=0
        )
        assert rig.projector.sweep() == []

    assert rig.state_of(TERMINAL) is WorkerState.IDLE


def test_the_sweep_skips_exited_terminals(rig: Rig) -> None:
    rig.pane(TERMINAL, EventKind.PROCESS_EXITED, payload={"reason": "teardown"})
    rig.clock.advance(NO_SIGNAL_S * 10)

    assert rig.projector.sweep() == []
    assert rig.state_of(TERMINAL) is WorkerState.EXITED


def test_the_sweep_skips_a_projection_with_no_events(rig: Rig) -> None:
    """A row created by a probe alone has never spoken, so it cannot fall silent.

    It would also produce a transition row with nothing to cite, which
    ``DIAG-GHOST-TRANSITION`` would then correctly complain about — a check
    firing on the projector's own correct behaviour is a check nobody trusts.
    """
    rig.states.touch_probe(
        TERMINAL, probed_at=rig.clock.now(), pane_present=True, pane_pid=7, miss_count=0
    )
    rig.clock.advance(NO_SIGNAL_S * 5)

    assert rig.projector.sweep() == []
    assert rig.findings.list_findings(code=FindingCode.DIAG_GHOST_TRANSITION) == []


def test_the_sweep_cites_the_last_thing_that_was_heard(rig: Rig) -> None:
    """Evidence for "nothing since" is the final event before the silence."""
    last = rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.clock.advance(NO_SIGNAL_S + 5)

    rig.projector.sweep()

    row = _transitions(rig)[-1]
    assert row.evidence == last.event_id
    assert row.payload["rule"] == "no_signal_sweep"
    assert rig.findings.list_findings(code=FindingCode.DIAG_GHOST_TRANSITION) == []


def test_no_signal_never_overwrites_a_stronger_reason(rig: Rig) -> None:
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.pane(
        TERMINAL,
        EventKind.PANE_MISSING,
        payload={"degraded_reason": DegradedReason.PRODUCER_ERROR.value},
    )
    rig.clock.advance(NO_SIGNAL_S * 2)

    outcomes = rig.projector.sweep()

    assert [o.rule for o in outcomes] == ["no_signal_already_degraded"]
    assert rig.states.get(TERMINAL).degraded_reason is DegradedReason.PRODUCER_ERROR


def test_the_sweep_covers_every_terminal_in_one_pass(rig: Rig) -> None:
    for index in range(3):
        rig.emit(f"term-{index}", EventKind.TURN_STARTED)
    rig.clock.advance(NO_SIGNAL_S + 1)

    outcomes = rig.projector.sweep()

    assert sorted(o.terminal_id for o in outcomes) == ["term-0", "term-1", "term-2"]


# ------------------------------------------------------------- ordering hygiene


def test_since_uses_the_server_clock_not_the_source_clock(rig: Rig) -> None:
    """Mixing ``observed_at`` into a row ordered by ``ingested_at`` breeds negative
    durations, which the agreement report would then have to explain away."""
    skewed = datetime(2019, 1, 1, tzinfo=UTC)

    stored = rig.emit(TERMINAL, EventKind.TURN_STARTED, observed_at=skewed)

    assert rig.states.get(TERMINAL).since == stored.ingested_at


def test_an_unmapped_legacy_status_moves_nothing(rig: Rig) -> None:
    """ "No opinion" beats a guessed state that the agreement report would then
    compare against the projection."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)

    outcome = rig.legacy(TERMINAL, "some_new_status_from_the_future")

    assert outcome is not None
    assert rig.state_of(TERMINAL) is WorkerState.BUSY
    assert len(_transitions(rig)) == 1


def test_confidence_not_producer_decides_muting(rig: Rig) -> None:
    """An authoritative row from any producer applies while a source is healthy."""
    rig.sources.add(TERMINAL)
    rig.states.touch_source_probe(TERMINAL, probed_at=rig.clock.now())
    rig.emit(TERMINAL, EventKind.TURN_ENDED)

    rig.emit(
        TERMINAL,
        EventKind.TURN_STARTED,
        producer=Producer.HOOK,
        confidence=Confidence.AUTHORITATIVE,
    )

    assert rig.state_of(TERMINAL) is WorkerState.BUSY
