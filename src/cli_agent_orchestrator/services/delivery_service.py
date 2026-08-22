"""FX191: Convergent delivery service.

Obligation store, resolver (D2), ladder (D3), convergence tick (D5),
escalation (D6), stranded detector (D8), trace emitter (D7).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, text
from sqlalchemy.orm import Session

from cli_agent_orchestrator.clients.database import (
    DeliveryObligationModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    SessionLocal,
    TerminalModel,
    _utcnow,
)

logger = logging.getLogger(__name__)

# F203 D17: Health-warning dedup state — (terminal_id, inbox_row_id, diagnosis) → last emit time
_health_warning_dedup: dict[tuple[str, int, str], "datetime"] = {}

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
    liveness: str = "presumed_live"  # D6: "presumed_live" | "confirmed_dead"


def is_target_confirmed_dead(terminal_id: str, db: Session) -> bool:
    """D6: DB-only deadness check. No tmux call. `db` is REQUIRED (S2 — no default)."""
    from cli_agent_orchestrator.clients.database import PaneExitTombstoneModel

    # A terminal is confirmed dead if a tombstone exists for its current generation
    # and no newer activated incarnation exists.
    tombstone = (
        db.query(PaneExitTombstoneModel.id)
        .filter(PaneExitTombstoneModel.terminal_id == terminal_id)
        .first()
    )
    return tombstone is not None


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

        # D11: Active re-probe readmits — if registry now has a record,
        # un-eject rung1 (self-heal on CC upgrade/restart).
        if has_registry:
            try:
                from cli_agent_orchestrator.services.transport_ejection import (
                    transport_ejection_service,
                )

                if transport_ejection_service.is_ejected(terminal.id, "rung1"):
                    transport_ejection_service.readmit(terminal.id, "rung1")
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
    """D15: Repeated identical traces collapse onto existing row via counter.

    F351: Extended to collapse ANY repeated identical (phase, decision, reason)
    combination, not just "defer". The resolve+proceed trace was writing a new
    row every 5s for every OPEN obligation — hundreds of garbage rows per hour.
    """
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
    if existing is not None:
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


def _create_self_notify_obligation(terminal_id: str) -> None:
    """F203 D15: Create a delivery obligation targeting the supervisor's own mailbox.

    Called when the watchdog detects a supervisor terminal with pending messages
    but no caller_id. Routes delivery to the supervisor's own mailbox instead of
    permanently latching fired=True.
    """
    try:
        with SessionLocal() as db:
            mailbox = (
                db.query(MailboxModel)
                .filter_by(current_terminal_id=terminal_id)
                .first()
            )
            if mailbox is None:
                return

            # Check if there's already an OPEN obligation for this mailbox
            existing = (
                db.query(DeliveryObligationModel)
                .filter_by(mailbox_id=mailbox.id, state="OPEN")
                .first()
            )
            if existing is not None:
                return  # Already has an obligation

            # Find the oldest pending message for this mailbox
            oldest = (
                db.query(InboxModel)
                .filter(
                    InboxModel.logical_receiver_id == mailbox.id,
                    InboxModel.status.in_(["pending", "held"]),
                )
                .order_by(InboxModel.id)
                .first()
            )
            if oldest is None:
                return  # No pending messages

            now = _utcnow()
            obl = DeliveryObligationModel(
                inbox_row_id=oldest.id,
                mailbox_id=mailbox.id,
                state="OPEN",
                accepted_at=now,
                first_attempt_at=now,
                next_attempt_at=now,
                attempts=0,
            )
            db.add(obl)
            db.commit()
    except Exception:
        logger.debug("f203 self-notify obligation failed for %s", terminal_id, exc_info=True)


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
    F203 D9: no_registry_records is a counted refusal → ejection after N.
    """
    import os

    # D7: Short-circuit on confirmed-dead target
    if target.liveness == "confirmed_dead":
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="settle",
            reason="target_confirmed_dead",
        )

    from cli_agent_orchestrator.services.transport_ejection import (
        transport_ejection_service,
    )

    if not target.has_registry:
        # D9: Record the refusal for ejection counting
        transport_ejection_service.record_refusal(
            target.terminal_id, "rung1", "no_registry_records"
        )
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
    # D7: Short-circuit on confirmed-dead target (ahead of its own no_live_target check)
    if target.liveness == "confirmed_dead":
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="settle",
            reason="target_confirmed_dead",
        )

    if target.tmux_session is None or target.tmux_window is None:
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="fail",
            reason="no_live_target",
        )

    # F210 D1/D2: supervisor-role terminals NEVER receive composer injection.
    # Enforced here, at the sole send_keys sink of the delivery ladder, so no
    # present or future caller can bypass it. Unconditional: it outranks
    # delivery.nudge_sendkeys_enabled in both knob states (D10).
    if _is_supervisor_role_target(target):
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="defer",
            reason="supervisor_role_exempt",
        )

    # F210 D10: the knob gates the whole rung, ahead of any capture work.
    from cli_agent_orchestrator.services.config_service import ConfigService

    if not ConfigService.get("delivery.nudge_sendkeys_enabled", True):
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="defer",
            reason="sendkeys_disabled",
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

    # F210 D7/D8: fire-time draft re-verification. _check_safety_gates read the
    # composer two full history captures ago; re-read at the sink so a draft
    # that appeared meanwhile still vetoes.
    refire_reason = _refire_draft_state(target)
    if refire_reason is not None:
        return LadderResult(
            delivered=False,
            phase="transport_attempt",
            decision="defer",
            reason=refire_reason,
        )

    # Inject via tmux send-keys
    try:
        from cli_agent_orchestrator.clients.tmux import tmux_client

        # F210 D9: no trailing "\n" — tmux_client.send_keys submits with its own
        # send-keys Enter, and a newline inside the pasted buffer submits the
        # pane's current input line by itself.
        tmux_client.send_keys(target.tmux_session, target.tmux_window, nudge_text)
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


