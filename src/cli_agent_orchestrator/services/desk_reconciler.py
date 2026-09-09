"""F875 desk reconciler — the level-triggered controller that keeps one
designated standing secretary desk per live conversation (D2, D6).

A 15-second server timer enumerates the ``conversation_identity`` rows whose
lifecycle is ``'live'`` (fork) and reconciles a missing or unhealthy
``DeskBinding`` through the ordinary certified assignment boundary. It runs
whether or not the supervisor performs a lookup — the property the legacy
spawn-nudge lacks — and drives each binding to READY, BUSY or a typed DEGRADED
within 90 seconds of seat creation or detected loss.

Design seams that keep this testable in shadow mode (R1a):

* Membership comes from ``list_live_conversation_ids()`` — an enumeration of
  ``conversation_identity.lifecycle == 'live'`` rows, NOT observed tool events
  (B3 / AC-18). The reconciler never derives the active set from activity.
* Desk *creation* goes through an injected ``AssignmentBoundary`` callable.
  R1a supplies test doubles; the real certified create path (which depends on
  the F868 #481/#505 routing-composition fix) is wired in R1c. The boundary
  returns a typed ``CreateOutcome`` — never a silently rewritten general
  profile (D6 / AC-12).
* Concurrency is fenced on ``desk_bindings.reconcile_generation`` with a CAS,
  so two reconcilers cannot produce overlapping live incarnations (AC-4).

Nothing here is wired into the live seat create path or the hooks in R1a.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, List, Optional

from sqlalchemy import text

from cli_agent_orchestrator.clients.database import (
    DeskBindingModel,
    SessionLocal,
    _utcnow,
    engine,
)
from cli_agent_orchestrator.services.desk_service import (
    EV_STATE_ENTER,
    record_event,
)

RECONCILE_TICK_SECONDS = 15
READY_BOUND_SECONDS = 90  # seat creation / detected loss -> READY|DEGRADED within this
REPLACEMENT_BACKOFF_SECONDS = 300  # five-minute backoff after the one automatic attempt

# Typed DEGRADED causes (D6). A capped pi_cli NEVER becomes a general or
# other-provider lane; each cause is distinct.
DEGRADED_CAUSES = ("capped", "cert_failed", "fleet_full", "server_down", "init_timeout")


class CreateStatus(str, Enum):
    """Outcome of one certified desk-creation attempt (D6)."""

    READY = "ready"
    CAPPED = "capped"
    CERT_FAILED = "cert_failed"
    FLEET_FULL = "fleet_full"
    SERVER_DOWN = "server_down"
    INIT_TIMEOUT = "init_timeout"


@dataclass(frozen=True)
class CreateOutcome:
    """What the certified assignment boundary produced. ``provider_binding`` and
    ``terminal_ref`` are set only on READY."""

    status: CreateStatus
    provider_binding: Optional[str] = None
    terminal_ref: Optional[str] = None
    retry_after_seconds: Optional[int] = None  # provider retry-after, if any


# The boundary: given a conversation identity + incarnation, attempt to create
# the certified desk. R1a injects doubles; R1c wires the real certified path.
AssignmentBoundary = Callable[[str, str], CreateOutcome]

_CAUSE_BY_STATUS = {
    CreateStatus.CAPPED: "capped",
    CreateStatus.CERT_FAILED: "cert_failed",
    CreateStatus.FLEET_FULL: "fleet_full",
    CreateStatus.SERVER_DOWN: "server_down",
    CreateStatus.INIT_TIMEOUT: "init_timeout",
}


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce a possibly naive datetime (sqlite read-back) to aware UTC so it
    compares safely against ``_utcnow()`` (which is aware under a SimClock)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def list_live_conversation_ids() -> List[str]:
    """Enumerate ``conversation_identity`` rows whose lifecycle is ``'live'``.

    This is how the server learns the active set (B3 / AC-18): an unregistered
    seat is impossible because registration IS the identity root itself, and no
    seat tool call populates the set. Uses a raw read so it does not depend on
    importing the identity ORM model here.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT identity_key FROM conversation_identity "
                "WHERE lifecycle = 'live' ORDER BY identity_key"
            )
        ).fetchall()
    return [r[0] for r in rows]


def _live_generation(conversation_id: str) -> Optional[int]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT generation FROM conversation_identity WHERE identity_key = :k"),
            {"k": conversation_id},
        ).fetchone()
    return int(row[0]) if row is not None else None


def _enter_state(
    db: object,
    binding: DeskBindingModel,
    state: str,
    *,
    cause: Optional[str] = None,
) -> None:
    """Transition a binding, stamping observed_at and recording a deduplicated
    state-enter event keyed by (conversation, incarnation, state@observed)."""
    now = _utcnow()
    binding.state = state
    binding.degraded_cause = cause if state == "DEGRADED" else None
    binding.observed_at = now
    if state == "READY":
        # A new READY closes the failure episode.
        binding.replacement_attempts = 0
        binding.retry_deadline = None
    dedup = f"{binding.incarnation}:{state}:{now.isoformat()}"
    record_event(
        binding.conversation_id,
        str(binding.incarnation),
        EV_STATE_ENTER,
        dedup,
        payload={"state": state, "cause": cause},
        db=db,
    )


