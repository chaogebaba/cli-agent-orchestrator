"""Fold worker events into the shadow state projection (WP-ARCH phase 1, AC6).

The projector is the only writer of ``worker_state_shadow`` and, in phase 1, the
projection has no readers in ``services/``.  That is what makes AC11's "no
behaviour change with the switch ON" true by construction rather than by
assertion: the projector can be wrong for a whole session and nothing downstream
notices, which is precisely the point of a shadow phase and of the agreement
report (AC10) that measures it.

Four rules carry the design, and each is a named method below rather than a
branch inside one loop, because the gate has to be able to point at them:

**Source-level precedence (r9).**  A terminal has at most one authoritative
source, declared by its adapter.  While that source is HEALTHY — its tailer
stat-ed the file within ``NO_SIGNAL_S``, recorded in the
``last_source_probe_at`` COLUMN — derived events are logged but applied only for
the kinds the source cannot know.  While it is unhealthy, or for a terminal with
no authoritative source, derived events apply fully and no finding is written:
the pane is a first-class fallback, not a deprecated one.  This replaced r8's
settle window, and it is why the projector never compares timestamps between two
producers.

**The diagonal is a no-op.**  Same-state re-entry keeps ``since`` and advances
``last_event_seq`` without a transition row, so a hundred identical publishes
cost one column update.  The single exception is ``degraded -> degraded`` with a
RISING reason, which appends ``status.reason_changed``: ``no_signal`` must never
overwrite ``producer_error``.

**An anomalous cell is applied, never dropped.**  ``validate()`` classifies; it
does not authorise.  Dropping the event would leave the projection stale and
silent, which is the failure mode this work package exists to end.  The
``DIAG-BAD-TRANSITION`` finding fires from the appended transition row, in
``checks.py``.

**Silence is noticed by a sweep, not by the projector.**  A projector only ever
runs when something arrives, so it cannot by itself observe that nothing has
(r8 N5).  :meth:`Projector.sweep` runs every ``PANE_HEARTBEAT_S`` and is the only
producer of ``degraded(no_signal)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from cli_agent_orchestrator.core.events import (
    Confidence,
    DecisionKind,
    EventDraft,
    EventKind,
    Producer,
    WorkerEvent,
)
from cli_agent_orchestrator.core.ports import Clock, EventStore, StateProjection, StateStore
from cli_agent_orchestrator.core.states import (
    DegradedReason,
    TransitionClass,
    WorkerState,
    reason_rises,
    validate,
)
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S

from cli_agent_orchestrator.app.worker_truth.mapping import (
    PANE_MISSING_REASON,
    implied_state,
    legacy_state,
)

__all__ = [
    "DERIVED_ALWAYS_KINDS",
    "NullSourceRegistry",
    "ProjectionOutcome",
    "Projector",
    "ShadowState",
    "SourceRegistry",
    "StaticSourceRegistry",
]

logger = logging.getLogger(__name__)


def _no_check(terminal_id: str) -> bool:
    """The default durational check: none."""
    return False


#: The kinds an authoritative source CANNOT know, so a derived producer stays
#: authoritative for them even while the real source is healthy (audit §3.1).
#:
#: The blueprint lists four.  ``pane.recovered`` is the fifth here, and the
#: reason is stated rather than assumed: it asserts no state of its own, it only
#: cancels a degradation that this same derived producer caused.  Gating it
#: behind source health would let a terminal that degraded while its source was
#: down stay degraded forever once the source came back, and AC4b is explicit
#: that an idle terminal must never stick in degraded.  ``pane.missing`` is NOT
#: in the set, and that asymmetry is deliberate: a terminal whose rollout is
#: healthy is demonstrably alive, so a vanished tmux pane is a rendering problem
#: and not a reason to degrade it.  If it really died, ``process.exited`` — which
#: IS in the set — says so two probe ticks later.
DERIVED_ALWAYS_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.PROMPT_AWAITING,
        EventKind.PROMPT_ANSWERED,
        EventKind.USAGE_CAPPED,
        EventKind.PROCESS_EXITED,
        EventKind.PANE_RECOVERED,
    }
)


@runtime_checkable
class SourceRegistry(Protocol):
    """Which terminals have an authoritative event source.

    Deliberately one boolean rather than the :class:`~core.ports.EventSource`
    object: the projector needs to know that a source EXISTS, and reads whether
    it is HEALTHY from the ``last_source_probe_at`` column the tailer bumps.
    Handing it the live source object would invite it to ask the adapter
    directly, which is the coupling the ports exist to prevent.
    """

    def is_authoritative(self, terminal_id: str) -> bool: ...


class NullSourceRegistry:
    """No terminal has an authoritative source.

    The correct default for phase 1: with ``CAO_WORKER_TRUTH_INGEST`` off there
    are no tailers at all, and a registry that claimed otherwise would make the
    projector ignore the only producer actually running.
    """

    def is_authoritative(self, terminal_id: str) -> bool:
        return False


class StaticSourceRegistry:
    """A fixed set of terminals with authoritative sources.

    What ``bootstrap.py`` wires from the adapters that declare
    ``EventSource.is_authoritative``, and what tests use to model a codex
    terminal without running a tailer.
    """

    def __init__(self, terminal_ids: frozenset[str] = frozenset()) -> None:
        self._terminal_ids = set(terminal_ids)

    def add(self, terminal_id: str) -> None:
        self._terminal_ids.add(terminal_id)

    def discard(self, terminal_id: str) -> None:
        self._terminal_ids.discard(terminal_id)

    def is_authoritative(self, terminal_id: str) -> bool:
        return terminal_id in self._terminal_ids


@dataclass(frozen=True)
class ShadowState:
    """One ``worker_state_shadow`` row, satisfying :class:`~core.ports.StateProjection`.

    Frozen, and every rule below produces a new one with
    :func:`dataclasses.replace`.  A projector that mutated a row in place would
    make "what changed in this step" unanswerable, and the whole package exists
    to make that question answerable.

    Column order matches AC6's list exactly.
    """

    terminal_id: str
    state: WorkerState = WorkerState.STARTING
    since: datetime | None = None
    last_event_seq: int = 0
    degraded_reason: DegradedReason | None = None
    prior_state: WorkerState | None = None
    last_probe_at: datetime | None = None
    last_source_probe_at: datetime | None = None
    pane_pid: int | None = None
    pane_present: bool = False
    miss_count: int = 0

    @classmethod
    def from_projection(cls, projection: StateProjection) -> "ShadowState":
        """Adopt whatever concrete row the store handed back.

        The store's own type is its business — the port is structural — so the
        projector copies the fields it knows about and works on its own value.
        """
        return cls(
            terminal_id=projection.terminal_id,
            state=projection.state,
            since=projection.since,
            last_event_seq=projection.last_event_seq,
            degraded_reason=projection.degraded_reason,
            prior_state=projection.prior_state,
            last_probe_at=projection.last_probe_at,
            last_source_probe_at=projection.last_source_probe_at,
            pane_pid=projection.pane_pid,
            pane_present=projection.pane_present,
            miss_count=projection.miss_count,
        )


@dataclass(frozen=True)
class ProjectionOutcome:
    """What one :meth:`Projector.project` call decided, and under which rule.

    ``rule`` is a stable identifier, not prose: it goes into the
    ``status.transition`` payload, ``cao diag`` prints it, and the tests assert
    on it.  Naming the rule is what turns "the state flipped" into "the state
    flipped because a derived event applied while the source was silent".
    """

    terminal_id: str
    rule: str
    applied: bool
    from_state: WorkerState | None = None
    to_state: WorkerState | None = None
    classification: TransitionClass | None = None
    decision_event_id: str | None = None


class Projector:
    """Folds worker events into ``worker_state_shadow`` (AC6)."""

    def __init__(
        self,
        events: EventStore,
        states: StateStore,
        clock: Clock,
        sources: SourceRegistry | None = None,
        legacy_check: Callable[[str], bool] | None = None,
    ) -> None:
        self._events = events
        self._states = states
        self._clock = clock
        self._sources = sources if sources is not None else NullSourceRegistry()
        # ``DIAG-LEGACY-DISAGREE`` is durational, so it cannot ride the store's
        # on-append registry: during an append the projection is one event stale
        # by construction.  The projector drives it instead, after the fold.
        # Optional, because a projector must remain runnable without it.
        self._legacy_check = legacy_check if legacy_check is not None else _no_check

    # -------------------------------------------------------------------- apply

    def project(self, event: WorkerEvent) -> ProjectionOutcome:
        """Fold one appended event into the projection.

        The event is already stored — the projector never appends worker truth,
        only the ``status.*`` decision rows its own rules produce.
        """
        if event.decision is not None:
            # A server decision records what the server DID.  It never moves the
            # projection, or the projector's own transition rows would feed back
            # into it.
            return ProjectionOutcome(event.terminal_id, rule="decision_row", applied=False)

        row = self._load(event.terminal_id, event.ingested_at)

        if self._is_muted(row, event):
            self._states.upsert(replace(row, last_event_seq=event.seq))
            return ProjectionOutcome(
                event.terminal_id,
                rule="derived_muted_by_healthy_source",
                applied=False,
                from_state=row.state,
            )

        if event.kind is EventKind.PANE_RECOVERED:
            outcome = self._recover(row, event)
        else:
            outcome = self._transition(row, event)

        self._legacy_check(event.terminal_id)
        return outcome

    def _load(self, terminal_id: str, at: datetime) -> ShadowState:
        existing = self._states.get(terminal_id)
        if existing is not None:
            return ShadowState.from_projection(existing)
        # A terminal the projector has never seen starts in ``starting``, which
        # every state is reachable from.  ``since`` is the server clock of the
        # first event rather than its ``observed_at``: the whole projection is
        # ordered by ``ingested_at`` (audit §3.1), and mixing the two orderings
        # in one row is how a "negative duration" bug is born.
        return ShadowState(terminal_id=terminal_id, since=at)

    def _is_muted(self, row: ShadowState, event: WorkerEvent) -> bool:
        """Source-level precedence: is this derived event logged but not applied?"""
        if event.confidence is not Confidence.DERIVED:
            return False
        if event.kind in DERIVED_ALWAYS_KINDS:
            return False
        if not self._sources.is_authoritative(event.terminal_id):
            return False
        return self._source_healthy(row)

    def _source_healthy(self, row: ShadowState) -> bool:
        """A source is healthy while its tailer stat-ed the file within ``NO_SIGNAL_S``.

        A source that has never probed is NOT healthy.  That direction matters:
        the alternative — treating "no probe yet" as healthy — would mute the
        pane fallback for a terminal whose tailer failed to start, which is the
        exact moment the fallback is most needed.
        """
        if row.last_source_probe_at is None:
            return False
        return self._clock.now() - row.last_source_probe_at <= timedelta(seconds=NO_SIGNAL_S)

    # -------------------------------------------------------------------- rules

    def _transition(self, row: ShadowState, event: WorkerEvent) -> ProjectionOutcome:
        target, reason = self._target(event)
        if target is None:
            # A boundary event that asserts no state — a tool result on a
            # provider we do not map, an unrecognised legacy status.  It is
            # still worth its row in the log; it just moves nothing.
            self._states.upsert(replace(row, last_event_seq=event.seq))
            return ProjectionOutcome(
                event.terminal_id,
                rule="no_implied_state",
                applied=False,
                from_state=row.state,
            )

        classification = validate(row.state, target)

        if classification is TransitionClass.NO_OP:
            return self._diagonal(row, event, target, reason)

        prior = row.state if target is WorkerState.DEGRADED else None
        updated = replace(
            row,
            state=target,
            since=event.ingested_at,
            last_event_seq=event.seq,
            degraded_reason=reason if target is WorkerState.DEGRADED else None,
            prior_state=prior,
        )
        self._states.upsert(updated)
        decision_id = self._append_decision(
            DecisionKind.STATUS_TRANSITION,
            event,
            payload={
                "from": row.state.value,
                "to": target.value,
                "rule": "applied",
                "classification": classification.value,
                "degraded_reason": reason.value if reason is not None else None,
            },
        )
        return ProjectionOutcome(
            event.terminal_id,
            rule="applied",
            applied=True,
            from_state=row.state,
            to_state=target,
            classification=classification,
            decision_event_id=decision_id,
        )

    def _diagonal(
        self,
        row: ShadowState,
        event: WorkerEvent,
        target: WorkerState,
        reason: DegradedReason | None,
    ) -> ProjectionOutcome:
        """Same-state re-entry: keep ``since``, advance the seq, write no row.

        The one exception is a degraded terminal whose reason RISES.  The order
        is fixed in ``core.states`` and its direction is the point: a terminal
        already degraded for ``producer_error`` must not be relabelled
        ``no_signal`` just because the sweep also noticed the silence.
        """
        if (
            target is WorkerState.DEGRADED
            and reason is not None
            and reason_rises(row.degraded_reason, reason)
        ):
            previous = row.degraded_reason
            self._states.upsert(
                replace(row, degraded_reason=reason, last_event_seq=event.seq)
            )
            decision_id = self._append_decision(
                DecisionKind.STATUS_REASON_CHANGED,
                event,
                payload={
                    "from_reason": previous.value if previous is not None else None,
                    "to_reason": reason.value,
                    "rule": "reason_rise",
                },
            )
            return ProjectionOutcome(
                event.terminal_id,
                rule="reason_rise",
                applied=True,
                from_state=row.state,
                to_state=target,
                classification=TransitionClass.NO_OP,
                decision_event_id=decision_id,
            )

        self._states.upsert(replace(row, last_event_seq=event.seq))
        return ProjectionOutcome(
            event.terminal_id,
            rule="diagonal_noop",
            applied=False,
            from_state=row.state,
            to_state=target,
            classification=TransitionClass.NO_OP,
        )

    def _recover(self, row: ShadowState, event: WorkerEvent) -> ProjectionOutcome:
        """``pane.recovered``: restore ``prior_state`` (AC6 rule (b)).

        A no-op on a terminal that is not degraded.  The probe fires
        ``pane.recovered`` whenever a successful probe lists a pane it had not
        previously confirmed, which includes plenty of terminals that were never
        degraded at all; treating that as a state change would let a probe tick
        knock a busy terminal back to idle.
        """
        if row.state is not WorkerState.DEGRADED:
            self._states.upsert(replace(row, last_event_seq=event.seq))
            return ProjectionOutcome(
                event.terminal_id,
                rule="recovery_not_degraded",
                applied=False,
                from_state=row.state,
            )

        target = row.prior_state if row.prior_state is not None else WorkerState.IDLE
        classification = validate(row.state, target)
        updated = replace(
            row,
            state=target,
            since=event.ingested_at,
            last_event_seq=event.seq,
            degraded_reason=None,
            prior_state=None,
        )
        self._states.upsert(updated)
        decision_id = self._append_decision(
            DecisionKind.STATUS_RECOVERED,
            event,
            payload={
                "from": row.state.value,
                "to": target.value,
                "rule": "recovered",
                "classification": classification.value,
                "restored_reason": (
                    row.degraded_reason.value if row.degraded_reason is not None else None
                ),
            },
        )
        return ProjectionOutcome(
            event.terminal_id,
            rule="recovered",
            applied=True,
            from_state=row.state,
            to_state=target,
            classification=classification,
            decision_event_id=decision_id,
        )

    def _target(self, event: WorkerEvent) -> tuple[WorkerState | None, DegradedReason | None]:
        """The state this event asserts, and the reason if that state is degraded."""
        if event.kind is EventKind.STATUS_LEGACY_PUBLISHED:
            raw = event.payload.get("latched_status")
            if not isinstance(raw, str):
                return None, None
            target = legacy_state(raw)
            # The legacy pair ``unknown``/``render_uncertain`` is exactly what
            # ``degraded`` replaced, so that is the reason it carries.
            reason = DegradedReason.RENDER_UNCERTAIN if target is WorkerState.DEGRADED else None
            return target, reason

        target = implied_state(event.kind)
        if target is not WorkerState.DEGRADED:
            return target, None
        # A producer that KNOWS why it is degrading a terminal says so in the
        # payload, and that wins.  The probe needs this: after
        # ``PROBE_FAIL_TICKS`` failures it degrades the fleet for
        # ``producer_error``, which is a strictly stronger statement than the
        # ``pane_unreadable`` a single missing pane implies, and a projector that
        # hard-coded the reason by kind would silently downgrade it.
        override = self._payload_reason(event)
        if override is not None:
            return target, override
        if event.kind is EventKind.PANE_MISSING:
            return target, PANE_MISSING_REASON
        return target, DegradedReason.RENDER_UNCERTAIN

    @staticmethod
    def _payload_reason(event: WorkerEvent) -> DegradedReason | None:
        raw = event.payload.get("degraded_reason")
        if not isinstance(raw, str):
            return None
        try:
            return DegradedReason(raw)
        except ValueError:
            return None

    # -------------------------------------------------------------------- sweep

    def sweep(self) -> list[ProjectionOutcome]:
        """Degrade terminals whose source AND probe have both gone silent.

        Runs every ``PANE_HEARTBEAT_S``.  ``degraded(no_signal)`` has no other
        producer: silence is not an event, and a projector that only runs on
        arrival can never observe it.

        Two guards keep the sweep from inventing degradations:

        * An ``exited`` terminal is silent by definition and is skipped.
        * A terminal with NO events is skipped.  ``no_signal`` means "we heard
          something and then stopped"; a projection row that exists only because
          a probe touched its liveness columns has never spoken, and degrading it
          would additionally produce a transition row with nothing to cite, which
          ``DIAG-GHOST-TRANSITION`` would then correctly complain about.
        """
        now = self._clock.now()
        horizon = timedelta(seconds=NO_SIGNAL_S)
        outcomes: list[ProjectionOutcome] = []

        for projection in self._states.all_terminals():
            row = ShadowState.from_projection(projection)
            if row.state is WorkerState.EXITED:
                continue
            last_signal = self._last_signal(row)
            if last_signal is None or now - last_signal <= horizon:
                continue
            last_event = self._last_event(row.terminal_id)
            if last_event is None:
                continue
            outcomes.append(self._degrade_no_signal(row, last_event, now))

        # The durational check runs for EVERY terminal, not only the ones this
        # pass degraded.  ``DIAG-LEGACY-DISAGREE`` is defined by how long a
        # disagreement has lasted, and a worker that has stalled with the two
        # sides apart is precisely the case where no further event will arrive to
        # trigger the check on append.  Missing that is missing the bug.
        for projection in self._states.all_terminals():
            self._legacy_check(projection.terminal_id)
        return outcomes

    @staticmethod
    def _last_signal(row: ShadowState) -> datetime | None:
        """The most recent moment anything was heard about this terminal.

        ``since`` participates as a floor so a projection created moments ago
        cannot be judged silent before its first probe has had a chance to land.
        """
        candidates = [
            stamp
            for stamp in (row.last_probe_at, row.last_source_probe_at, row.since)
            if stamp is not None
        ]
        return max(candidates) if candidates else None

    def _last_event(self, terminal_id: str) -> WorkerEvent | None:
        """The last WORKER TRUTH row for a terminal, decisions excluded.

        The exclusion is the whole point.  The projector's own
        ``status.transition`` rows are the newest thing in the log for any
        terminal it has just touched, so citing "the last row" would make the
        sweep's evidence point at the projector rather than at the last thing
        anyone actually heard from the worker — a self-referential chain that
        ``cao diag --why`` would walk in circles.
        """
        rows = [row for row in self._events.read(terminal_id) if row.decision is None]
        return rows[-1] if rows else None

    def _degrade_no_signal(
        self, row: ShadowState, last_event: WorkerEvent, now: datetime
    ) -> ProjectionOutcome:
        target = WorkerState.DEGRADED
        reason = DegradedReason.NO_SIGNAL

        if row.state is WorkerState.DEGRADED:
            # ``no_signal`` is the LOWEST rank, so this branch only ever
            # re-confirms an existing degradation.  It is written as a rise check
            # rather than as an early return so the ordering stays the single
            # source of truth for which reason wins.
            if not reason_rises(row.degraded_reason, reason):
                self._states.upsert(row)
                return ProjectionOutcome(
                    row.terminal_id,
                    rule="no_signal_already_degraded",
                    applied=False,
                    from_state=row.state,
                    to_state=target,
                    classification=TransitionClass.NO_OP,
                )

        classification = validate(row.state, target)
        self._states.upsert(
            replace(
                row,
                state=target,
                since=now,
                degraded_reason=reason,
                prior_state=row.state,
            )
        )
        # The evidence for "nothing has been heard since" is the last thing that
        # WAS heard.  It is a real join, not a placeholder: ``cao diag --why``
        # lands on the final event before the silence began.
        decision_id = self._append_decision(
            DecisionKind.STATUS_TRANSITION,
            last_event,
            payload={
                "from": row.state.value,
                "to": target.value,
                "rule": "no_signal_sweep",
                "classification": classification.value,
                "degraded_reason": reason.value,
                "silent_since": last_event.ingested_at.isoformat(),
            },
            observed_at=now,
        )
        return ProjectionOutcome(
            row.terminal_id,
            rule="no_signal_sweep",
            applied=True,
            from_state=row.state,
            to_state=target,
            classification=classification,
            decision_event_id=decision_id,
        )

    # ------------------------------------------------------------------ helpers

    def _append_decision(
        self,
        decision: DecisionKind,
        cause: WorkerEvent,
        *,
        payload: dict[str, object],
        observed_at: datetime | None = None,
    ) -> str:
        """Append one server decision row naming the event that justified it.

        ``evidence`` is never optional here.  A decision row with no evidence is
        the ``DIAG-GHOST-TRANSITION`` finding, and the mutant that drops this
        argument is one the empirical gate must kill.

        ``run_id`` and ``msg_id`` are carried over from the causing event so the
        correlation-id family (audit §4.1) survives the hop from worker truth to
        server decision; without that, a transition caused by a dispatched
        message could not be joined back to the dispatch.
        """
        stored = self._events.append(
            EventDraft(
                terminal_id=cause.terminal_id,
                kind=decision,
                producer=Producer.SERVER,
                confidence=Confidence.AUTHORITATIVE,
                observed_at=observed_at if observed_at is not None else self._clock.now(),
                payload=dict(payload),
                run_id=cause.run_id,
                msg_id=cause.msg_id,
                decision=decision,
                evidence=cause.event_id,
            )
        )
        return stored.event_id