def _is_supervisor_role_target(target: DeliveryTarget) -> bool:
    """F210 D1/D2: is this target the live incarnation of a supervisor mailbox?

    Fails CLOSED — an unanswerable probe reports "supervisor", because the cost
    of a wrong False is typing into the user's own pane.
    """
    if target.terminal_id is None:
        return False
    try:
        from cli_agent_orchestrator.services.mailbox_service import is_supervisor_role_terminal

        with SessionLocal() as db:
            return is_supervisor_role_terminal(target.terminal_id, db)
    except Exception:
        logger.debug("f210 supervisor-role probe failed for %s", target.terminal_id, exc_info=True)
        return True


def _refire_draft_state(target: DeliveryTarget) -> str | None:
    """F210 D7/D8: one fresh authority read at the sink. None = clear to inject.

    Providers without ``read_composer_draft_authority`` keep today's single
    capture-and-check gate — the probe degrades, it does not block delivery.
    """
    if target.terminal_id is None:
        return None
    try:
        from cli_agent_orchestrator.providers.manager import provider_manager

        provider = provider_manager.get_provider(target.terminal_id)
    except Exception:
        return None
    authority_reader = getattr(provider, "read_composer_draft_authority", None)
    if provider is None or not callable(authority_reader):
        return None
    try:
        state, _chip_present = authority_reader()
    except Exception:
        return "draft_unresolved"
    if state == "empty":
        return None
    if state == "nonempty":
        return "user_draft_present"
    return "draft_unresolved"


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

    # FX193 D2b: Draft-guard veto — non-empty composer draft defers injection
    # for ALL rung2 calls (first, repeats, AND escalation). A submitted corrupted
    # prompt is worse than a late nudge; the E-bound escalation clock still runs.
    try:
        from cli_agent_orchestrator.clients.database import get_terminal_metadata
        from cli_agent_orchestrator.providers.manager import provider_manager

        metadata = get_terminal_metadata(target.terminal_id)
        if metadata is not None:
            provider = provider_manager.get_provider(target.terminal_id)
            if provider is not None and callable(getattr(provider, "read_composer_draft", None)):
                from cli_agent_orchestrator.backends.registry import get_backend

                try:
                    captured = get_backend().get_history(
                        metadata["tmux_session"],
                        metadata["tmux_window"],
                        tail_lines=45,
                        strip_escapes=True,
                    )
                    draft = provider.read_composer_draft(captured.splitlines())
                    if draft:
                        return "user_draft_present"
                except Exception:
                    pass
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# D5: Convergence tick (called from watchdog run loop)
# ---------------------------------------------------------------------------


def convergence_tick() -> None:
    """Drive all OPEN obligations one step. Called as first sibling tick (D5/D8).

    FX194: The tick loop no longer drives delivery at boundaries — it only
    tracks obligation age, fires the masked interrupt (D2) when no boundary
    has occurred for the E-window, updates the @cao_pending indicator (D4),
    and handles escalation (D6).
    """
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

    # F218-a D8: Settle obligations for confirmed-dead targets (S2: tick's session)
    with SessionLocal() as db:
        _settle_dead_target_obligations(db)
        db.commit()

    # FX193/FX194: Fire any due nudges — now the D2 masked interrupt path only
    # (boundaries handle primary delivery)
    _fire_due_nudges()

    # F203 D6: Oneshot re-arm — call reset_boundary_counter on every pull-cycle
    # exit for all terminals with OPEN obligations.  If it returns True (work
    # arrived since last reset), re-poll by repeating _fire_due_nudges.
    _oneshot_rearm_boundaries()

    # FX194 D4: Update @cao_pending for all terminals with OPEN obligations
    _update_pending_indicators()

    # FX194 D1b: Check health warnings for obligations crossing E
    _check_health_warnings(escalate_after_s)

    # F206a/H3: Re-resolve ESCALATED obligations whose inbox message is still
    # undelivered, at a bounded cadence (once per escalate_after_s).
    _reresolve_escalated(escalate_after_s)

    # WPDT W5: Late-bind watcher for native UDS gate
    _check_uds_late_bind()