def reconcile_once(boundary: AssignmentBoundary, *, now: Optional[datetime] = None) -> int:
    """Run one reconciliation pass over all live conversations.

    Returns the number of bindings acted on. Idempotent: repeated seat
    creation, SessionStart, compaction and restart yield at most one live
    designated desk per conversation, because the binding row is keyed by
    ``conversation_id`` (the identity_key) and every write is fenced on
    ``reconcile_generation`` (AC-4).
    """
    now = now or _utcnow()
    acted = 0
    for conversation_id in list_live_conversation_ids():
        generation = _live_generation(conversation_id)
        if generation is None:
            continue
        if _reconcile_binding(conversation_id, str(generation), boundary, now):
            acted += 1
    return acted


def _reconcile_binding(
    conversation_id: str,
    incarnation: str,
    boundary: AssignmentBoundary,
    now: datetime,
) -> bool:
    """Reconcile a single conversation's binding under a generation-fenced lease.

    The lease: read ``reconcile_generation``, do the work, and CAS the row on
    that same value. If another reconciler advanced it first, our write affects
    zero rows and we abort — so two reconcilers cannot produce overlapping live
    incarnations (AC-4).
    """
    with SessionLocal.begin() as db:
        binding = (
            db.query(DeskBindingModel).filter_by(conversation_id=conversation_id).one_or_none()
        )
        if binding is None:
            # First sight of this live conversation: create the STARTING binding.
            binding = DeskBindingModel(
                conversation_id=conversation_id,
                incarnation=incarnation,
                position="secretary",
                state="STARTING",
                observed_at=now,
                created_at=now,
                queue_depth=0,
                replacement_attempts=0,
                reconcile_generation=0,
            )
            db.add(binding)
            db.flush()

        lease = int(binding.reconcile_generation)

        # A healthy READY/BUSY desk on the current incarnation needs nothing.
        if binding.state in ("READY", "BUSY") and str(binding.incarnation) == incarnation:
            return False

        # DEGRADED: honor the one-automatic-replacement-per-episode rule, then
        # retry-after / five-minute backoff (AC-13). Do not retry on every tick.
        if binding.state == "DEGRADED":
            if binding.retry_deadline is not None and now < _as_aware(binding.retry_deadline):
                return False  # still backing off; no attempt this tick

        # Attempt one certified creation through the boundary (D6).
        outcome = boundary(conversation_id, incarnation)

        # Fence: only write if no other reconciler advanced the lease.
        affected = (
            db.query(DeskBindingModel)
            .filter(
                DeskBindingModel.conversation_id == conversation_id,
                DeskBindingModel.reconcile_generation == lease,
            )
            .update({DeskBindingModel.reconcile_generation: lease + 1})
        )
        if affected == 0:
            # Lost the lease to a concurrent reconciler; abort without touching
            # the row (AC-4). The winner's transition stands.
            return False

        binding.incarnation = incarnation
        if outcome.status == CreateStatus.READY:
            binding.provider_binding = outcome.provider_binding
            binding.terminal_ref = outcome.terminal_ref
            _enter_state(db, binding, "READY")
        else:
            # A typed DEGRADED cause — never a general-profile fallback (AC-12).
            cause = _CAUSE_BY_STATUS[outcome.status]
            binding.replacement_attempts = int(binding.replacement_attempts) + 1
            backoff = (
                outcome.retry_after_seconds
                if outcome.retry_after_seconds is not None
                else REPLACEMENT_BACKOFF_SECONDS
            )
            binding.retry_deadline = now + timedelta(seconds=backoff)
            _enter_state(db, binding, "DEGRADED", cause=cause)
        return True


def within_ready_bound(binding_created_at: datetime, observed_at: datetime) -> bool:
    """True iff the binding reached its READY/DEGRADED decision within the 90 s
    bound of seat creation or detected loss (D2 / AC-5)."""
    return (observed_at - binding_created_at) <= timedelta(seconds=READY_BOUND_SECONDS)


def hibernation_allowed(*, resume_artifact_verified: bool, memory_roundtrip_verified: bool) -> bool:
    """Whether a desk may hibernate to save a slot (D6 / AC-14 / K3).

    Hibernation is NOT promised: it is enabled ONLY after that provider's resume
    artifact AND a memory-retention round trip are BOTH verified. A merely
    *declared* artifact is not enough. Until both hold, the desk stops at
    session end and cold-starts — so this returns False, and there is no code
    path that persists a binding on an unverified artifact.
    """
    return bool(resume_artifact_verified and memory_roundtrip_verified)


def stop_at_session_end(conversation_id: str) -> None:
    """The only session-end behavior available in R1a: the desk STOPS and will
    cold-start next session (no hibernation). Marks the binding STOPPED."""
    with SessionLocal.begin() as db:
        binding = (
            db.query(DeskBindingModel).filter_by(conversation_id=conversation_id).one_or_none()
        )
        if binding is not None:
            binding.state = "STOPPED"
            binding.observed_at = _utcnow()
