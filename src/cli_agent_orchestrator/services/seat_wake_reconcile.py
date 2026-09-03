"""F747 (#604): periodic idle-seat wake reconcile.

Why this exists
---------------
A supervisor seat running in pull mode has exactly one wake channel while the
WS doorbell is off: the client-side ``f213-callback-rewake.sh`` watcher, an
inline hook process armed only on ``Stop`` / ``PostToolUse``. An idle seat
emits neither event, so when that watcher dies -- and it dies unobservably,
without even its own SIGTERM log line -- nothing re-arms it and every later
callback lands in a mailbox with no live poller.

The 2026-09-03 sample (seat ``5561a7d1``, mailbox ``mb_d176ebe0``): the
stop-source watcher armed at 07:15:08Z, polled every 5 s until 07:19:15Z, then
vanished. Ids 4153-4162 arrived 07:27-07:38Z and sat unsurfaced for 35 min
until the user typed. The server-side nets that would otherwise have caught
this were all disabled in that deployment (``supervisor.mailbox_pull``,
``supervisor.teammate_push`` and ``supervisor.wake.native`` were false), so
``inbox_service.reconcile_pull_mode_notifications`` returned at its first gate.

This module is the net that does not depend on those flags. It sweeps every
supervisor mailbox on a fixed interval and emits AT MOST ONE wake per batch
per mailbox, keyed on ``mailboxes.wake_notified_id`` so an id that has already
been announced is never announced again -- that is F713 (#568)'s dedup key and
its opposite failure mode.

Contract
--------
* One wake per reconcile batch, never per row.
* ``wake_notified_id`` only ever advances, and only after a successful push.
  A failed push leaves it alone so the next tick retries the same batch.
* Rows at or below ``wake_notified_id`` are already announced: silent.
* Rows at or below ``consumed_through_id`` are already drained: silent.
* Every decision that had pending rows is logged at INFO with the seat id.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Config path for the kill switch. Default ON -- this is a safety net, and a
#: net that ships off is a net that is not there when the sample recurs.
CONFIG_ENABLED = "delivery.seat_wake_reconcile"
CONFIG_INTERVAL_S = "delivery.seat_wake_reconcile_interval_s"
CONFIG_GRACE_S = "delivery.seat_wake_reconcile_grace_s"

DEFAULT_ENABLED = True
DEFAULT_INTERVAL_S = 60.0
#: A row younger than the grace is still the fast paths' to deliver. The
#: reconcile only adopts what they have already missed.
DEFAULT_GRACE_S = 90.0

#: Upper bound on rows pulled into one batch. The wake carries ids, not bodies,
#: so this only bounds the query.
BATCH_LIMIT = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SeatWakeDecision:
    """One mailbox's outcome for one reconcile tick.

    ``outcome`` is a closed set:

    ``no_terminal``      mailbox has no current terminal to wake
    ``no_pending``       nothing unsurfaced past the grace window
    ``already_notified`` highest unsurfaced id was already announced
    ``woken``            one wake emitted, ``wake_notified_id`` advanced
    ``push_failed``      emission failed; cursor left alone for the next tick
    """

    mailbox_id: str
    terminal_id: str
    outcome: str
    max_id: int = 0
    row_count: int = 0
    previous_notified_id: int = 0
    reason: str = ""


def _enabled() -> bool:
    from cli_agent_orchestrator.services.config_service import ConfigService

    return bool(ConfigService.get(CONFIG_ENABLED, default=DEFAULT_ENABLED))


def _interval_s() -> float:
    from cli_agent_orchestrator.services.config_service import ConfigService

    try:
        value = float(ConfigService.get(CONFIG_INTERVAL_S, default=DEFAULT_INTERVAL_S))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    # A zero or negative interval would spin the loop; clamp to something sane.
    return value if value >= 1.0 else DEFAULT_INTERVAL_S


def _grace_s() -> float:
    from cli_agent_orchestrator.services.config_service import ConfigService

    try:
        value = float(ConfigService.get(CONFIG_GRACE_S, default=DEFAULT_GRACE_S))
    except (TypeError, ValueError):
        return DEFAULT_GRACE_S
    return value if value >= 0.0 else DEFAULT_GRACE_S


def _advance_wake_cursor(mailbox_id: str, previous_id: int, new_id: int) -> bool:
    """Move ``wake_notified_id`` forward, but only from the value we observed.

    The compare-and-set on ``previous_id`` means a concurrent claim that
    already advanced the cursor wins and this reconcile does not rewind it.

    ``wake_notified_at`` is deliberately NOT stamped. That column is F476's
    300 s claim lease, and ``claim_unnotified_wake`` refuses a claim when the
    lease is fresh AND ``claimed_high_water <= wake_notified_id``
    (``clients/database.py:11616``). Stamping it here would satisfy both halves
    at once, so the seat we just woke would call in and be handed
    ``kind="lease_held"`` with zero rows for the next five minutes -- woken with
    nothing to drain, which is the failure this reconcile exists to prevent.
    Advancing the id alone leaves the lease stale, so the seat's own claim
    proceeds and stamps the lease properly.
    """
    from cli_agent_orchestrator.clients.database import MailboxModel, SessionLocal

    with SessionLocal() as db:
        changed = (
            db.query(MailboxModel)
            .filter(
                MailboxModel.id == mailbox_id,
                MailboxModel.wake_notified_id == previous_id,
            )
            .update(
                {MailboxModel.wake_notified_id: new_id},
                synchronize_session=False,
            )
        )
        db.commit()
    return bool(changed)


def _reconcile_one(mailbox: Any, cutoff: datetime) -> SeatWakeDecision:
    """Reconcile a single supervisor mailbox. Never raises for data reasons."""
    from cli_agent_orchestrator.clients.database import InboxModel, SessionLocal, TerminalModel
    from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
    from cli_agent_orchestrator.services.teammate_push_service import (
        attempt_teammate_push_reported,
    )

    mailbox_id = str(mailbox.id)
    terminal_id = str(mailbox.current_terminal_id or "")
    consumed_through = int(mailbox.consumed_through_id or 0)
    previous_notified = int(mailbox.wake_notified_id or 0)

    if not terminal_id:
        return SeatWakeDecision(
            mailbox_id=mailbox_id,
            terminal_id="",
            outcome="no_terminal",
            previous_notified_id=previous_notified,
            reason="mailbox has no current terminal",
        )

    with SessionLocal() as db:
        terminal_row = db.query(TerminalModel).filter_by(id=terminal_id).one_or_none()
        if terminal_row is None:
            return SeatWakeDecision(
                mailbox_id=mailbox_id,
                terminal_id=terminal_id,
                outcome="no_terminal",
                previous_notified_id=previous_notified,
                reason="no terminals row",
            )

        rows = (
            db.query(InboxModel)
            .filter(
                InboxModel.logical_receiver_id == mailbox_id,
                InboxModel.status == MessageStatus.PENDING.value,
                InboxModel.id > consumed_through,
                InboxModel.created_at < cutoff,
            )
            .order_by(InboxModel.id.asc())
            .limit(BATCH_LIMIT)
            .all()
        )
        # Materialise inside the session: logical_receiver_id is deferred and
        # raises DetachedInstanceError once the session closes.
        scalars: list[dict[str, Any]] = [
            {
                "id": int(row.id),
                "sender_id": str(row.sender_id),
                "receiver_id": str(row.receiver_id),
                "message": row.message,
                "orchestration_type": row.orchestration_type,
                "status": row.status,
                "created_at": row.created_at,
                "logical_receiver_id": getattr(row, "logical_receiver_id", None),
            }
            for row in rows
        ]

    if not scalars:
        return SeatWakeDecision(
            mailbox_id=mailbox_id,
            terminal_id=terminal_id,
            outcome="no_pending",
            previous_notified_id=previous_notified,
        )

    max_id = max(int(s["id"]) for s in scalars)

    # F713 (#568) dedup key: an id at or below the wake cursor has already been
    # announced. Re-announcing it is the opposite failure and is never correct.
    if max_id <= previous_notified:
        return SeatWakeDecision(
            mailbox_id=mailbox_id,
            terminal_id=terminal_id,
            outcome="already_notified",
            max_id=max_id,
            row_count=len(scalars),
            previous_notified_id=previous_notified,
            reason="max id at or below wake cursor",
        )

    # Announce only the ids the seat has not been told about yet, so the wake
    # names the new work rather than replaying the whole backlog.
    fresh = [s for s in scalars if int(s["id"]) > previous_notified]
    messages = [
        InboxMessage(
            id=int(s["id"]),
            sender_id=s["sender_id"],
            receiver_id=s["receiver_id"],
            message=s["message"],
            orchestration_type=OrchestrationType(s["orchestration_type"]),
            status=MessageStatus(s["status"]),
            created_at=s["created_at"],
            logical_receiver_id=s["logical_receiver_id"],
        )
        for s in fresh
    ]

    outcome = attempt_teammate_push_reported(terminal_id, messages, mailbox_id=mailbox_id)
    if not outcome.pushed:
        return SeatWakeDecision(
            mailbox_id=mailbox_id,
            terminal_id=terminal_id,
            outcome="push_failed",
            max_id=max_id,
            row_count=len(fresh),
            previous_notified_id=previous_notified,
            reason=outcome.reason,
        )

    advanced = _advance_wake_cursor(mailbox_id, previous_notified, max_id)
    return SeatWakeDecision(
        mailbox_id=mailbox_id,
        terminal_id=terminal_id,
        outcome="woken",
        max_id=max_id,
        row_count=len(fresh),
        previous_notified_id=previous_notified,
        reason="cursor advanced" if advanced else "cursor advanced concurrently",
    )


def reconcile_seat_wakes(*, now: Optional[datetime] = None) -> list[SeatWakeDecision]:
    """Sweep every supervisor mailbox once and emit at most one wake each.

    Returns the per-mailbox decisions in mailbox-id order. A fault on one
    mailbox is isolated: it is logged and the sweep continues.
    """
    if not _enabled():
        return []

    from cli_agent_orchestrator.clients.database import MailboxModel, SessionLocal

    cutoff = (now or _utcnow()) - timedelta(seconds=_grace_s())

    with SessionLocal() as db:
        mailboxes = (
            db.query(MailboxModel)
            .filter_by(role="supervisor")
            .order_by(MailboxModel.id.asc())
            .all()
        )
        snapshots: list[dict[str, Any]] = [
            {
                "id": mb.id,
                "current_terminal_id": mb.current_terminal_id,
                "consumed_through_id": mb.consumed_through_id,
                "wake_notified_id": mb.wake_notified_id,
            }
            for mb in mailboxes
        ]

    decisions: list[SeatWakeDecision] = []
    for snapshot in snapshots:
        holder = _MailboxSnapshot(
            id=str(snapshot["id"]),
            current_terminal_id=snapshot["current_terminal_id"],
            consumed_through_id=int(snapshot["consumed_through_id"] or 0),
            wake_notified_id=int(snapshot["wake_notified_id"] or 0),
        )
        try:
            decision = _reconcile_one(holder, cutoff)
        except Exception:
            logger.exception(
                "f747_seat_wake_reconcile_failed mailbox=%s seat=%s",
                snapshot["id"],
                snapshot["current_terminal_id"],
            )
            continue
        decisions.append(decision)
        _log_decision(decision)
    return decisions


@dataclass
class _MailboxSnapshot:
    """Detached copy of the mailbox columns the reconcile reads."""

    id: str
    current_terminal_id: Optional[str]
    consumed_through_id: int
    wake_notified_id: int


def _log_decision(decision: SeatWakeDecision) -> None:
    """Log one decision, always naming the seat.

    ``no_pending`` and ``no_terminal`` are the steady state on every tick, so
    they go to DEBUG -- an INFO line per mailbox per minute forever is itself
    the kind of noise this repo files as a quirk. Every decision that had
    unsurfaced rows is INFO, which is what leaves a journal line behind for the
    next sample.
    """
    payload = (
        "f747_seat_wake_reconcile seat=%s mailbox=%s outcome=%s "
        "max_id=%d rows=%d prev_notified_id=%d reason=%s"
    )
    args = (
        decision.terminal_id or "-",
        decision.mailbox_id,
        decision.outcome,
        decision.max_id,
        decision.row_count,
        decision.previous_notified_id,
        decision.reason or "-",
    )
    if decision.outcome in {"no_pending", "no_terminal"}:
        logger.debug(payload, *args)
    else:
        logger.info(payload, *args)


async def seat_wake_reconcile_daemon() -> None:
    """Background loop: reconcile idle-seat wakes on a fixed interval.

    The interval and the kill switch are read every tick, so flipping either
    takes effect without a server restart.
    """
    logger.info(
        "f747_seat_wake_reconcile_daemon_started interval_s=%.1f enabled=%s",
        _interval_s(),
        _enabled(),
    )
    while True:
        await asyncio.sleep(_interval_s())
        try:
            await asyncio.to_thread(reconcile_seat_wakes)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("f747_seat_wake_reconcile_sweep_failed")
