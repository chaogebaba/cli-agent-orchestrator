"""Continuous invariant checks that emit typed findings (WP-ARCH phase 1, AC8).

The audit §4.4 replaces prose watchdog pings with counted, typed rows: a check
runs as rows arrive, and a repeat increments a count rather than firing again.
This module holds both halves — the registry the store is constructed with, and
the three phase-1 checks themselves, which are described where they are defined
further down.

Why the registry lives in ``app`` while the store that calls it lives in
``adapters``: ``adapters-are-leaves`` forbids an adapter from importing ``app``.
The store therefore depends on the ``core.ports.CheckRunner`` Protocol, and
``bootstrap.py`` injects this concrete registry.  The dependency arrow points the
right way and the contract enforces it.

**A check may never break an append.**  Every check runs inside its own
try/except here, and the store wraps the whole call again.  A diagnostic that can
take the event log down would be worse than no diagnostic at all — the log is
what every later phase reads.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from cli_agent_orchestrator.app.worker_truth.mapping import implied_state, legacy_state
from cli_agent_orchestrator.core.events import Confidence, DecisionKind, EventKind, WorkerEvent
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.ports import Clock, EventStore, FindingStore, StateStore
from cli_agent_orchestrator.core.states import TRANSITIONS, TransitionClass, WorkerState
from cli_agent_orchestrator.core.timing import PANE_HEARTBEAT_S

logger = logging.getLogger(__name__)

__all__ = [
    "EVIDENCE_REQUIRING_DECISIONS",
    "TRANSITION_ROW_KINDS",
    "CheckOutcome",
    "CheckRegistry",
    "EventCheck",
    "PaneDisagreementCheck",
    "ProducerDisagreementCheck",
    "bad_transition_check",
    "ghost_transition_check",
    "record_migration_failure",
    "register_phase1_checks",
]


@dataclass(frozen=True)
class CheckOutcome:
    """What a check reports when its invariant is broken.

    ``dedupe_key`` is what makes two breaches "the same problem again" — the
    offending cell for a bad transition, the disagreeing pair for a legacy
    disagreement.  Only the check knows that, so only the check supplies it.
    """

    dedupe_key: str = ""
    detail: str = ""
    terminal_id: str | None = None
    sample_event_id: str | None = None


#: A check inspects one appended event and returns an outcome when its invariant
#: is broken, or ``None`` when it holds.
EventCheck = Callable[[WorkerEvent], CheckOutcome | None]


class CheckRegistry:
    """``core.ports.CheckRunner``: runs every registered check on each append."""

    def __init__(self, finding_store: FindingStore) -> None:
        self._finding_store = finding_store
        self._checks: list[tuple[FindingCode, EventCheck]] = []

    def register(self, code: FindingCode, check: EventCheck) -> None:
        """Register ``check`` to report findings under ``code``."""
        self._checks.append((code, check))

    @property
    def registered_codes(self) -> tuple[FindingCode, ...]:
        return tuple(code for code, _ in self._checks)

    def on_append(self, event: WorkerEvent) -> None:
        """Run every check against ``event``.  Never raises."""
        for code, check in self._checks:
            try:
                outcome = check(event)
            except Exception:  # noqa: BLE001 — one broken check must not stop the others
                logger.warning("worker-truth check %s raised; skipping", code.value, exc_info=True)
                continue
            if outcome is None:
                continue
            try:
                self._finding_store.record(
                    code,
                    terminal_id=(
                        outcome.terminal_id
                        if outcome.terminal_id is not None
                        else event.terminal_id
                    ),
                    dedupe_key=outcome.dedupe_key,
                    detail=outcome.detail,
                    sample_event_id=(
                        outcome.sample_event_id
                        if outcome.sample_event_id is not None
                        else event.event_id
                    ),
                )
            except Exception:  # noqa: BLE001 — recording a finding must not break an append
                logger.warning(
                    "worker-truth finding %s could not be recorded", code.value, exc_info=True
                )


# ---------------------------------------------------------------------------
# The phase-1 checks themselves (AC8).
#
# They split by what they need to look at, and the split is not cosmetic:
#
# * **Structural** — ``DIAG-GHOST-TRANSITION`` and ``DIAG-BAD-TRANSITION`` read
#   nothing but the row that was just appended, so they are plain ``EventCheck``
#   callables in the registry above and run on every append.
# * **Durational** — ``DIAG-PANE-DISAGREE`` (``DIAG-LEGACY-DISAGREE`` before
#   phase 2's D5 repointed it) is defined as a disagreement that has LASTED
#   longer than ``PANE_HEARTBEAT_S``, so no single row can decide it.
#   It reads the projection, which during ``on_append`` is one event stale by
#   construction: the store commits, THEN the projector applies.  A registry
#   check would therefore race the projector and report disagreements that exist
#   only inside that window.  It is a separate callable object, driven by the
#   projector after each fold and by the sweep every heartbeat — the sweep being
#   the half that matters, because a stalled worker is exactly the case where no
#   further row arrives to trigger anything.
# ---------------------------------------------------------------------------

#: Decision kinds whose whole point is to cite the observation that justified
#: them.  A row here with a null ``evidence`` is a ghost: the server acted and
#: nothing in the log explains why.
#:
#: The set is deliberately not "every decision kind".  ``probe.failed`` is a
#: fleet-wide row that touches no terminal and has no prior observation to cite;
#: ``teardown.intended`` records an intent formed before any observation.
#: Demanding evidence from those would fire on every healthy boot, and a check
#: that cries wolf is worse than no check.  ``delivery.attempt`` cites the latest
#: ``status.legacy_published`` for its terminal, which a freshly created terminal
#: may genuinely not have yet, so it is excluded too and its evidence is asserted
#: by AC4c's own tests instead.
EVIDENCE_REQUIRING_DECISIONS: frozenset[DecisionKind] = frozenset(
    {
        DecisionKind.STATUS_TRANSITION,
        DecisionKind.STATUS_REASON_CHANGED,
        DecisionKind.STATUS_RECOVERED,
        DecisionKind.FLEET_OVERRIDE,
    }
)

#: Decision rows that carry a ``from``/``to`` pair and are therefore worth
#: classifying against the transition table.  ``status.reason_changed`` is absent
#: because it is by construction a ``degraded -> degraded`` diagonal, which is
#: never anomalous; it carries reasons, not states.
TRANSITION_ROW_KINDS: frozenset[DecisionKind] = frozenset(
    {DecisionKind.STATUS_TRANSITION, DecisionKind.STATUS_RECOVERED}
)


def ghost_transition_check(event: WorkerEvent) -> CheckOutcome | None:
    """``DIAG-GHOST-TRANSITION``: a decision that cites nothing (audit §4.4).

    ``EventDraft`` already forbids ``evidence`` on worker-truth rows, so this can
    only ever fire on a decision — which is the intent, not a limitation.
    """
    decision = event.decision
    if decision is None or decision not in EVIDENCE_REQUIRING_DECISIONS:
        return None
    # An empty string is as much a ghost as a NULL, and easier to write by
    # accident, so both count.
    if event.evidence:
        return None
    return CheckOutcome(
        dedupe_key=decision.value,
        detail=f"{decision.value} appended with no evidence event_id",
    )


def bad_transition_check(event: WorkerEvent) -> CheckOutcome | None:
    """``DIAG-BAD-TRANSITION``: an anomalous cell was applied (AC1, AC6 rule (d)).

    Read from the appended transition row rather than from a projector callback,
    so the check holds for ANY writer of a transition row, including a future
    replay tool.  The row's own ``classification`` field is ignored and the cell
    re-derived from the table, so a projector that stamped the wrong
    classification is caught rather than believed.

    The classification comes out of ``TRANSITIONS`` rather than through
    ``validate()`` on purpose: ``validate()`` raises on an anomalous cell under
    ``CAO_WORKER_TRUTH_STRICT=1``, and the strict flag exists to make tests fail
    loudly, not to make appends fail.  The table is still consulted, so loosening
    it still kills this check.
    """
    if event.decision not in TRANSITION_ROW_KINDS:
        return None
    from_raw = event.payload.get("from")
    to_raw = event.payload.get("to")
    if not isinstance(from_raw, str) or not isinstance(to_raw, str):
        return None
    try:
        cell = (WorkerState(from_raw), WorkerState(to_raw))
    except ValueError:
        return None
    if TRANSITIONS.get(cell) is not TransitionClass.ANOMALOUS:
        return None
    return CheckOutcome(
        dedupe_key=f"{from_raw}->{to_raw}",
        detail=f"anomalous transition {from_raw} -> {to_raw} applied",
    )


#: How many of a terminal's most recent event rows the durational checks read.
#:
#: Not a duration (§4c is about durations, and this is a row count), and not a
#: tuning knob either: it is the bound that keeps a check that runs per terminal
#: per sweep from growing with uptime.  Wide enough that a pane classification
#: still in play is inside it — the pane classifies on every detection edge, so a
#: row this far back is one the pane has had 256 events to restate — and narrow
#: enough that the read is a constant.
_RECENT_EVENT_WINDOW = 256


class PaneDisagreementCheck:
    """``DIAG-PANE-DISAGREE``: projection ≠ the pane's reading for over a heartbeat.

    Durational, so it reads the projection and the pane's classifications together
    and is driven by the projector after each fold and by the sweep every
    heartbeat.  It is not a registry check: during ``on_append`` the projection is
    one event stale by construction, and a check that raced the projector would
    report disagreements that exist only inside that window.

    **What phase 2's D5 changed, and why it is not a rename.**  Phase 1 compared
    the projection against ``status.legacy_published`` — the row the egress writes
    for what the fleet and the inbox actually consume.  Phase 2's D1 makes the
    projection publish through that same egress, so from that moment the published
    status is *caused by* the projection and the check compares the projection
    with itself: perfect agreement, forever, reported by a check that is still
    running.  The comparison would go quiet rather than break, which is the
    failure mode phase 1's own AC10 was corrected for in the opposite direction.

    So the check reads ``status.pane_classified`` instead (D1c), which the
    classification site appends whether or not the publish happens.  Pointing it
    at "the raw classification the pane path still computes" was the r2 draft and
    is not enough on its own: a computation is not a record, and for a sourced
    terminal nothing was writing one.

    The old code ``DIAG-LEGACY-DISAGREE`` is retained in the enum and never
    raised (D9b), so findings written before the cutover stay readable.
    """

    def __init__(
        self,
        finding_store: FindingStore,
        event_store: EventStore,
        state_store: StateStore,
        clock: Clock,
    ) -> None:
        self._finding_store = finding_store
        self._event_store = event_store
        self._state_store = state_store
        self._clock = clock

    def __call__(self, terminal_id: str) -> bool:
        """Evaluate one terminal.  Returns whether a finding was recorded.

        Never raises: the projector calls this on every fold.
        """
        try:
            return self._evaluate(terminal_id)
        except Exception:  # noqa: BLE001 — a diagnostic must not break the fold
            logger.warning("legacy-disagreement check failed for %s", terminal_id, exc_info=True)
            return False

    def _evaluate(self, terminal_id: str) -> bool:
        projection = self._state_store.get(terminal_id)
        if projection is None:
            return False
        # BOUNDED, and for the same reason the sweep's evidence read is
        # (``Projector._last_event``): this runs for every terminal on every
        # sweep, and an unbounded read materialises a month of rows to answer a
        # question about the newest one.  The window is the last
        # ``_RECENT_EVENT_WINDOW`` rows of the terminal's log; a classification
        # older than that has been followed by that many events without the pane
        # speaking again, which is not the standing disagreement this check is
        # about.
        high_water = self._event_store.high_water(terminal_id)
        rows = self._event_store.read(
            terminal_id,
            since_seq=max(0, high_water - _RECENT_EVENT_WINDOW),
            kinds=frozenset({EventKind.STATUS_PANE_CLASSIFIED}),
        )
        if not rows:
            return False

        raw = rows[-1].payload.get("latched_status")
        if not isinstance(raw, str):
            return False
        mapped = legacy_state(raw)
        if mapped is None or mapped is projection.state:
            return False

        onset, sample = self._onset(rows, mapped, projection.since)
        age = self._clock.now() - onset
        # Ordinary lag is not a finding.  The two sides are fed by producers on
        # different clocks and can never move in the same instant; firing on that
        # would bury the disagreements that actually matter under lag.
        if age <= timedelta(seconds=PANE_HEARTBEAT_S):
            return False

        self._finding_store.record(
            FindingCode.DIAG_PANE_DISAGREE,
            terminal_id=terminal_id,
            dedupe_key=f"{projection.state.value}|{raw}",
            detail=(
                f"projected {projection.state.value} vs pane {raw} "
                f"for {age.total_seconds():.0f}s"
            ),
            sample_event_id=sample.event_id,
        )
        return True

    @staticmethod
    def _onset(
        rows: list[WorkerEvent], mapped: WorkerState, state_since: datetime
    ) -> tuple[datetime, WorkerEvent]:
        """When the CURRENT disagreement began, and the row that opened it.

        Measuring from the LATEST classification would be wrong in the case that
        matters most: a pane re-reading the same wrong status every few seconds
        would reset the clock forever and never fire, which is precisely the
        long-running disagreement worth a finding.  So walk back through the
        consecutive classifications that carry the same mapped state and take the
        earliest.

        The projection bounds it too.  A classification that predates the current
        projected ``state`` was not disagreeing with THIS state, so the onset is
        the later of the two: the run's first classification, or the moment the
        projection arrived where it now is.
        """
        first = rows[-1]
        for row in reversed(rows[:-1]):
            raw = row.payload.get("latched_status")
            if not isinstance(raw, str) or legacy_state(raw) is not mapped:
                break
            first = row
        return max(first.ingested_at, state_since), first


class ProducerDisagreementCheck:
    """``DIAG-PRODUCER-DISAGREE``: two producers, one terminal, two states (D9b).

    The other two disagreement checks compare the projection against a RECORD —
    what the pane published, or what it classified — and they are durational
    because a record sits there while the clocks drift apart.  This one is
    instantaneous and structural, because the moment it is about lasts no time at
    all: source-level precedence has just MUTED a derived event, which means the
    projector was holding two readings of one terminal and chose one.  If the
    reading it discarded asserted a different state, the two producers disagree,
    and nothing downstream will ever say so — the muted event is in the log and
    applied to nothing, which is exactly how it should be and exactly why the
    disagreement is invisible without a finding.

    Why it is not a registry check: ``on_append`` cannot see the mute.  The
    decision to mute is the projector's and is not recorded as a row, so a check
    reading the log alone would have to re-derive source health and would then be
    a second implementation of the precedence rule — free to disagree with the
    first, which is the failure this whole package is about.

    Why it does not fire on lag: a muted event whose implied state MATCHES the
    projection is the normal case and is silent.  Only a contradiction is
    recorded, deduped per ``(projected, asserted)`` pair, so a pane that reads
    ``processing`` against an idle rollout for an hour is one finding with a
    count, not thirty.
    """

    def __init__(self, finding_store: FindingStore) -> None:
        self._finding_store = finding_store

    def __call__(self, event: WorkerEvent, row: "object") -> bool:
        """Evaluate one muted event.  Returns whether a finding was recorded.

        Never raises: the projector calls this from inside its critical section.
        """
        try:
            return self._evaluate(event, row)
        except Exception:  # noqa: BLE001 — a diagnostic must not break the fold
            logger.warning(
                "producer-disagreement check failed for %s", event.terminal_id, exc_info=True
            )
            return False

    def _evaluate(self, event: WorkerEvent, row: "object") -> bool:
        standing = getattr(row, "state", None)
        if not isinstance(standing, WorkerState):
            return False
        if event.confidence is not Confidence.DERIVED:
            # Belt and braces: only a DERIVED event can be muted by precedence,
            # so an authoritative one reaching here would mean the projector's
            # rule changed shape and this check's premise no longer holds.
            return False
        asserted = _asserted_state(event)
        if asserted is None or asserted is standing:
            return False

        self._finding_store.record(
            FindingCode.DIAG_PRODUCER_DISAGREE,
            terminal_id=event.terminal_id,
            dedupe_key=f"{standing.value}|{asserted.value}",
            detail=(
                f"projected {standing.value} from the authoritative source while "
                f"{event.producer.value} asserted {asserted.value} "
                f"({event.kind.value}, muted)"
            ),
            sample_event_id=event.event_id,
        )
        return True


def _asserted_state(event: WorkerEvent) -> WorkerState | None:
    """The state a muted event claims, by whichever route its kind carries one.

    ``status.legacy_published`` carries it in the payload — it is the pane's whole
    reading — and every state-asserting kind carries it in the kind.  Anything
    else (a classification row, a decision) asserts nothing and cannot disagree
    with anything.
    """
    if event.kind is EventKind.STATUS_LEGACY_PUBLISHED:
        raw = event.payload.get("latched_status")
        return legacy_state(raw) if isinstance(raw, str) else None
    return implied_state(event.kind)


def register_phase1_checks(registry: CheckRegistry) -> CheckRegistry:
    """Register the two structural checks.  The durational one is not a registry
    check — see the note above this section for why."""
    registry.register(FindingCode.DIAG_GHOST_TRANSITION, ghost_transition_check)
    registry.register(FindingCode.DIAG_BAD_TRANSITION, bad_transition_check)
    return registry


def record_migration_failure(finding_store: FindingStore, step: str, detail: str) -> None:
    """Write the AC5 ``DIAG-MIGRATION-FAILED`` row.

    Lives here so the four phase-1 finding codes have one home and one shape.
    AC5's ordering is what lets it work at all: the ``finding`` table is created
    FIRST, in its own transaction, so it survives the failure of any later step.
    ``step`` is the dedupe key, so a server that reboots ten times into the same
    broken migration leaves one row with a count of ten, not ten rows.
    """
    finding_store.record(FindingCode.DIAG_MIGRATION_FAILED, dedupe_key=step, detail=detail)
