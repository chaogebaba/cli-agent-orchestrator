"""Shadow projection vs legacy published status (WP-ARCH phase 1, AC10).

This is the report phase 1 exits on, so its job is to be hard to fool rather than
to look good.  Three design choices follow from that:

**Both sides come out of ``worker_event``.**  The projected side is the
projector's own ``status.transition``/``status.recovered`` decision rows; the
legacy side is ``status.legacy_published``, which is what the fleet and the inbox
actually consume.  r9 rejected comparing against the raw classification because
the projector is fed by that same classification through the legacy egress, and
a comparison against your own input measures nothing.

**A disagreement is an interval, not a row.**  The two sides are written by
different producers on different clocks, so they are never simultaneous.  What
matters is whether one side got somewhere first and the other followed
(``projection_early``/``legacy_early``, which is lag) or whether they genuinely
disagreed about what the worker was doing (``genuine``).  The classifier below
decides that by asking what value the interval CLOSED on, which is the only
question whose answer distinguishes the two.

**An empty comparison is invalid, never perfect.**  A run with no rows would
otherwise report 100% agreement, which is the most dangerous number this report
could print.  The content floor is checked first and reported as reasons, and a
report that fails it says so at the top.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from cli_agent_orchestrator.app.worker_truth.mapping import legacy_state
from cli_agent_orchestrator.core.events import (
    FLEET_TERMINAL_ID,
    DecisionKind,
    EventKind,
    Producer,
    WorkerEvent,
)
from cli_agent_orchestrator.core.states import WorkerState

__all__ = [
    "MIN_CODEX_TERMINALS",
    "MIN_EVENTS",
    "MIN_LEGACY_PUBLISHES",
    "MIN_TERMINALS",
    "MIN_TRANSITIONS",
    "AgreementReport",
    "Disagreement",
    "TerminalAgreement",
    "TerminalFacts",
    "build_agreement_report",
]

#: The AC10 content floor.  Below any of these the report is INVALID and says
#: why; it never reports agreement it did not have the evidence to measure.
MIN_TERMINALS = 3
MIN_CODEX_TERMINALS = 1
MIN_EVENTS = 200
MIN_TRANSITIONS = 20
MIN_LEGACY_PUBLISHES = 20

#: Decision rows that move the shadow state.  ``status.recovered`` is included
#: alongside ``status.transition``: it is a state change with a different name,
#: and leaving it out would credit the projection with a stall it did not have.
_PROJECTION_KINDS = frozenset({DecisionKind.STATUS_TRANSITION, DecisionKind.STATUS_RECOVERED})


@dataclass(frozen=True)
class TerminalFacts:
    """What the caller knows about a terminal that the event log does not.

    Supplied by the composition root from the legacy ``terminals`` table, which
    is where ``tmux_session`` and ``provider`` live.  ``app`` may not read that
    table itself, and the report is more useful — and far easier to test — when
    the scope is data rather than a query.
    """

    session: str = ""
    provider: str = ""


@dataclass(frozen=True)
class Disagreement:
    """One interval during which the two sides held different states."""

    terminal_id: str
    projected: WorkerState
    legacy: WorkerState
    started_at: datetime
    ended_at: datetime | None
    classification: str
    opened_by: str
    sample_event_id: str

    @property
    def duration_s(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()


@dataclass(frozen=True)
class TerminalAgreement:
    """Per-terminal comparison result."""

    terminal_id: str
    session: str = ""
    provider: str = ""
    is_codex: bool = False
    events: int = 0
    transitions: int = 0
    legacy_publishes: int = 0
    comparisons: int = 0
    agreements: int = 0
    disagreements: list[Disagreement] = field(default_factory=list)

    @property
    def agreement_rate(self) -> float | None:
        """Fraction of comparison points where both sides held the same state.

        ``None``, not 1.0, when there were no comparison points.  A terminal
        nobody could compare has no rate, and averaging a fabricated 1.0 into the
        fleet summary is exactly how an empty session would come to look perfect.
        """
        if self.comparisons == 0:
            return None
        return self.agreements / self.comparisons


@dataclass(frozen=True)
class AgreementReport:
    """The fleet summary AC10 requires attached before phase 2 may start."""

    valid: bool
    invalid_reasons: list[str]
    terminals: list[TerminalAgreement]
    total_events: int
    total_transitions: int
    total_legacy_publishes: int
    codex_terminals: int
    generated_at: datetime | None = None

    @property
    def total_comparisons(self) -> int:
        return sum(t.comparisons for t in self.terminals)

    @property
    def total_agreements(self) -> int:
        return sum(t.agreements for t in self.terminals)

    @property
    def disagreements(self) -> list[Disagreement]:
        return [d for t in self.terminals for d in t.disagreements]

    @property
    def fleet_agreement_rate(self) -> float | None:
        """``None`` when nothing was comparable — see :meth:`TerminalAgreement.agreement_rate`."""
        if self.total_comparisons == 0:
            return None
        return self.total_agreements / self.total_comparisons

    def classification_counts(self) -> dict[str, int]:
        counts = {"projection_early": 0, "legacy_early": 0, "genuine": 0}
        for disagreement in self.disagreements:
            counts[disagreement.classification] = counts.get(disagreement.classification, 0) + 1
        return counts


def build_agreement_report(
    events: Iterable[WorkerEvent],
    *,
    scope: Mapping[str, TerminalFacts] | None = None,
    session: str | None = None,
    generated_at: datetime | None = None,
) -> AgreementReport:
    """Compare the shadow projection against the legacy published status.

    ``session`` filters by the caller-supplied ``scope``.  Asking for a session
    the scope does not describe yields an empty, INVALID report rather than a
    silent fleet-wide comparison: a report that quietly widened its own scope
    would be worse than no report.

    Rows carrying :data:`~core.events.FLEET_TERMINAL_ID` are dropped before
    grouping — they describe the fleet, not a worker, and counting the sentinel
    as a terminal would inflate the content floor.
    """
    facts = dict(scope or {})
    by_terminal: dict[str, list[WorkerEvent]] = {}
    for event in events:
        if event.terminal_id == FLEET_TERMINAL_ID:
            # Fleet rows are about the FLEET, not about a worker, so they are not
            # a terminal and must never be counted as one.  ``probe.failed`` is
            # the phase-1 case: a failed probe names no pane, but the column is
            # NOT NULL, so those rows carry the sentinel.
            #
            # This is a content-floor bug, not a cosmetic one.  The floor demands
            # at least three terminals precisely so a thin session cannot claim
            # agreement it has not measured.  Counting the sentinel would let a
            # session with two real workers pass it while contributing zero
            # comparisons — the exact shape the floor exists to reject.
            continue
        if session is not None and facts.get(event.terminal_id, TerminalFacts()).session != session:
            continue
        by_terminal.setdefault(event.terminal_id, []).append(event)

    terminals = [
        _compare_terminal(terminal_id, rows, facts.get(terminal_id, TerminalFacts()))
        for terminal_id, rows in sorted(by_terminal.items())
    ]

    total_events = sum(t.events for t in terminals)
    total_transitions = sum(t.transitions for t in terminals)
    total_legacy = sum(t.legacy_publishes for t in terminals)
    codex_terminals = sum(1 for t in terminals if t.is_codex)

    reasons = _floor_violations(
        terminals=len(terminals),
        codex_terminals=codex_terminals,
        events=total_events,
        transitions=total_transitions,
        legacy_publishes=total_legacy,
    )

    return AgreementReport(
        valid=not reasons,
        invalid_reasons=reasons,
        terminals=terminals,
        total_events=total_events,
        total_transitions=total_transitions,
        total_legacy_publishes=total_legacy,
        codex_terminals=codex_terminals,
        generated_at=generated_at,
    )


def _floor_violations(
    *,
    terminals: int,
    codex_terminals: int,
    events: int,
    transitions: int,
    legacy_publishes: int,
) -> list[str]:
    """The AC10 content floor, as human-readable reasons rather than a boolean."""
    reasons: list[str] = []
    if terminals == 0:
        reasons.append("no evidence: the log contains no events in scope")
    if terminals < MIN_TERMINALS:
        reasons.append(f"{terminals} terminals, need {MIN_TERMINALS}")
    if codex_terminals < MIN_CODEX_TERMINALS:
        reasons.append(f"{codex_terminals} codex terminals, need {MIN_CODEX_TERMINALS}")
    if events < MIN_EVENTS:
        reasons.append(f"{events} events, need {MIN_EVENTS}")
    if transitions < MIN_TRANSITIONS:
        reasons.append(f"{transitions} status.transition rows, need {MIN_TRANSITIONS}")
    if legacy_publishes < MIN_LEGACY_PUBLISHES:
        reasons.append(
            f"{legacy_publishes} status.legacy_published rows, need {MIN_LEGACY_PUBLISHES}"
        )
    return reasons


@dataclass
class _Interval:
    """A disagreement being accumulated, before it is known how it ends."""

    projected: WorkerState
    legacy: WorkerState
    started_at: datetime
    opened_by: str
    opened_value: WorkerState
    sample_event_id: str


def _compare_terminal(
    terminal_id: str, rows: list[WorkerEvent], facts: TerminalFacts
) -> TerminalAgreement:
    """Walk one terminal's two state streams in ``ingested_at`` order."""
    ordered = sorted(rows, key=lambda e: (e.ingested_at, e.seq))

    projected: WorkerState | None = None
    legacy: WorkerState | None = None
    open_interval: _Interval | None = None
    disagreements: list[Disagreement] = []
    comparisons = 0
    agreements = 0
    transitions = 0
    legacy_publishes = 0
    # A terminal counts as codex when a JSONL producer wrote for it.  The event
    # log has no provider column, and this is not a guess: only the codex rollout
    # tailer produces ``jsonl`` rows in phase 1.  The caller-supplied provider
    # from the legacy terminals table wins when it is available.
    saw_jsonl = False

    for event in ordered:
        if event.producer is Producer.JSONL:
            saw_jsonl = True

        side: str | None = None
        value: WorkerState | None = None

        if event.decision in _PROJECTION_KINDS:
            transitions += 1
            raw = event.payload.get("to")
            if isinstance(raw, str):
                try:
                    value = WorkerState(raw)
                except ValueError:
                    value = None
            side = "projection"
        elif event.kind is EventKind.STATUS_LEGACY_PUBLISHED and event.decision is None:
            legacy_publishes += 1
            raw_status = event.payload.get("latched_status")
            value = legacy_state(raw_status) if isinstance(raw_status, str) else None
            side = "legacy"

        if side is None or value is None:
            continue

        if side == "projection":
            projected = value
        else:
            legacy = value

        if projected is None or legacy is None:
            # Only one side has spoken so far.  There is nothing to compare, and
            # counting it as an agreement would inflate the rate with silence.
            continue

        comparisons += 1
        if projected == legacy:
            agreements += 1
            if open_interval is not None:
                disagreements.append(
                    _close(
                        open_interval,
                        terminal_id=terminal_id,
                        at=event.ingested_at,
                        agreed_on=projected,
                    )
                )
                open_interval = None
            continue

        if open_interval is None:
            open_interval = _Interval(
                projected=projected,
                legacy=legacy,
                started_at=event.ingested_at,
                opened_by=side,
                opened_value=value,
                sample_event_id=event.event_id,
            )
        else:
            # The disagreement changed shape without resolving — one side moved
            # again while they were still apart.  The interval keeps its opening
            # side but adopts the newest pair, so the report names the states
            # that were actually in force when it ended.
            open_interval.projected = projected
            open_interval.legacy = legacy

    if open_interval is not None:
        disagreements.append(
            _close(open_interval, terminal_id=terminal_id, at=None, agreed_on=None)
        )

    return TerminalAgreement(
        terminal_id=terminal_id,
        session=facts.session,
        provider=facts.provider,
        is_codex=facts.provider.startswith("codex") or (not facts.provider and saw_jsonl),
        events=len(ordered),
        transitions=transitions,
        legacy_publishes=legacy_publishes,
        comparisons=comparisons,
        agreements=agreements,
        disagreements=disagreements,
    )


def _close(
    interval: _Interval,
    *,
    terminal_id: str,
    at: datetime | None,
    agreed_on: WorkerState | None,
) -> Disagreement:
    """Classify a disagreement by the value it closed on.

    The rule in one sentence: if the two sides ended up agreeing on the value the
    interval OPENED with, the side that opened it simply got there first, and the
    disagreement was lag.  Anything else — closing on a third value, closing back
    on the other side's value (the opener flapped), or never closing at all — is
    a genuine disagreement about what the worker was doing.

    Returning ``genuine`` unconditionally is one of the phase-1 mutants, so the
    two early classifications are each covered by their own test.
    """
    if at is None or agreed_on is None:
        classification = "genuine"
    elif agreed_on == interval.opened_value:
        classification = (
            "projection_early" if interval.opened_by == "projection" else "legacy_early"
        )
    else:
        classification = "genuine"

    return Disagreement(
        terminal_id=terminal_id,
        projected=interval.projected,
        legacy=interval.legacy,
        started_at=interval.started_at,
        ended_at=at,
        classification=classification,
        opened_by=interval.opened_by,
        sample_event_id=interval.sample_event_id,
    )
