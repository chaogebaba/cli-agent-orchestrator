"""Fold worker events into the worker-state projection (WP-ARCH phase 1, AC6).

The projector is the only writer of ``worker_state_shadow`` and, in phase 1, the
projection has no readers in ``services/``.  That is what makes AC11's "no
behaviour change with the switch ON" true by construction rather than by
assertion: the projector can be wrong for a whole session and nothing downstream
notices.  What surfaces it is the ``DIAG-LEGACY-DISAGREE`` check and, for a flag
flip, a grok-box live round (#738) — which is the whole acceptance now that the
AC10 agreement report has gone with shadow-live mode.

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
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from cli_agent_orchestrator.app.worker_truth.health import NullSourceHealth, SourceHealthWriter
from cli_agent_orchestrator.app.worker_truth.mapping import (
    PANE_MISSING_REASON,
    answered_state,
    implied_state,
    legacy_state,
)
from cli_agent_orchestrator.app.worker_truth.publisher import PublishTransition
from cli_agent_orchestrator.core.events import (
    AnyKind,
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

__all__ = [
    "DERIVED_ALWAYS_KINDS",
    "NullSourceRegistry",
    "ProjectedState",
    "ProjectionOutcome",
    "Projector",
    "SourceRegistry",
    "StaticSourceRegistry",
]

logger = logging.getLogger(__name__)


def _no_check(terminal_id: str) -> bool:
    """The default durational check: none."""
    return False


def _no_producer_check(event: "WorkerEvent", row: "ProjectedState") -> bool:
    """The default producer-disagreement check: none."""
    return False


def _no_publish(
    terminal_id: str,
    state: WorkerState,
    *,
    causing_kind: "AnyKind | None",
    degraded_reason: DegradedReason | None,
    event_id: str | None,
    since: datetime,
) -> bool:
    """The default publisher: none.  The projection moves and nobody reads it,
    which is every arm before the cutover and the ``off`` position after it."""
    return False


#: ``worker_state_shadow.since`` is NOT NULL in the DDL and every projector path
#: sets it, so this default is only ever a placeholder for the instant between
#: constructing a row and filling it in.  A distant past rather than "now" so
#: that a row which somehow escaped with it is obviously wrong in a diag view
#: instead of plausibly recent.
_UNSET_SINCE = datetime(1970, 1, 1, tzinfo=UTC)


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
class _PublishIntent:
    """One decided-but-not-yet-published move.

    The value that lets the decision stay inside the projector's lock while the
    publish happens outside it — see :meth:`Projector._deliver` for why those two
    must not be the same critical section.
    """

    terminal_id: str
    state: WorkerState
    causing_kind: AnyKind | None
    degraded_reason: DegradedReason | None
    event_id: str | None
    since: datetime


@dataclass(frozen=True)
class ProjectedState:
    """One ``worker_state_shadow`` row (the table keeps its phase-1 name),
    satisfying :class:`~core.ports.StateProjection`.

    Frozen, and every rule below produces a NEW instance with
    :func:`dataclasses.replace`.  A projector that edited a row in place would
    make "what changed in this step" unanswerable, and the whole package exists
    to make that question answerable — so the compiler enforces it rather than
    the convention asking nicely.

    Freezing became possible at lane A's ``805e0cd7``, which made
    ``StateProjection``'s members read-only properties.  Under the previous plain
    annotations mypy read every member as settable, and a frozen dataclass could
    not satisfy the Protocol.

    Column order matches AC6's list exactly.
    """

    terminal_id: str
    state: WorkerState = WorkerState.STARTING
    since: datetime = _UNSET_SINCE
    last_event_seq: int = 0
    degraded_reason: DegradedReason | None = None
    prior_state: WorkerState | None = None
    last_probe_at: datetime | None = None
    last_source_probe_at: datetime | None = None
    pane_pid: int | None = None
    pane_present: bool = False
    miss_count: int = 0

    @classmethod
    def from_projection(cls, projection: StateProjection) -> "ProjectedState":
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
        health: SourceHealthWriter | None = None,
        producer_check: Callable[[WorkerEvent, ProjectedState], bool] | None = None,
        publisher: PublishTransition | None = None,
    ) -> None:
        self._events = events
        self._states = states
        self._clock = clock
        self._sources = sources if sources is not None else NullSourceRegistry()
        # One writer at a time.  Folds arrive on whatever thread called ``emit``
        # — the status monitor's detection threads, a tailer's thread, the
        # liveness probe's executor thread — and since sub-phase 2b the sweep runs
        # on an executor thread of its own, every ``PANE_HEARTBEAT_S``.  Both
        # paths are read-modify-write over a row the store replaces WHOLE
        # (``upsert`` is an unconditional ``INSERT … ON CONFLICT DO UPDATE`` of
        # every column, with no version guard) and both hold their row across an
        # event-log read, so without this lock the sweep's stale snapshot can
        # overwrite a transition that landed while it was reading: the arriving
        # state is lost, ``last_event_seq`` goes BACKWARDS, and the log gains a
        # transition row describing a move the worker never made — which the
        # ghost- and pane-disagreement checks then report as real.  The event that
        # races the sweep is precisely the event that ends the silence, so the
        # window is the interesting case rather than a rare one.
        #
        # Re-entrant because a decision append inside the critical section reaches
        # the store's ``CheckRunner``; nothing there folds today, and an RLock
        # means nothing there ever deadlocks if something one day does.
        #
        # NOTHING that reaches the legacy status monitor may run while this is
        # held — see :meth:`_deliver`.  The monitor's own lock is taken before
        # this one on the fold's usual path, so taking them the other way round
        # anywhere else is the deadlock that stops every status read.
        self._lock = threading.RLock()
        # D1e's gate, written here and read by the legacy status monitor.  A
        # projector with no view still folds: the view is what the CUTOVER needs,
        # and a projector must remain runnable without the thing that consumes
        # it — which is also the shape of every phase before the cutover lands.
        self._health: SourceHealthWriter = health if health is not None else NullSourceHealth()
        # ``DIAG-LEGACY-DISAGREE`` is durational, so it cannot ride the store's
        # on-append registry: during an append the projection is one event stale
        # by construction.  The projector drives it instead, after the fold.
        # Optional, because a projector must remain runnable without it.
        self._legacy_check = legacy_check if legacy_check is not None else _no_check
        # D9b's ``DIAG-PRODUCER-DISAGREE``.  Optional for the same reason
        # ``legacy_check`` is: a projector must stay runnable without the
        # diagnostics that ride on it.
        self._producer_check = producer_check if producer_check is not None else _no_producer_check
        # D1's publisher.  Absent until the cutover switch resolves ``on``, and
        # absent is the shape of every arm before it: the projection moves and
        # nothing reads it, which is what made phase 1's "no behaviour change"
        # true by construction.
        self._publisher: PublishTransition = publisher if publisher is not None else _no_publish

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

        # THE critical section (phase 2, sub-phase 2b).  Load, decide, upsert:
        # one lock for the whole read-modify-write, held against the sweep, which
        # is the second writer this sub-phase introduced.  See the class
        # docstring's "One writer at a time" note for the interleaving this
        # closes.
        with self._lock:
            row = self._load(event.terminal_id, event.ingested_at)
            # D1e — re-state the gate on EVERY fold, before any rule runs.  The
            # mark is level rather than edge-triggered (see ``health``'s module
            # docstring): an arriving event is the freshest moment the two inputs
            # are both known, and re-stating an unchanged answer costs one
            # dictionary write.
            self._health.mark(event.terminal_id, projected=self._projected(event.terminal_id, row))

            if self._is_muted(row, event):
                self._states.upsert(replace(row, last_event_seq=event.seq))
                outcome = ProjectionOutcome(
                    event.terminal_id,
                    rule="derived_muted_by_healthy_source",
                    applied=False,
                    from_state=row.state,
                )
                # D9b — a muted event that asserts a DIFFERENT state than the one
                # the projection is standing on is two producers disagreeing, and
                # the muted path is the only place both readings are in hand.
                self._producer_check(event, row)
            elif event.kind is EventKind.PANE_RECOVERED:
                outcome = self._recover(row, event)
            else:
                outcome = self._transition(row, event)

            # D1 — what to publish is decided INSIDE the critical section, from
            # the row this fold just wrote.  The publish itself happens outside
            # it; see :meth:`_deliver` for why that separation is not optional.
            intent = self._publish_intent(outcome, causing_kind=event.kind)

            # Run for EVERY event, muted ones included.  A muted
            # ``status.legacy_published`` is exactly where a disagreement begins —
            # the pane said one thing, the healthy source said another — so
            # skipping the check on the muted path would leave the most
            # interesting case to the sweep alone.  It cannot fire spuriously
            # here: the horizon is measured from the latest legacy publish, which
            # at this moment is zero seconds old.
            self._legacy_check(event.terminal_id)

        self._deliver(intent)
        return outcome

    def _load(self, terminal_id: str, at: datetime) -> ProjectedState:
        existing = self._states.get(terminal_id)
        if existing is not None:
            return ProjectedState.from_projection(existing)
        # A terminal the projector has never seen starts in ``starting``, which
        # every state is reachable from.  ``since`` is the server clock of the
        # first event rather than its ``observed_at``: the whole projection is
        # ordered by ``ingested_at`` (audit §3.1), and mixing the two orderings
        # in one row is how a "negative duration" bug is born.
        return ProjectedState(terminal_id=terminal_id, since=at)

    def _is_muted(self, row: ProjectedState, event: WorkerEvent) -> bool:
        """Source-level precedence: is this derived event logged but not applied?"""
        if event.confidence is not Confidence.DERIVED:
            return False
        if event.kind in DERIVED_ALWAYS_KINDS:
            return False
        return self._projected(event.terminal_id, row)

    def _publish_intent(
        self, outcome: ProjectionOutcome, *, causing_kind: AnyKind | None
    ) -> "_PublishIntent | None":
        """What to publish for an APPLIED move.  Caller holds the lock.

        Only an applied move: a diagonal changed nothing to publish, a muted
        event was not applied, and a decision row never reaches here.  The row is
        re-read rather than reconstructed from the outcome, because ``since`` and
        the standing degraded reason are the store's answer and the outcome
        carries neither — and ``since`` is what the fleet row's ``status_since``
        will be read from.  That read is why this half stays inside the lock: it
        must see the row this fold just wrote and no later one.
        """
        if not outcome.applied or outcome.to_state is None:
            return None
        current = self._states.get(outcome.terminal_id)
        if current is None:  # pragma: no cover - the row was just written
            return None
        return _PublishIntent(
            terminal_id=outcome.terminal_id,
            state=outcome.to_state,
            causing_kind=causing_kind,
            degraded_reason=current.degraded_reason,
            event_id=outcome.decision_event_id,
            since=current.since,
        )

    def _deliver(self, intent: "_PublishIntent | None") -> None:
        """Publish, OUTSIDE the projector's lock.  Never holds both.

        The separation is a deadlock fix, not a tidiness one.  The publisher
        reaches the legacy status monitor and takes ITS lock, and the fold is
        most often entered from inside that same lock (a hook on the monitor's
        publish path calls ``emit``).  So the monitor-to-projector direction
        already exists, on the monitor's own thread, where re-entrancy makes it
        safe.  Publishing while holding the projector's lock would add the
        OPPOSITE direction on three other threads — the sweep, the liveness probe
        and the rollout tailer all fold without holding the monitor lock — and
        two threads taking the same two locks in opposite orders is the textbook
        shape.  The status monitor's lock guards ``get_status``, so the deadlock
        would present as the whole server's status reads stopping.

        The cost is that two applied moves for one terminal could in principle
        reach the egress out of order, in the window between releasing the lock
        and publishing.  That is bounded and self-correcting — the next event
        republishes, the observation carries its own sequence, and the receiver
        slot is last-write-wins by construction (D2) — where a deadlock is
        neither.
        """
        if intent is None:
            return
        self._publisher(
            intent.terminal_id,
            intent.state,
            causing_kind=intent.causing_kind,
            degraded_reason=intent.degraded_reason,
            event_id=intent.event_id,
            since=intent.since,
        )

    def _projected(self, terminal_id: str, row: ProjectedState) -> bool:
        """Does the projection own this terminal's status? (D1e.)

        Deliberately the SAME predicate source-level precedence mutes a derived
        event with.  Two spellings of "this terminal has a live authoritative
        source" would eventually disagree, and the disagreement would read as the
        pane path being suppressed for a terminal whose derived events still
        apply — a terminal publishing nothing at all.
        """
        if not self._sources.is_authoritative(terminal_id):
            return False
        return self._source_healthy(row)

    def _source_healthy(self, row: ProjectedState) -> bool:
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

    def _transition(self, row: ProjectedState, event: WorkerEvent) -> ProjectionOutcome:
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
        row: ProjectedState,
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
            self._states.upsert(replace(row, degraded_reason=reason, last_event_seq=event.seq))
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

    def _recover(self, row: ProjectedState, event: WorkerEvent) -> ProjectionOutcome:
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

        if event.kind is EventKind.PROMPT_ANSWERED:
            # WP-ARCH phase 2, D1f — the RESULTING state wins over the kind.
            #
            # ``prompt.answered`` means "the card is gone", which is a statement
            # about the dialog and not about what the worker did next.  The kind
            # alone implies BUSY, and for the provider hooks that raise it that is
            # right: the agent answered and proceeded.  The pane's derived
            # producer sees the other case too — a card DISMISSED, leaving the
            # terminal idle — and it knows which, because it read the screen and
            # put the reading in the payload.  Keying on the kind there would
            # project a dismissed card as a busy worker until the source next
            # spoke, and for a source-healthy terminal nothing would correct it:
            # the pane's own ``status.legacy_published`` is muted by precedence.
            #
            # Absent or unreadable payload falls through to the implied BUSY,
            # which is the hook-produced shape.
            resolved = answered_state(event.payload)
            if resolved is not None:
                return resolved, None

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

        # ``all_terminals`` supplies the ROSTER and nothing else.  Every row it
        # hands back is re-read inside the lock below, because by the time this
        # pass reaches a given terminal its snapshot may be many seconds old — the
        # loop does an event-log read per silent terminal — and acting on the
        # snapshot is precisely how the sweep would clobber a transition that
        # arrived while it was reading (see the lock's note in ``__init__``).
        for terminal_id in [projection.terminal_id for projection in self._states.all_terminals()]:
            intent: _PublishIntent | None = None
            with self._lock:
                current = self._states.get(terminal_id)
                if current is None:
                    # Deleted between the roster read and now.  Nothing to judge,
                    # and its mark goes with it.
                    self._health.forget(terminal_id)
                    continue
                row = ProjectedState.from_projection(current)
                # D1e — re-state the gate for EVERY terminal, before the guards
                # below skip it.  This is the half of the mark that closes I7: a
                # source that died quietly produces no event, so the fold can
                # never lower a standing ``True``, and without this pass a
                # terminal would stay projected for as long as it stayed silent —
                # which is exactly the condition under which the projection has
                # nothing to publish.  An exited terminal is marked by the same
                # rule rather than forced to ``False``: while its source is still
                # warm the projection has a real answer for it — ``error`` — and
                # it is the row the sweep's own rules never revisit, so this pass
                # is its only mark.
                self._health.mark(terminal_id, projected=self._projected(terminal_id, row))
                if row.state is WorkerState.EXITED:
                    continue
                last_signal = self._last_signal(row)
                if last_signal is None or now - last_signal <= horizon:
                    continue
                last_event = self._last_event(terminal_id, row.last_event_seq)
                if last_event is None:
                    continue
                outcome = self._degrade_no_signal(row, last_event, now)
                outcomes.append(outcome)
                # Offered to the publisher like any other applied move, and
                # refused by it: the mark above has just been lowered, because
                # a terminal is only here when its source went silent.  The
                # handover is the point — the pane path is publishing for this
                # terminal again, and a projected ``unknown`` on top of it would
                # be the cutover turning a source outage into a status outage.
                intent = self._publish_intent(outcome, causing_kind=None)
            # OUTSIDE the lock, and this is the path that made it necessary: the
            # sweep runs on its own thread and holds no monitor lock, so
            # publishing from inside the critical section would take the two
            # locks in the opposite order to the fold's own caller.
            self._deliver(intent)

        # The durational check runs for EVERY terminal, not only the ones this
        # pass degraded.  ``DIAG-LEGACY-DISAGREE`` is defined by how long a
        # disagreement has lasted, and a worker that has stalled with the two
        # sides apart is precisely the case where no further event will arrive to
        # trigger the check on append.  Missing that is missing the bug.
        for projection in self._states.all_terminals():
            with self._lock:
                self._legacy_check(projection.terminal_id)
        return outcomes

    @staticmethod
    def _last_signal(row: ProjectedState) -> datetime | None:
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

    def _last_event(self, terminal_id: str, last_event_seq: int) -> WorkerEvent | None:
        """The last WORKER TRUTH row for a terminal, decisions excluded.

        The exclusion is the whole point.  The projector's own
        ``status.transition`` rows are the newest thing in the log for any
        terminal it has just touched, so citing "the last row" would make the
        sweep's evidence point at the projector rather than at the last thing
        anyone actually heard from the worker — a self-referential chain that
        ``cao diag --why`` would walk in circles.

        BOUNDED by the projection's own cursor, and that bound is not an
        optimisation detail: an unbounded ``read`` here materialises every row a
        terminal has ever stored — thirty days of retention — into Python, once
        per silent terminal, once per sweep, 4320 times a day.  The cost grows
        with uptime while the answer never does.

        ``last_event_seq`` is the seq of the event that last MOVED this
        projection, so the row being looked for is that one; the window opens one
        seq earlier so a ``read`` whose bound is exclusive still contains it.
        When retention has pruned that row the window is empty and the answer is
        ``None`` — the sweep then skips the terminal, exactly as it does for a
        terminal that has never spoken, which is the right reading: a degradation
        with no surviving evidence to cite is what ``DIAG-GHOST-TRANSITION``
        exists to complain about.
        """
        if last_event_seq <= 0:
            return None
        rows = [
            row
            for row in self._events.read(terminal_id, since_seq=last_event_seq - 1)
            if row.decision is None
        ]
        return rows[-1] if rows else None

    def _degrade_no_signal(
        self, row: ProjectedState, last_event: WorkerEvent, now: datetime
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