# ---------------------------------------------------------------------------
# WPDT W5: Late-bind watcher for native UDS messaging gate
# ---------------------------------------------------------------------------

# Per-process state: logs state transition once (id+revision style, not spam)
_uds_gate_observed: bool = False


def _check_uds_late_bind() -> None:
    """W5: Cheap periodic check for UDS socket-dir existence.

    Logs a debug line once when the [uds-messaging] directory appears (F191/F194
    parked-with-watcher). Does not activate any delivery path — only observes.
    """
    global _uds_gate_observed

    if _uds_gate_observed:
        return  # Already logged the transition, no repeat

    try:
        from cli_agent_orchestrator.services.cc_session_registry import _sessions_dir

        sessions_dir = _sessions_dir()
        if sessions_dir is None or not sessions_dir.exists():
            return

        # Look for any session dir with a socket file
        import glob

        socket_files = glob.glob(str(sessions_dir / "*" / "*.sock"))
        if socket_files:
            _uds_gate_observed = True
            logger.debug(
                "[uds-messaging] socket-dir detected: %s (%d sockets)",
                sessions_dir,
                len(socket_files),
            )
    except Exception:
        pass


def _oneshot_rearm_boundaries() -> None:
    """F203 D6: Oneshot re-arm for all terminals with tracked boundary state.

    Called unconditionally on every pull-cycle exit (both the delivered path and
    the no-work exit).  If reset_boundary_counter returns True for any terminal
    (work arrived since last_boundary_at), re-poll via _fire_due_nudges.
    D7: No dedup on re-arm — duplicate wakes are noise; a missed wake is a hang.
    """
    from cli_agent_orchestrator.services.boundary_pull_service import boundary_pull_service

    # Get all tracked terminal IDs
    with boundary_pull_service._lock:
        terminal_ids = list(boundary_pull_service._states.keys())

    needs_repoll = False
    for terminal_id in terminal_ids:
        if boundary_pull_service.reset_boundary_counter(terminal_id):
            needs_repoll = True

    if needs_repoll:
        # Work arrived during the cycle — re-poll (D6: caller re-polls instead
        # of arming into a lost notify)
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
    """FX193/FX194: Collect and fire nudges that passed D1/D2/D4 gates.

    FX194 D2: The nudge is now a masked interrupt — it fires ONLY when no
    consumption boundary has occurred for the E-window. After firing, it is
    MASKED and re-arms on the first subsequent consumption boundary.

    F203 D1/D3: Uses delivery.interrupt_after_s (clamped to < escalate_after_s)
    instead of sharing the escalation threshold.
    """
    from cli_agent_orchestrator.services.boundary_pull_service import boundary_pull_service
    from cli_agent_orchestrator.services.config_service import ConfigService
    from cli_agent_orchestrator.services.nudge_discipline import nudge_discipline

    escalate_after_s = float(ConfigService.get("delivery.escalate_after_s", 120.0))
    tick_s = float(ConfigService.get("delivery.tick_s", 5.0))
    raw_interrupt = float(ConfigService.get("delivery.interrupt_after_s", 30.0))

    # D2: Clamp interrupt_after_s to strictly less than escalate_after_s
    effective_interrupt = min(raw_interrupt, escalate_after_s - tick_s)
    if effective_interrupt != raw_interrupt:
        logger.warning(
            "f203 delivery.interrupt_after_s clamped: configured=%.1f effective=%.1f "
            "(escalate_after_s=%.1f tick_s=%.1f)",
            raw_interrupt, effective_interrupt, escalate_after_s, tick_s,
        )

    intents = nudge_discipline.collect_due(
        get_consumption_cursor=_get_consumption_cursor,
        get_pending_oldest=_get_pending_oldest,
    )

    for intent in intents:
        target = resolve_supervisor_target(intent.mailbox_id)
        if target.terminal_id is None or target.tmux_session is None:
            continue

        # FX194 D2: Check if interrupt is allowed (not masked)
        # Compute oldest obligation age for this terminal
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

        # D2: Only fire the interrupt if boundary pull hasn't delivered
        if not boundary_pull_service.should_interrupt(
            target.terminal_id,
            intent.mailbox_id,
            oldest_age_s,
            effective_interrupt,
        ):
            # Boundary deliveries are handling it — no composer injection needed
            continue

        # D3: Single coalesced signal carrying count
        # "[cao] N waiting, oldest <id> <age>"
        age_s = int(oldest_age_s)
        nudge_text = (
            f"[cao] {intent.message_count} waiting, "
            f"oldest {intent.oldest_inbox_row_id} {age_s}s"
        )

        r2 = attempt_rung2(
            target,
            intent.oldest_inbox_row_id,
            message_count=intent.message_count,
            oldest_age_s=oldest_age_s,
        )
        if r2.delivered:
            # D2: Mark interrupt as fired → MASKED
            boundary_pull_service.mark_interrupt_fired(target.terminal_id)
            logger.debug(
                "fx194 interrupt fired terminal=%s count=%d oldest=%d",
                target.terminal_id,
                intent.message_count,
                intent.oldest_inbox_row_id,
            )
        elif r2.reason == "supervisor_role_exempt":
            # F210 D4: the interrupt rung's channel for exempt terminals. Without
            # this the exemption would make the interrupt threshold silent for
            # supervisors — only the escalation rung has the F206b floor.
            # Masking still applies, so the status line is not rewritten per tick.
            _fire_escalation_display_message(
                target, intent.oldest_inbox_row_id, intent.message_count
            )
            boundary_pull_service.mark_interrupt_fired(target.terminal_id)
            logger.debug(
                "f210 interrupt display-message terminal=%s count=%d oldest=%d",
                target.terminal_id,
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

    # F351: If the inbox message is already delivered, the obligation just waits
    # for the consumption ack — no need to resolve target or attempt transport.
    # Reschedule at a longer interval to avoid spinning every tick_s.
    inbox_row = db.query(InboxModel.status).filter_by(id=obl.inbox_row_id).one_or_none()
    if inbox_row is not None and inbox_row.status == "delivered":
        from datetime import timedelta
        # Back off: delivered messages just need the ack cursor to advance.
        # Check every 30s instead of every 5s — the ack_messages call will
        # settle it much sooner via the normal path.
        obl.next_attempt_at = now + timedelta(seconds=max(tick_s * 6, 30.0))
        obl.attempts += 1
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

    # D7: Short-circuit on confirmed-dead target
    if target.terminal_id and getattr(target, "liveness", "presumed_live") == "confirmed_dead":
        obl.state = "SETTLED_TARGET_DEAD"
        obl.terminal_at = now
        obl.terminal_reason = "target_confirmed_dead"
        emit_trace_or_collapse(obl.inbox_row_id, "escalate", "settle", "target_confirmed_dead", db)
        logger.info(
            "f219_dead_target terminal=%s transport=escalate action=settled",
            target.terminal_id,
        )
        return

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
        # F206a/H3: Arm re-resolve cadence
        from cli_agent_orchestrator.services.config_service import ConfigService
        from datetime import timedelta

        eas = float(ConfigService.get("delivery.escalate_after_s", 120.0))
        obl.next_attempt_at = now + timedelta(seconds=eas)
        return

    # WPDT W4: Supervisor targets NEVER receive rung2 composer injection at escalation.
    # Delete the re-entry path for supervisor targets — the rung2 body stays
    # dead-by-policy per f210 D14 (D10 knob retained for non-supervisor targets).
    if _is_supervisor_role_target(target):
        obl.state = "ESCALATED"
        obl.terminal_at = now
        obl.terminal_reason = "supervisor_role_exempt"
        emit_trace_or_collapse(obl.inbox_row_id, "escalate", "settle", "supervisor_role_exempt", db)
        logger.error(
            "fx191_escalated inbox=%d reason=supervisor_role_exempt age=%.0fs attempts=%d",
            obl.inbox_row_id,
            age_s,
            obl.attempts,
        )
        # F206b: fire display-message floor for supervisor targets
        _fire_escalation_display_message(target, obl.inbox_row_id)
        # F206a/H3: Arm re-resolve cadence
        from cli_agent_orchestrator.services.config_service import ConfigService
        from datetime import timedelta

        eas = float(ConfigService.get("delivery.escalate_after_s", 120.0))
        obl.next_attempt_at = now + timedelta(seconds=eas)
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

    # F206b: Visible floor — if injection failed, fire tmux display-message
    # unconditionally (bypasses draft guard — display-message does not touch
    # the pane input line).
    if not r2.delivered:
        _fire_escalation_display_message(target, obl.inbox_row_id)

    # F206a/H3: Arm next_attempt_at for bounded re-resolve cadence
    from cli_agent_orchestrator.services.config_service import ConfigService
    from datetime import timedelta

    eas = float(ConfigService.get("delivery.escalate_after_s", 120.0))
    obl.next_attempt_at = now + timedelta(seconds=eas)


def _fire_escalation_display_message(
    target: DeliveryTarget, inbox_row_id: int, undelivered_count: int = 1
) -> None:
    """F206b: Draft-guard-independent visible floor via tmux display-message.

    display-message renders in the status line and does NOT touch the pane
    input, so the D2b draft guard rationale does not apply.
    """
    # D7: Short-circuit on confirmed-dead target — zero tmux calls
    if getattr(target, "liveness", "presumed_live") == "confirmed_dead":
        logger.info(
            "f219_dead_target terminal=%s transport=display action=settled",
            target.terminal_id,
        )
        return

    from cli_agent_orchestrator.utils.tmux_command import tmux_argv

    # Use provided count or try to get aggregate (best-effort, no nested session
    # to avoid SQLite locking issues when called within an active transaction).
    if undelivered_count <= 0:
        undelivered_count = 1

    msg = f"[cao] {undelivered_count} undelivered — run list_messages"
    try:
        result = subprocess.run(
            tmux_argv("display-message", "-t", target.tmux_session, msg),
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            logger.warning(
                "f206b display-message rc=%d session=%s stderr=%s",
                result.returncode,
                target.tmux_session,
                result.stderr[:200] if result.stderr else b"",
            )
        else:
            logger.info(
                "f206b escalation_display_message inbox=%d session=%s count=%d",
                inbox_row_id,
                target.tmux_session,
                undelivered_count,
            )
    except Exception as e:
        logger.warning("f206b display-message exception session=%s: %s", target.tmux_session, e)


def _settle_dead_target_obligations(db: Session) -> None:
    """F218-a D8: Sweep OPEN/ESCALATED obligations for confirmed-dead targets.

    Three-case settlement (D8 — CAS/incarnation-safe, never ACKED, never
    deleted, zero transport to confirmed-dead terminals):

    Case (i) — Mailbox with successor incarnation:
        A newer or current generation exists in MailboxIncarnationModel whose
        terminal_id is NOT confirmed dead.  The inbox message's receiver_id is
        retargeted to that successor terminal; the obligation stays OPEN so the
        normal delivery ladder picks it up on the next tick.  No settlement, no
        parking — the message remains deliverable.

    Case (ii) — Mailbox-owned, no live successor:
        The logical_receiver_id exists (mailbox authority) but no live successor
        terminal is available.  The inbox message is PARKED with
        owner_receiver_id and owner_generation preserved for later reactivation
        by a future incarnation publish.  Obligation → SETTLED_TARGET_DEAD.

    Case (iii) — Direct terminal receiver, no mailbox authority:
        The message's logical_receiver_id is NULL (a direct terminal-addressed
        send with no mailbox backing).  Outcome: obligation →
        SETTLED_TARGET_DEAD, terminal_reason = "receiver_gone".  One aggregate
        notice per session is emitted (never per-message storm).

    Invariants enforced:
    - NEVER sets state = ACKED on any obligation.
    - NEVER deletes any inbox message.
    - NEVER dispatches transport (rung1/rung2) to a confirmed-dead terminal.
    - CAS-safe: generation comparisons use the obligation's mailbox_id row as
      the serialisation point; no ABA possible across incarnation publish.

    Parameters:
        db: The caller's active SQLAlchemy session (S2 — no nested SessionLocal;
            passed from convergence_tick's own session scope).
    """
    from cli_agent_orchestrator.clients.database import PaneExitTombstoneModel

    # N1: SQL join/subquery replaces O(N) Python scan — only fetch obligations
    # whose mailbox current_terminal_id has a tombstone (confirmed dead).
    dead_subq = (
        db.query(PaneExitTombstoneModel.terminal_id)
        .distinct()
        .subquery()
    )
    dead_obls = (
        db.query(DeliveryObligationModel)
        .join(MailboxModel, MailboxModel.id == DeliveryObligationModel.mailbox_id)
        .filter(
            DeliveryObligationModel.state.in_(("OPEN", "ESCALATED")),
            MailboxModel.current_terminal_id.isnot(None),
            MailboxModel.current_terminal_id.in_(
                db.query(dead_subq.c.terminal_id)
            ),
        )
        .all()
    )

    settled_count = 0
    rerouted_count = 0
    parked_count = 0
    # Track sessions that need aggregate notice (case iii)
    noticed_sessions: set[str] = set()
    now = _utcnow()

    for obl in dead_obls:
        try:
            # Resolve the mailbox to find the terminal
            mailbox = db.query(MailboxModel).filter_by(id=obl.mailbox_id).first()
            if mailbox is None or mailbox.current_terminal_id is None:
                continue

            terminal_id = mailbox.current_terminal_id
            # Double-check deadness (defensive; SQL join already filtered)
            if not is_target_confirmed_dead(terminal_id, db):
                continue

            # Target is confirmed dead — determine which case applies
            inbox_row = db.query(InboxModel).filter_by(id=obl.inbox_row_id).first()
            if inbox_row is None:
                continue

            # ─── Case determination ───────────────────────────────
            has_mailbox_authority = inbox_row.logical_receiver_id is not None

            if has_mailbox_authority:
                # Look for a live successor incarnation in this mailbox
                successor_terminal_id = _find_live_successor(
                    db, obl.mailbox_id, terminal_id
                )

                if successor_terminal_id is not None:
                    # ═══ Case (i): Reroute to successor ═══
                    inbox_row.receiver_id = successor_terminal_id
                    # Update mailbox current_terminal_id cache if stale
                    if mailbox.current_terminal_id != successor_terminal_id:
                        mailbox.current_terminal_id = successor_terminal_id
                    # Keep obligation OPEN — delivery ladder picks it up
                    obl.next_attempt_at = now
                    emit_trace_or_collapse(
                        obl.inbox_row_id, "settle", "reroute",
                        "successor_incarnation", db,
                    )
                    rerouted_count += 1
                    continue

                # ═══ Case (ii): Park — mailbox-owned, no live successor ═══
                if inbox_row.owner_receiver_id is None:
                    inbox_row.owner_receiver_id = inbox_row.receiver_id
                if inbox_row.owner_generation is None:
                    inbox_row.owner_generation = mailbox.generation
                inbox_row.status = "parked"
                obl.state = "SETTLED_TARGET_DEAD"
                obl.terminal_at = now
                obl.terminal_reason = "parked_no_successor"
                emit_trace_or_collapse(
                    obl.inbox_row_id, "settle", "park",
                    "no_live_successor", db,
                )
                parked_count += 1

            else:
                # ═══ Case (iii): Direct terminal, no mailbox authority ═══
                obl.state = "SETTLED_TARGET_DEAD"
                obl.terminal_at = now
                obl.terminal_reason = "receiver_gone"
                emit_trace_or_collapse(
                    obl.inbox_row_id, "settle", "settle",
                    "receiver_gone", db,
                )
                settled_count += 1
                # Track for aggregate session notice
                noticed_sessions.add(mailbox.session_name)

        except Exception:
            logger.debug(
                "f219_settle_exception inbox=%s", obl.inbox_row_id, exc_info=True
            )
            continue

    dirty = settled_count + parked_count + rerouted_count
    if dirty > 0:
        # Emit one aggregate notice per session for case (iii)
        for session_name in noticed_sessions:
            db.add(
                InboxMessageTraceEventModel(
                    message_id=0,
                    kind="f219.session_notice",
                    phase="settle",
                    decision="receiver_gone",
                    reason=f"session={session_name} settled={settled_count}",
                    payload={
                        "session_name": session_name,
                        "settled_count": settled_count,
                    },
                )
            )
        db.flush()
        logger.info(
            "f219_settle disposition=three_case rerouted=%d parked=%d "
            "settled=%d sessions_notified=%d",
            rerouted_count,
            parked_count,
            settled_count,
            len(noticed_sessions),
        )


def _find_live_successor(
    db: Session, mailbox_id: str, dead_terminal_id: str
) -> str | None:
    """Return the terminal_id of the newest live incarnation for mailbox_id.

    Iterates incarnations from highest generation downward.  Returns the first
    terminal_id that is NOT confirmed dead and differs from dead_terminal_id.
    Returns None when no live successor exists (all incarnations dead or only
    the dead terminal is registered).
    """
    incarnations = (
        db.query(MailboxIncarnationModel.terminal_id)
        .filter(MailboxIncarnationModel.mailbox_id == mailbox_id)
        .order_by(MailboxIncarnationModel.generation.desc())
        .all()
    )
    for (candidate_tid,) in incarnations:
        if candidate_tid == dead_terminal_id:
            continue
        if not is_target_confirmed_dead(candidate_tid, db):
            return candidate_tid
    return None


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
# F206a/H3: Re-resolve ESCALATED obligations at bounded cadence
# ---------------------------------------------------------------------------


def _reresolve_escalated(escalate_after_s: float) -> None:
    """F206a: Pick up ESCALATED obligations whose inbox message is still undelivered.

    Re-resolve at a slow cadence (once per escalate_after_s) using the existing
    next_attempt_at machinery — no new config key. On re-resolve:
    - If the draft guard no longer vetoes, perform the normal escalation injection.
    - If it still vetoes, re-fire the H2 display-message floor at most once per cadence.
    - If consumed in the meantime, settle to ACKED.
    """
    from datetime import timedelta

    now = _utcnow()

    with SessionLocal() as db:
        # Pick ESCALATED obligations that are due for re-resolve AND whose inbox
        # message is still not delivered.
        escalated_due = (
            db.query(DeliveryObligationModel)
            .filter(
                DeliveryObligationModel.state == "ESCALATED",
                DeliveryObligationModel.next_attempt_at <= now,
            )
            .all()
        )

        for obl in escalated_due:
            try:
                # Check if already consumed (race with MCP pull)
                mailbox = db.query(MailboxModel).filter_by(id=obl.mailbox_id).one_or_none()
                if mailbox is not None and mailbox.consumed_through_id >= obl.inbox_row_id:
                    obl.state = "ACKED"
                    obl.terminal_at = now
                    obl.terminal_reason = "consumed"
                    emit_trace_or_collapse(
                        obl.inbox_row_id, "f206_reresolve", "settle", "consumed", db
                    )
                    db.commit()
                    continue

                # Check inbox row status
                inbox_row = db.query(InboxModel).filter_by(id=obl.inbox_row_id).one_or_none()
                if inbox_row is None or inbox_row.status == "delivered":
                    obl.state = "ACKED"
                    obl.terminal_at = now
                    obl.terminal_reason = "consumed"
                    emit_trace_or_collapse(
                        obl.inbox_row_id, "f206_reresolve", "settle", "consumed", db
                    )
                    db.commit()
                    continue

                # Still undelivered — attempt re-injection
                target = resolve_supervisor_target(obl.mailbox_id, db)
                if target.terminal_id is None or target.tmux_session is None:
                    # No live target — keep ESCALATED, bump next_attempt_at
                    obl.next_attempt_at = now + timedelta(seconds=escalate_after_s)
                    db.commit()
                    continue

                # F219/WPDT W4: extend abandonment to escalation re-resolve
                if getattr(target, "liveness", "presumed_live") == "confirmed_dead":
                    obl.state = "SETTLED_TARGET_DEAD"
                    obl.terminal_at = now
                    obl.terminal_reason = "target_confirmed_dead"
                    emit_trace_or_collapse(
                        obl.inbox_row_id, "f206_reresolve", "settle", "target_confirmed_dead", db
                    )
                    db.commit()
                    continue

                # WPDT W4: supervisor targets skip rung2 — only display-message floor
                if _is_supervisor_role_target(target):
                    # F206b: re-trigger display-message for supervisor targets
                    from sqlalchemy import func as _func

                    esc_count = (
                        db.query(_func.count(DeliveryObligationModel.inbox_row_id))
                        .filter(
                            DeliveryObligationModel.state == "ESCALATED",
                            DeliveryObligationModel.mailbox_id == obl.mailbox_id,
                        )
                        .scalar()
                    ) or 1
                    _fire_escalation_display_message(target, obl.inbox_row_id, esc_count)
                    emit_trace_or_collapse(
                        obl.inbox_row_id, "f206_reresolve", "defer", "supervisor_role_exempt", db
                    )
                    obl.next_attempt_at = now + timedelta(seconds=escalate_after_s)
                    db.commit()
                    continue

                # Try escalation injection (goes through safety gates incl. draft guard)
                r2 = attempt_rung2(
                    target,
                    obl.inbox_row_id,
                    oldest_age_s=(now - (obl.accepted_at.replace(tzinfo=timezone.utc)
                                         if obl.accepted_at and obl.accepted_at.tzinfo is None
                                         else obl.accepted_at)).total_seconds()
                    if obl.accepted_at
                    else 0.0,
                    is_escalation=True,
                )

                if r2.delivered:
                    # Injection succeeded — settle as delivered-late
                    obl.state = "ACKED"
                    obl.terminal_at = now
                    obl.terminal_reason = "f206_reresolve_delivered"
                    emit_trace_or_collapse(
                        obl.inbox_row_id, "f206_reresolve", "settle", "delivered", db
                    )
                    logger.info(
                        "f206a reresolve_delivered inbox=%d", obl.inbox_row_id
                    )
                else:
                    # Still vetoed — fire H2 floor (display-message) at cadence
                    # Count escalated obligations for this mailbox for the message
                    from sqlalchemy import func as _func

                    esc_count = (
                        db.query(_func.count(DeliveryObligationModel.inbox_row_id))
                        .filter(
                            DeliveryObligationModel.state == "ESCALATED",
                            DeliveryObligationModel.mailbox_id == obl.mailbox_id,
                        )
                        .scalar()
                    ) or 1
                    _fire_escalation_display_message(target, obl.inbox_row_id, esc_count)
                    emit_trace_or_collapse(
                        obl.inbox_row_id,
                        "f206_reresolve",
                        "defer",
                        r2.reason or "still_vetoed",
                        db,
                    )

                # Bump next_attempt_at for cadence control
                obl.next_attempt_at = now + timedelta(seconds=escalate_after_s)
                db.commit()

            except Exception:
                db.rollback()
                logger.warning(
                    "f206a reresolve exception inbox=%s",
                    obl.inbox_row_id,
                    exc_info=True,
                )
                # Gate S1: rollback undid the cadence bump — re-apply it in a
                # separate mini-transaction so a persistently-throwing obligation
                # retries per escalate_after_s, not every tick.
                try:
                    db.query(DeliveryObligationModel).filter_by(
                        inbox_row_id=obl.inbox_row_id
                    ).update(
                        {"next_attempt_at": now + timedelta(seconds=escalate_after_s)}
                    )
                    db.commit()
                except Exception:
                    db.rollback()


# ---------------------------------------------------------------------------
# FX194 D4: Update @cao_pending tmux user variable
# ---------------------------------------------------------------------------


def _update_pending_indicators() -> None:
    """FX194 D4: Update @cao_pending for all terminals with OPEN obligations.

    Piggybacks on the existing tick loop. Write-through only on count change.
    """
    from cli_agent_orchestrator.services.boundary_pull_service import boundary_pull_service

    try:
        with SessionLocal() as db:
            from sqlalchemy import func

            # Get pending counts per mailbox
            pending_counts = (
                db.query(
                    DeliveryObligationModel.mailbox_id,
                    func.count(DeliveryObligationModel.inbox_row_id),
                )
                .filter(DeliveryObligationModel.state == "OPEN")
                .group_by(DeliveryObligationModel.mailbox_id)
                .all()
            )

            # F210 D16: an ESCALATED obligation whose message is still undelivered
            # keeps the indicator up. Escalation is the moment the message is most
            # urgent; counting OPEN alone unset @cao_pending exactly then.
            escalated_counts = (
                db.query(
                    DeliveryObligationModel.mailbox_id,
                    func.count(DeliveryObligationModel.inbox_row_id),
                )
                .join(InboxModel, InboxModel.id == DeliveryObligationModel.inbox_row_id)
                .join(MailboxModel, MailboxModel.id == DeliveryObligationModel.mailbox_id)
                .filter(
                    DeliveryObligationModel.state == "ESCALATED",
                    InboxModel.status != "delivered",
                    MailboxModel.consumed_through_id < DeliveryObligationModel.inbox_row_id,
                )
                .group_by(DeliveryObligationModel.mailbox_id)
                .all()
            )

            merged: dict[str, int] = {}
            for mailbox_id, count in list(pending_counts) + list(escalated_counts):
                merged[mailbox_id] = merged.get(mailbox_id, 0) + count

            # Get all mailboxes with their terminals and sessions
            active_mailboxes = set()
            for mailbox_id, count in merged.items():
                active_mailboxes.add(mailbox_id)
                target = resolve_supervisor_target(mailbox_id, db)
                if target.terminal_id and target.tmux_session:
                    boundary_pull_service.update_pending_count(
                        target.terminal_id,
                        target.tmux_session,
                        count,
                    )

            # Clear indicators for mailboxes with no undelivered obligations
            # (get all mailboxes that HAD state but now have count=0)
            all_supervisor_mailboxes = (
                db.query(MailboxModel).filter_by(role="supervisor").all()
            )
            for mailbox in all_supervisor_mailboxes:
                if mailbox.id not in active_mailboxes:
                    target = resolve_supervisor_target(mailbox.id, db)
                    if target.terminal_id and target.tmux_session:
                        boundary_pull_service.update_pending_count(
                            target.terminal_id,
                            target.tmux_session,
                            0,
                        )
    except Exception:
        logger.warning("fx194 _update_pending_indicators error", exc_info=True)


# ---------------------------------------------------------------------------
# FX194 D1b: Health warning — harness contract monitoring
# ---------------------------------------------------------------------------


def _check_health_warnings(escalate_after_s: float) -> None:
    """FX194 D1b: Emit health warnings for obligations crossing E without boundaries.

    Logs a warning distinguishing:
    - stuck_thinking: expected case, D2 interrupt will handle
    - harness_contract_broken: H1/H2 regression, boundaries not being surfaced

    F203 D17: Dedup — at most one WARN per (terminal_id, inbox_row_id, diagnosis)
    per escalate_after_s window. A diagnosis CHANGE emits a new one.
    """
    from cli_agent_orchestrator.services.boundary_pull_service import boundary_pull_service

    now = _utcnow()

    try:
        with SessionLocal() as db:
            # Find obligations crossing E
            from datetime import timedelta

            e_cutoff = now - timedelta(seconds=escalate_after_s)
            aging_obligations = (
                db.query(DeliveryObligationModel)
                .filter(
                    DeliveryObligationModel.state == "OPEN",
                    DeliveryObligationModel.accepted_at <= e_cutoff,
                )
                .all()
            )

            for obl in aging_obligations:
                target = resolve_supervisor_target(obl.mailbox_id, db)
                if target.terminal_id is None:
                    continue

                accepted = obl.accepted_at
                if accepted is not None and accepted.tzinfo is None:
                    accepted = accepted.replace(tzinfo=timezone.utc)
                age_s = (now - accepted).total_seconds() if accepted else 0.0

                warning = boundary_pull_service.check_health_warning(
                    target.terminal_id, age_s, escalate_after_s
                )
                if warning:
                    # D17: Dedup — only emit if (terminal, inbox_row, diagnosis)
                    # not already emitted within this window
                    dedup_key = (target.terminal_id, obl.inbox_row_id, warning)
                    last_emitted = _health_warning_dedup.get(dedup_key)
                    if last_emitted is not None:
                        elapsed = (now - last_emitted).total_seconds()
                        if elapsed < escalate_after_s:
                            continue  # suppress duplicate
                    _health_warning_dedup[dedup_key] = now

                    logger.warning(
                        "fx194_health_warning terminal=%s inbox=%d age=%.0fs "
                        "diagnosis=%s boundary_deliveries=0",
                        target.terminal_id,
                        obl.inbox_row_id,
                        age_s,
                        warning,
                    )
    except Exception:
        logger.debug("fx194 _check_health_warnings error", exc_info=True)


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
