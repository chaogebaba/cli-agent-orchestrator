"""FX191: Convergent delivery service.

Obligation store, resolver (D2), ladder (D3), convergence tick (D5),
escalation (D6), stranded detector (D8), trace emitter (D7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, text
from sqlalchemy.orm import Session

from cli_agent_orchestrator.clients.database import (
    DeliveryObligationModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxModel,
    SessionLocal,
    TerminalModel,
    _utcnow,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# D2: Delivery target resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryTarget:
    """Resolved target for delivering a message to a supervisor."""

    terminal_id: str | None
    tmux_session: str | None
    tmux_window: str | None
    cc_inbox_path: str | None
    has_registry: bool = False  # CC session registry record exists


def resolve_supervisor_target(mailbox_id: str, db: Session | None = None) -> DeliveryTarget:
    """D2: Derive deliverability from the mailbox row — never registered state.

    The mailbox row is the authority. A missing optional field demotes a rung;
    it never disables delivery.
    """

    def _resolve(session: Session) -> DeliveryTarget:
        mailbox = session.query(MailboxModel).filter_by(id=mailbox_id).one_or_none()
        if mailbox is None or mailbox.current_terminal_id is None:
            return DeliveryTarget(
                terminal_id=None,
                tmux_session=None,
                tmux_window=None,
                cc_inbox_path=None,
            )
        terminal = (
            session.query(TerminalModel).filter_by(id=mailbox.current_terminal_id).one_or_none()
        )
        if terminal is None:
            return DeliveryTarget(
                terminal_id=mailbox.current_terminal_id,
                tmux_session=None,
                tmux_window=None,
                cc_inbox_path=mailbox.cc_inbox_path,
            )
        # Best-effort registry check (for rung 1 optimization)
        has_registry = False
        try:
            from cli_agent_orchestrator.services.cc_session_registry import (
                resolve_target,
            )

            result = resolve_target(terminal.id, terminal.tmux_session, terminal.tmux_window)
            has_registry = result.refusal_reason != "no_registry_records"
        except Exception:
            pass

        return DeliveryTarget(
            terminal_id=terminal.id,
            tmux_session=terminal.tmux_session,
            tmux_window=terminal.tmux_window,
            cc_inbox_path=mailbox.cc_inbox_path,
            has_registry=has_registry,
        )

    if db is not None:
        return _resolve(db)
    with SessionLocal() as session:
        return _resolve(session)


# ---------------------------------------------------------------------------
# D7: Trace emitter
# ---------------------------------------------------------------------------


def emit_trace(
    message_id: int,
    kind: str,
    phase: str,
    decision: str,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
    db: Session | None = None,
) -> None:
    """Write a single fx191-namespaced trace row. No log emission (D15)."""

    def _emit(session: Session) -> None:
        session.add(
            InboxMessageTraceEventModel(
                message_id=message_id,
                kind=f"fx191.{phase}",
                phase=phase,
                decision=decision,
                reason=reason,
                payload=payload or {},
            )
        )
        session.commit()

    if db is not None:
        _emit(db)
    else:
        with SessionLocal() as session:
            _emit(session)


def emit_trace_or_collapse(
    message_id: int,
    phase: str,
    decision: str,
    reason: str | None,
    db: Session,
) -> None:
    """D15: Repeated identical defers collapse onto existing row via counter."""
    existing = (
        db.query(InboxMessageTraceEventModel)
        .filter(
            InboxMessageTraceEventModel.message_id == message_id,
            InboxMessageTraceEventModel.phase == phase,
            InboxMessageTraceEventModel.decision == decision,
            InboxMessageTraceEventModel.reason == reason,
        )
        .first()
    )
    if existing is not None and decision == "defer":
        # Collapse: bump counter in payload
        p = existing.payload if isinstance(existing.payload, dict) else {}
        p["count"] = p.get("count", 1) + 1
        existing.payload = p
        # Force SQLAlchemy to detect the change
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(existing, "payload")
    else:
        db.add(
            InboxMessageTraceEventModel(
                message_id=message_id,
                kind=f"fx191.{phase}",
                phase=phase,
                decision=decision,
                reason=reason,
                payload={},
            )
        )


# ---------------------------------------------------------------------------
# D1: Obligation store
# ---------------------------------------------------------------------------


def create_obligation(inbox_row_id: int, mailbox_id: str, db: Session) -> None:
    """Create obligation in the same transaction as the message (D1 atomic)."""
    db.add(
        DeliveryObligationModel(
            inbox_row_id=inbox_row_id,
            mailbox_id=mailbox_id,
            state="OPEN",
            accepted_at=_utcnow(),
            next_attempt_at=_utcnow(),
        )
    )
    # Trace: accept
    db.add(
        InboxMessageTraceEventModel(
            message_id=inbox_row_id,
            kind="fx191.accept",
            phase="accept",
            decision="proceed",
            reason=None,
            payload={},
        )
    )


def settle_obligation_acked(inbox_row_id: int, db: Session | None = None) -> None:
    """Settle obligation to ACKED (D10: evidence of consumption)."""

    def _settle(session: Session) -> None:
        obl = (
            session.query(DeliveryObligationModel)
            .filter_by(inbox_row_id=inbox_row_id, state="OPEN")
            .one_or_none()
        )
        if obl is None:
            return
        obl.state = "ACKED"
        obl.terminal_at = _utcnow()
        obl.terminal_reason = "consumed"
        emit_trace_or_collapse(inbox_row_id, "ack", "settle", "consumed", session)

    if db is not None:
        _settle(db)
    else:
        with SessionLocal() as session:
            _settle(session)
            session.commit()


# ---------------------------------------------------------------------------
# D3: Ladder — rung 1 (native) and rung 2 (nudge/floor)
# ---------------------------------------------------------------------------


@dataclass
class LadderResult:
    """Result of attempting delivery via the ladder."""

    delivered: bool
    phase: str
    decision: str
    reason: str | None


def attempt_rung1(
    target: DeliveryTarget,
    inbox_row_id: int,
    message_count: int = 1,
    oldest_age_s: float = 0.0,
) -> LadderResult:
    """Rung 1: native CC socket ring + cc_inbox_path write. Best-effort.

    At S0 (shadow), this is purely additive — old transports still run.
    """
    import os

    if not target.has_registry:
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="defer",
            reason="no_registry_records",
        )

    if not target.cc_inbox_path:
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="defer",
            reason="no_inbox_path",
        )

    # Validate path usability (D2 rule 3)
    parent_dir = os.path.dirname(target.cc_inbox_path)
    if not os.path.isdir(parent_dir):
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="defer",
            reason="path_unusable",
        )

    # Attempt native ring via the existing doorbell machinery
    try:
        from cli_agent_orchestrator.services.doorbell_service import _attempt_native_ring

        result = _attempt_native_ring(target.terminal_id, inbox_row_id)
        if result == "rang":
            return LadderResult(
                delivered=True,
                phase="transport_attempt",
                decision="proceed",
                reason=None,
            )
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="defer",
            reason=result or "wake_unverified",
        )
    except Exception:
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="defer",
            reason="native_exception",
        )


def attempt_rung2(
    target: DeliveryTarget,
    inbox_row_id: int,
    message_count: int = 1,
    oldest_age_s: float = 0.0,
    *,
    is_escalation: bool = False,
) -> LadderResult:
    """Rung 2: dumb guaranteed pane nudge. Zero deliverability preconditions (D3/D16).

    Only safety gates can defer this rung (D4).
    """
    if target.tmux_session is None or target.tmux_window is None:
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="fail",
            reason="no_live_target",
        )

    # Safety gates (D4): check before injection
    safety_reason = _check_safety_gates(target, is_escalation=is_escalation)
    if safety_reason is not None:
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="defer",
            reason=safety_reason,
        )

    # D3: Fixed CAO-authored line with only CAO-computed integers
    age_s = int(oldest_age_s)
    if is_escalation:
        nudge_text = (
            f"[cao] ESCALATION: message {inbox_row_id} undelivered for {age_s}s "
            f"after attempting delivery. Run list_messages."
        )
    else:
        nudge_text = (
            f"[cao] {message_count} message(s) waiting "
            f"(oldest id {inbox_row_id}, {age_s}s). Run list_messages to surface them."
        )

    # Inject via tmux send-keys
    try:
        from cli_agent_orchestrator.clients.tmux import tmux_client

        tmux_client.send_keys(target.tmux_session, target.tmux_window, nudge_text + "\n")
        return LadderResult(
            delivered=True,
            phase="surface",
            decision="proceed",
            reason=None,
        )
    except Exception as e:
        logger.debug("rung2 send_keys failed: %s", e)
        return LadderResult(
            delivered=False,
            phase="surface",
            decision="defer",
            reason="send_keys_failed",
        )


def _check_safety_gates(target: DeliveryTarget, *, is_escalation: bool = False) -> str | None:
    """D4: Safety gates — may defer, never terminate.

    Returns the reason string if deferred, None if safe to proceed.
    """
    if target.terminal_id is None:
        return None  # no_live_target handled by rung2 itself

    try:
        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter_by(id=target.terminal_id).one_or_none()
            if terminal is None:
                return None

            # Recovery state gate
            if terminal.recovery_state and terminal.recovery_state != "idle":
                return "recovery_state"
    except Exception:
        pass

    # Draft guard / dialog hazard / _inject_safe vetoes — check via terminal status
    # D4: only safety gates, never deliverability gates
    try:
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            stalled_callback_watchdog,
        )

        with stalled_callback_watchdog._lock:
            episode = stalled_callback_watchdog._episodes.get(target.terminal_id)
            if episode is not None:
                # FX193 D2: episode.status is now live (was dead code pre-fx193)
                status_val = episode.status
                # D4: busy is caller-side policy, NOT a safety gate for escalation
                if not is_escalation and status_val == TerminalStatus.PROCESSING:
                    return "not_idle"
                if status_val == TerminalStatus.WAITING_USER_ANSWER:
                    return "waiting_user_answer"
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# D5: Convergence tick (called from watchdog run loop)
# ---------------------------------------------------------------------------


def convergence_tick() -> None:
    """Drive all OPEN obligations one step. Called as first sibling tick (D5/D8)."""
    from cli_agent_orchestrator.services.config_service import ConfigService

    phase = ConfigService.get("delivery.phase", "shadow")
    tick_s = float(ConfigService.get("delivery.tick_s", 5.0))
    escalate_after_s = float(ConfigService.get("delivery.escalate_after_s", 120.0))

    now = _utcnow()

    with SessionLocal() as db:
        # Claim due obligations
        due = (
            db.query(DeliveryObligationModel)
            .filter(
                DeliveryObligationModel.state == "OPEN",
                DeliveryObligationModel.next_attempt_at <= now,
            )
            .all()
        )

        for obl in due:
            try:
                _drive_one_obligation(db, obl, now, escalate_after_s, phase)
                db.commit()
            except Exception:
                db.rollback()
                logger.debug(
                    "fx191 obligation tick exception inbox=%s", obl.inbox_row_id, exc_info=True
                )

    # D8: stranded detector
    _check_stranded(escalate_after_s)

    # FX193: Fire any due nudges (after all obligations have been driven/coalesced)
    _fire_due_nudges()


def _get_consumption_cursor(mailbox_id: str) -> int | None:
    """D1 helper: read the durable consumption cursor for a mailbox."""
    try:
        with SessionLocal() as db:
            cursor = db.query(MailboxModel.consumed_through_id).filter_by(id=mailbox_id).scalar()
            return int(cursor) if cursor is not None else None
    except Exception:
        return None


def _get_pending_oldest(mailbox_id: str) -> tuple[int, int] | None:
    """D1 helper: return (count, oldest_id) of pending messages for a mailbox."""
    try:
        with SessionLocal() as db:
            from sqlalchemy import func

            row = (
                db.query(func.count(InboxModel.id), func.min(InboxModel.id))
                .filter(
                    InboxModel.logical_receiver_id == mailbox_id,
                    InboxModel.status.in_(["pending", "held"]),
                )
                .one()
            )
            count, oldest = row
            if count and oldest:
                return (int(count), int(oldest))
            return None
    except Exception:
        return None


def _fire_due_nudges() -> None:
    """FX193: Collect and fire nudges that passed D1/D2/D4 gates."""
    from cli_agent_orchestrator.services.nudge_discipline import nudge_discipline

    intents = nudge_discipline.collect_due(
        get_consumption_cursor=_get_consumption_cursor,
        get_pending_oldest=_get_pending_oldest,
    )

    for intent in intents:
        target = resolve_supervisor_target(intent.mailbox_id)
        if target.terminal_id is None or target.tmux_session is None:
            continue

        # Compute age from the oldest pending message
        oldest_age_s = 0.0
        try:
            with SessionLocal() as db:
                msg = db.query(InboxModel).filter_by(id=intent.oldest_inbox_row_id).one_or_none()
                if msg is not None and msg.created_at is not None:
                    created = msg.created_at
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    oldest_age_s = (_utcnow() - created).total_seconds()
        except Exception:
            pass

        r2 = attempt_rung2(
            target,
            intent.oldest_inbox_row_id,
            message_count=intent.message_count,
            oldest_age_s=oldest_age_s,
        )
        if r2.delivered:
            logger.debug(
                "fx193 nudge fired terminal=%s count=%d oldest=%d",
                intent.terminal_id,
                intent.message_count,
                intent.oldest_inbox_row_id,
            )


def _drive_one_obligation(
    db: Session,
    obl: DeliveryObligationModel,
    now: datetime,
    escalate_after_s: float,
    phase: str,
) -> None:
    """Drive a single obligation one step down the ladder."""
    from cli_agent_orchestrator.services.config_service import ConfigService

    tick_s = float(ConfigService.get("delivery.tick_s", 5.0))

    # Check if already ACKED by consumption (D10)
    mailbox = db.query(MailboxModel).filter_by(id=obl.mailbox_id).one_or_none()
    if mailbox is not None and mailbox.consumed_through_id >= obl.inbox_row_id:
        obl.state = "ACKED"
        obl.terminal_at = now
        obl.terminal_reason = "consumed"
        emit_trace_or_collapse(obl.inbox_row_id, "ack", "settle", "consumed", db)
        return

    # Check escalation threshold (D6)
    accepted = obl.accepted_at
    if accepted is not None and accepted.tzinfo is None:
        accepted = accepted.replace(tzinfo=timezone.utc)
    age_s = (now - accepted).total_seconds() if accepted else 0.0
    if age_s >= escalate_after_s:
        _escalate(db, obl, now, age_s)
        return

    # Resolve target (D2: every tick, not once at creation)
    target = resolve_supervisor_target(obl.mailbox_id, db)
    emit_trace_or_collapse(obl.inbox_row_id, "resolve", "proceed", None, db)

    if target.terminal_id is None:
        # No live target — defer, escalation clock keeps running
        from datetime import timedelta

        obl.next_attempt_at = now + timedelta(seconds=tick_s)
        obl.attempts += 1
        emit_trace_or_collapse(obl.inbox_row_id, "transport_attempt", "defer", "no_live_target", db)
        return

    # Record first_attempt_at
    if obl.first_attempt_at is None:
        obl.first_attempt_at = now

    # Calculate age for nudge text
    msg_age_s = age_s

    # Attempt rung 1 (optimization — only in primary or shadow modes)
    r1 = attempt_rung1(target, obl.inbox_row_id, oldest_age_s=msg_age_s)
    emit_trace_or_collapse(obl.inbox_row_id, r1.phase, r1.decision, r1.reason, db)

    if r1.delivered:
        # Rung 1 succeeded — obligation stays OPEN until ACKED by consumption (D10)
        from datetime import timedelta

        obl.next_attempt_at = now + timedelta(seconds=tick_s)
        obl.attempts += 1
        emit_trace_or_collapse(obl.inbox_row_id, "surface", "proceed", None, db)
        return

    # Rung 2: the floor (D16 — permanently armed, zero preconditions)
    # FX193: Instead of firing rung2 directly, arm/coalesce in nudge_discipline.
    # The actual pane injection happens in fire_due_nudges() after all obligations
    # have been driven — this is the coalescing seam (D3).
    from cli_agent_orchestrator.services.nudge_discipline import nudge_discipline

    # Count all pending messages for this mailbox for the coalesced payload
    pending_count = (
        db.query(InboxModel)
        .filter(
            InboxModel.logical_receiver_id == obl.mailbox_id,
            InboxModel.status.in_(["pending", "held"]),
        )
        .count()
    )
    oldest_pending = (
        db.query(InboxModel.id)
        .filter(
            InboxModel.logical_receiver_id == obl.mailbox_id,
            InboxModel.status.in_(["pending", "held"]),
        )
        .order_by(InboxModel.id.asc())
        .limit(1)
        .scalar()
    )
    if pending_count > 0 and oldest_pending is not None and target.terminal_id is not None:
        nudge_discipline.arm_or_coalesce(
            terminal_id=target.terminal_id,
            mailbox_id=obl.mailbox_id,
            message_count=pending_count,
            oldest_inbox_row_id=oldest_pending,
        )
    emit_trace_or_collapse(obl.inbox_row_id, "surface", "defer", "fx193_armed", db)

    from datetime import timedelta

    obl.next_attempt_at = now + timedelta(seconds=tick_s)
    obl.attempts += 1


def _escalate(
    db: Session,
    obl: DeliveryObligationModel,
    now: datetime,
    age_s: float,
) -> None:
    """D6: Bounded escalation — exactly once per obligation."""
    target = resolve_supervisor_target(obl.mailbox_id, db)

    if target.terminal_id is None or target.tmux_session is None:
        # No live target — escalate as no_live_target (D6)
        obl.state = "ESCALATED"
        obl.terminal_at = now
        obl.terminal_reason = "no_live_target"
        emit_trace_or_collapse(obl.inbox_row_id, "escalate", "settle", "no_live_target", db)
        logger.error(
            "fx191_escalated inbox=%d reason=no_live_target age=%.0fs attempts=%d",
            obl.inbox_row_id,
            age_s,
            obl.attempts,
        )
        return

    # Attempt escalation injection (D6 step 1: force past busy, not past hazard)
    r2 = attempt_rung2(
        target,
        obl.inbox_row_id,
        oldest_age_s=age_s,
        is_escalation=True,
    )

    # D6: ERROR and banner fire regardless of injection success (step 3/4)
    obl.state = "ESCALATED"
    obl.terminal_at = now
    last_reason = r2.reason or "escalation_timeout"
    obl.terminal_reason = last_reason
    emit_trace_or_collapse(obl.inbox_row_id, "escalate", "settle", last_reason, db)

    # D15: exactly one ERROR log
    logger.error(
        "fx191_escalated inbox=%d reason=%s age=%.0fs attempts=%d",
        obl.inbox_row_id,
        last_reason,
        age_s,
        obl.attempts,
    )


def _check_stranded(escalate_after_s: float) -> None:
    """D8: Stranded detector — any OPEN obligation with no recent trace is a bug."""
    from datetime import timedelta

    threshold = timedelta(seconds=2 * escalate_after_s)
    now = _utcnow()
    cutoff = now - threshold

    with SessionLocal() as db:
        stranded = (
            db.query(DeliveryObligationModel)
            .filter(
                DeliveryObligationModel.state == "OPEN",
                DeliveryObligationModel.accepted_at < cutoff,
            )
            .all()
        )
        for obl in stranded:
            # Check newest trace row
            newest = (
                db.query(InboxMessageTraceEventModel)
                .filter(
                    InboxMessageTraceEventModel.message_id == obl.inbox_row_id,
                    InboxMessageTraceEventModel.kind.like("fx191.%"),
                )
                .order_by(InboxMessageTraceEventModel.created_at.desc())
                .first()
            )
            newest_at = newest.created_at if newest else None
            if newest_at is not None and newest_at.tzinfo is None:
                newest_at = newest_at.replace(tzinfo=timezone.utc)
            if newest is None or (newest_at is not None and newest_at < cutoff):
                logger.error(
                    "fx191_stranded inbox=%d last_phase=%s",
                    obl.inbox_row_id,
                    newest.phase if newest else "none",
                )
                # Immediate escalation
                accepted = obl.accepted_at
                if accepted is not None and accepted.tzinfo is None:
                    accepted = accepted.replace(tzinfo=timezone.utc)
                _escalate(db, obl, now, (now - accepted).total_seconds() if accepted else 0.0)
                db.commit()


# ---------------------------------------------------------------------------
# D11: Backfill existing pending messages at first startup
# ---------------------------------------------------------------------------


def backfill_obligations() -> None:
    """D11: At first startup, create obligations for existing pending supervisor messages."""
    with SessionLocal() as db:
        # Find supervisor mailboxes
        supervisor_mailboxes = db.query(MailboxModel).filter_by(role="supervisor").all()
        for mailbox in supervisor_mailboxes:
            # Find pending messages without obligations
            from sqlalchemy import exists

            pending = (
                db.query(InboxModel)
                .filter(
                    InboxModel.status.in_(["pending", "held"]),
                    InboxModel.logical_receiver_id == mailbox.id,
                    ~exists(
                        db.query(DeliveryObligationModel)
                        .filter(DeliveryObligationModel.inbox_row_id == InboxModel.id)
                        .correlate(InboxModel)
                    ),
                )
                .all()
            )
            for msg in pending:
                db.add(
                    DeliveryObligationModel(
                        inbox_row_id=msg.id,
                        mailbox_id=mailbox.id,
                        state="OPEN",
                        accepted_at=msg.created_at or _utcnow(),
                        next_attempt_at=_utcnow(),
                    )
                )
        db.commit()
