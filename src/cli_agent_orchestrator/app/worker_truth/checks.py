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
from datetime import timedelta

from cli_agent_orchestrator.core.events import DecisionKind, EventKind, WorkerEvent
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.ports import Clock, EventStore, FindingStore, StateStore
from cli_agent_orchestrator.core.states import TRANSITIONS, TransitionClass, WorkerState
from cli_agent_orchestrator.core.timing import PANE_HEARTBEAT_S

from cli_agent_orchestrator.app.worker_truth.mapping import legacy_state

logger = logging.getLogger(__name__)

__all__ = [
    "EVIDENCE_REQUIRING_DECISIONS",
    "TRANSITION_ROW_KINDS",
    "CheckOutcome",
    "CheckRegistry",
    "EventCheck",
    "LegacyDisagreementCheck",
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
# * **Durational** — ``DIAG-LEGACY-DISAGREE`` is defined as a disagreement that
#   has LASTED longer than ``PANE_HEARTBEAT_S``, so no single row can decide it.
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


class LegacyDisagreementCheck:
    """``DIAG-LEGACY-DISAGREE``: shadow ≠ legacy for longer than one heartbeat."""

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

        The horizon is measured from the legacy publish's ``ingested_at``,
        because that publish is the moment the two sides last had a chance to
        agree.  A disagreement younger than ``PANE_HEARTBEAT_S`` is ordinary lag —
        the two sides are fed by producers on different clocks — and firing on it
        would bury the real disagreements the agreement report (AC10) counts.

        Never raises: the projector calls this on every fold.
        """
        try:
            return self._evaluate(terminal_id)
        except Exception:  # noqa: BLE001 — a diagnostic must not break the fold
            logger.warning(
                "legacy-disagreement check failed for %s", terminal_id, exc_info=True
            )
            return False

    def _evaluate(self, terminal_id: str) -> bool:
        projection = self._state_store.get(terminal_id)
        if projection is None:
            return False
        latest = self._latest_legacy_publish(terminal_id)
        if latest is None:
            return False
        raw = latest.payload.get("latched_status")
        if not isinstance(raw, str):
            return False
        mapped = legacy_state(raw)
        if mapped is None or mapped is projection.state:
            return False
        age = self._clock.now() - latest.ingested_at
        if age <= timedelta(seconds=PANE_HEARTBEAT_S):
            return False
        self._finding_store.record(
            FindingCode.DIAG_LEGACY_DISAGREE,
            terminal_id=terminal_id,
            dedupe_key=f"{projection.state.value}|{raw}",
            detail=(
                f"shadow {projection.state.value} vs legacy {raw} "
                f"for {age.total_seconds():.0f}s"
            ),
            sample_event_id=latest.event_id,
        )
        return True

    def _latest_legacy_publish(self, terminal_id: str) -> WorkerEvent | None:
        rows = self._event_store.read(
            terminal_id, kinds=frozenset({EventKind.STATUS_LEGACY_PUBLISHED})
        )
        return rows[-1] if rows else None


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
