"""F168/FX170 — Supervisor doorbell service.

FX170 (native wake): resolve → version guard → socket write → verify wake.
Any refusal/failure falls back to the existing fx168 _attempt_gated_ring.
Single dedup cursor, never double-ring, never fail silent.

fx168 D1-D13 retained for the fallback path (pane nudge through the gate wall).
fx170 D1-D11: socket write to CC's per-session UDS, no pane touch.

F476 D8: The doorbell is a transport of path 2, not an independent waker.
It fires only from the push cycle's max_written_row_id and inherits path 2's
D3 claim. Its private cursor is removed; the F457
still-pending check stays (consumed-check, not a wake cursor).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from cli_agent_orchestrator.clients.database import (
    get_terminal_last_doorbell_row_id,
    get_terminal_metadata,
    set_terminal_last_doorbell_row_id,
)
from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# D6-as-amended (S1 fold): channel-neutral instruction that provokes a tool call.
DOORBELL_NUDGE_TEXT = "[cao] You have new callback message(s). Run any command to surface them."

# D12: rate-limited WARN — one per terminal per 60s.
_last_warn_time: dict[str, float] = {}
_WARN_INTERVAL_S = 60.0


# F476 D8: Stubs for removed cursor functions — retained as patch targets.
_last_doorbell_row_id: dict[str, int] = {}
_last_doorbell_lock = threading.Lock()


def _get_last_doorbell_row_id(terminal_id: str) -> int:  # pragma: no cover — stub
    """F476: STUB — returns 0. Retained as patch target for legacy tests."""
    with _last_doorbell_lock:
        return _last_doorbell_row_id.get(terminal_id, 0)


def _persist_last_doorbell_row_id(terminal_id: str, row_id: int) -> None:  # pragma: no cover
    """F476: STUB — no-op. Retained as patch target for legacy tests."""
    with _last_doorbell_lock:
        _last_doorbell_row_id[terminal_id] = row_id


def _rate_limited_warn(terminal_id: str, reason: str, row_id: int) -> None:
    """D12: rate-limited WARN log, at most one per terminal per 60s."""
    now = time.monotonic()
    last = _last_warn_time.get(terminal_id)
    if last is not None and (now - last) < _WARN_INTERVAL_S:
        return
    _last_warn_time[terminal_id] = now
    logger.warning(
        "f168_doorbell terminal=%s decision=error reason=%s row=%s",
        terminal_id,
        reason,
        row_id,
    )


def _is_row_still_pending(row_id: int) -> bool:
    """F457: Re-check whether a specific inbox row is still PENDING at ring time.

    Returns True if the row exists and is PENDING, False otherwise (acked/delivered/gone).
    Fail-open on DB errors to avoid blocking the doorbell on transient failures.
    """
    try:
        from cli_agent_orchestrator.clients.database import SessionLocal
        from cli_agent_orchestrator.models.database import InboxModel
        from cli_agent_orchestrator.models.inbox import MessageStatus

        with SessionLocal() as db:
            status = db.query(InboxModel.status).filter(InboxModel.id == row_id).scalar()
            if status is None:
                return False
            return status == MessageStatus.PENDING.value
    except Exception:
        # Fail-open: if DB is unavailable, allow the ring to proceed.
        return True


def _mark_socket_delivered(row_id: int) -> None:
    """F459: Record that this row was socket-delivered via native bridge message.

    Stores a timestamp in the inbox_message_trace table so the drain hook can
    detect rows that already reached the supervisor via the native channel and
    skip re-injecting them as a duplicate digest.
    """
    try:
        from cli_agent_orchestrator.clients.database import (
            InboxMessageTraceEventModel,
            SessionLocal,
        )

        with SessionLocal() as db:
            db.add(
                InboxMessageTraceEventModel(
                    message_id=row_id,
                    kind="f459.socket_delivered",
                    phase="socket_delivered",
                    decision="proceed",
                    reason=None,
                    payload={},
                )
            )
            db.commit()
    except Exception:
        logger.debug("f459 mark_socket_delivered failed for row %s", row_id, exc_info=True)


def is_socket_delivered(row_id: int) -> bool:
    """F459: Check whether a row was already socket-delivered via native bridge.

    Used by the drain hook to suppress duplicate surfaces.
    """
    try:
        from cli_agent_orchestrator.clients.database import (
            InboxMessageTraceEventModel,
            SessionLocal,
        )

        with SessionLocal() as db:
            exists = (
                db.query(InboxMessageTraceEventModel.id)
                .filter(
                    InboxMessageTraceEventModel.message_id == row_id,
                    InboxMessageTraceEventModel.kind == "f459.socket_delivered",
                )
                .first()
            )
            return exists is not None
    except Exception:
        return False


def ring_supervisor_doorbell(
    terminal_id: str,
    max_written_row_id: int,
    *,
    written_count: int = 0,
    caller_holds_no_delivery_lock: bool = False,
    message_body: str | None = None,
    sender_display_name: str | None = None,
) -> str:
    """Ring the supervisor after a callback write.

    FX170 order: resolve → version guard → socket write → verify wake.
    ANY refusal/failure falls back to fx168 _attempt_gated_ring (D4).
    Returns: rang, fallback, skipped_dedup, skipped_disabled, error.

    F186: caller_holds_no_delivery_lock=True skips G1 in the fallback path.
    F459: message_body/sender_display_name carry the worker's actual callback
    text and display name through to the native bridge message.
    """
    # D10 (fx168): outer switch — off means no bell of any kind.
    if not ConfigService.get("supervisor.doorbell", default=True):
        logger.info(
            "f170_doorbell terminal=%s decision=skipped_disabled reason=flag_off row=%s",
            terminal_id,
            max_written_row_id,
        )
        return "skipped_disabled"

    # F476 D8: No cursor dedup — doorbell is a transport of path 2's claim.
    # Only skip if nothing was written this cycle.
    if written_count <= 0:
        logger.info(
            "f170_doorbell terminal=%s decision=skipped_dedup reason=no_written row=%s",
            terminal_id,
            max_written_row_id,
        )
        return "skipped_dedup"

    # F457: acked-row dedupe — skip the wake if the row is no longer PENDING.
    if not _is_row_still_pending(max_written_row_id):
        logger.info(
            "f170_doorbell terminal=%s decision=skipped_acked reason=row_not_pending row=%s",
            terminal_id,
            max_written_row_id,
        )
        return "skipped_acked"

    # FX170 D1: attempt native socket ring first (D2: no _should_teammate_push gate).
    native_enabled = ConfigService.get("supervisor.wake.native", default=True)
    if native_enabled:
        try:
            decision = _attempt_native_ring(
                terminal_id,
                max_written_row_id,
                message_body=message_body,
                sender_display_name=sender_display_name,
            )
        except Exception as exc:
            logger.debug("f170_doorbell native exception: %s", exc)
            decision = None

        if decision == "rang":
            # F459: mark row as socket-delivered (best-effort)
            if message_body is not None:
                try:
                    _mark_socket_delivered(max_written_row_id)
                except Exception:
                    pass
            return "rang"
        # decision is None or a refusal reason — fall through to fx168
        native_refusal = decision
    else:
        native_refusal = None

    # D4 fallback: fx168 pane nudge.
    # D8 (fx168): the fallback path gates on _should_teammate_push because the
    # pane nudge content lives in the file — meaningless if file was never written.
    from cli_agent_orchestrator.services.teammate_push_service import _should_teammate_push

    if not _should_teammate_push(terminal_id):
        # F203 D9/AC9: counted ejection for fallback ring
        from cli_agent_orchestrator.services.transport_ejection import (
            transport_ejection_service,
        )

        transport_ejection_service.record_refusal(
            terminal_id, "fallback", "not_registered_fallback"
        )
        logger.info(
            "f170_doorbell terminal=%s decision=skipped_disabled "
            "reason=not_registered_fallback row=%s",
            terminal_id,
            max_written_row_id,
        )
        return "skipped_disabled"

    try:
        fallback_decision = _attempt_gated_ring(
            terminal_id,
            max_written_row_id,
            caller_holds_no_delivery_lock=caller_holds_no_delivery_lock,
        )
    except Exception as exc:
        _rate_limited_warn(terminal_id, str(exc)[:120], max_written_row_id)
        return "error"

    if fallback_decision == "rang":
        if native_refusal is not None:
            # Native was attempted but failed — this is a true fallback
            logger.info(
                "f170_doorbell terminal=%s decision=fallback transport=nudge " "reason=%s row=%s",
                terminal_id,
                native_refusal,
                max_written_row_id,
            )
            return "fallback"
        # Native was disabled — gated ring is the primary path
        logger.info(
            "f170_doorbell terminal=%s decision=rang transport=nudge reason=native_disabled row=%s",
            terminal_id,
            max_written_row_id,
        )
        return "rang"

    return fallback_decision


def _attempt_native_ring(
    terminal_id: str,
    max_written_row_id: int,
    *,
    message_body: str | None = None,
    sender_display_name: str | None = None,
) -> Optional[str]:
    """FX170 D1: resolve → version guard → socket write → verify.

    F459: message_body/sender_display_name passed through to build_wake_payload.
    Returns "rang" on success, a refusal reason string on failure, or None
    if resolution cannot proceed (triggers fallback).
    """
    from cli_agent_orchestrator.services.cc_session_registry import (
        ResolveResult,
        build_wake_payload,
        check_version_guard,
        resolve_target,
        verify_wake,
        write_to_socket,
    )

    # Get terminal's tmux coordinates
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        return "no_terminal_metadata"

    tmux_session = metadata.get("tmux_session", "")
    tmux_window = metadata.get("tmux_window", "")
    if not tmux_session or not tmux_window:
        return "no_tmux_coordinates"

    # D3: resolve target
    result: ResolveResult = resolve_target(terminal_id, tmux_session, tmux_window)
    if result.refusal_reason:
        logger.info(
            "f170_doorbell terminal=%s decision=fallback transport=socket " "reason=%s row=%s",
            terminal_id,
            result.refusal_reason,
            max_written_row_id,
        )
        return result.refusal_reason

    record = result.record
    assert record is not None  # guaranteed by no refusal_reason

    # D6: version guard
    ver_refusal = check_version_guard(record)
    if ver_refusal:
        logger.info(
            "f170_doorbell terminal=%s decision=fallback transport=socket "
            "reason=%s ver=%s row=%s",
            terminal_id,
            ver_refusal,
            record.version,
            max_written_row_id,
        )
        return ver_refusal

    # D5: build payload
    # F459: pass message_body and sender_display_name for payload-carrying wake
    payload = build_wake_payload(
        terminal_id,
        max_written_row_id,
        message_body=message_body,
        sender_display_name=sender_display_name,
    )

    # D8: sample pre-write status for verification
    pre_status_updated_at = record.status_updated_at

    # F216: gate — refuse BEFORE any connect attempt when socket path is empty.
    # CC cross-session gate may be remotely off → messagingSocketPath:null in JSON.
    if not record.messaging_socket_path:
        logger.info(
            "f170_doorbell terminal=%s decision=fallback transport=socket "
            "reason=socket_unpublished pid=%s ver=%s row=%s",
            terminal_id,
            record.pid,
            record.version,
            max_written_row_id,
        )
        return "socket_unpublished"

    # D5: socket write
    write_err = write_to_socket(record.messaging_socket_path, payload)
    if write_err:
        logger.info(
            "f170_doorbell terminal=%s decision=fallback transport=socket "
            "reason=%s pid=%s ver=%s row=%s",
            terminal_id,
            write_err,
            record.pid,
            record.version,
            max_written_row_id,
        )
        return write_err

    # D8: verify wake
    woke = verify_wake(record, pre_status_updated_at)
    if not woke:
        logger.info(
            "f170_doorbell terminal=%s decision=fallback transport=socket "
            "reason=wake_unverified pid=%s ver=%s row=%s",
            terminal_id,
            record.pid,
            record.version,
            max_written_row_id,
        )
        return "wake_unverified"

    # Success
    logger.info(
        "f170_doorbell terminal=%s decision=rang transport=socket " "pid=%s ver=%s row=%s",
        terminal_id,
        record.pid,
        record.version,
        max_written_row_id,
    )
    return "rang"


def _attempt_gated_ring(
    terminal_id: str, max_written_row_id: int, *, caller_holds_no_delivery_lock: bool = False
) -> str:
    """Run through the gate wall and ring if safe.

    D13: G1 (delivery_lock) and G2 (recovery_state) exclude the rebind window.
    G4-G8 are checked via probe + _inject_safe + send_prepared_input.

    F186: when caller_holds_no_delivery_lock=True, G1 is skipped — the caller
    is provably outside the delivery-lock scope so the rebind-exclusion concern
    does not apply.
    """
    from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
    from cli_agent_orchestrator.services.inbox_service import (
        InjectSafetyResult,
        get_delivery_lock,
    )
    from cli_agent_orchestrator.services.receiver_state_view import native_probe
    from cli_agent_orchestrator.services.status_monitor import TerminalStatus, status_monitor

    # G1: delivery-lock non-blocking acquire (rebind exclusion, D13).
    # F186: skip G1 when caller provably holds no delivery lock (reconciler path).
    if caller_holds_no_delivery_lock:
        _owns_lock = False
    else:
        delivery_lock = get_delivery_lock(terminal_id)
        if not delivery_lock.acquire(blocking=False):
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=delivery_lock row=%s",
                terminal_id,
                max_written_row_id,
            )
            return "skipped_gate"
        _owns_lock = True
    try:
        # G2: recovery_state check.
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=no_metadata row=%s",
                terminal_id,
                max_written_row_id,
            )
            return "skipped_gate"

        md = metadata.get("metadata") or {}
        recovery_state = md.get("recovery_state")
        if recovery_state not in (None, "rebound"):
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=recovery_state row=%s",
                terminal_id,
                max_written_row_id,
            )
            return "skipped_gate"

        # G4: native probe — require fresh, max_age_s=2.0.
        # fx168 FIX-5: tmux-compatible fallback. native_probe returns None when
        # native_status_source != "herdr" (i.e. on the default tmux backend).
        # Fall back to probe_screen_status — the same idle-evidence mechanism
        # deliver_pending uses daily on tmux — preserving the gate's safety intent.
        probe_result = native_probe(terminal_id, status_monitor)
        _evidence_source = "native"
        if probe_result is None:
            # Tmux fallback: use screen-classification probe (proven daily in deliver_pending)
            try:
                tmux_probe = status_monitor.probe_screen_status(terminal_id)
                if tmux_probe is not None and hasattr(tmux_probe, "status"):
                    _evidence_source = "tmux"
                    # Adapt ProbeResult to the shape _attempt_gated_ring expects
                    probe_result = type(
                        "_TmuxProbe",
                        (),
                        {
                            "status": tmux_probe.status,
                            "meta": tmux_probe.meta,
                        },
                    )()
            except Exception:
                pass

        if probe_result is None:
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=probe_failed row=%s",
                terminal_id,
                max_written_row_id,
            )
            return "skipped_gate"

        # G6: status must be IDLE or COMPLETED for doorbell (no eager PROCESSING).
        if probe_result.status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=not_idle status=%s row=%s",
                terminal_id,
                (
                    probe_result.status.value
                    if hasattr(probe_result.status, "value")
                    else probe_result.status
                ),
                max_written_row_id,
            )
            return "skipped_gate"

        # G5: _inject_safe pre-open verdict.
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        provider = None
        try:
            from cli_agent_orchestrator.providers.manager import provider_manager

            provider = provider_manager.get_provider(terminal_id)
        except Exception:
            pass

        safety: InjectSafetyResult = inbox_service._inject_safe(
            terminal_id,
            provider,
            probe_result.meta,
        )
        if safety.verdict == "veto":
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=%s row=%s",
                terminal_id,
                safety.reason,
                max_written_row_id,
            )
            return "skipped_gate"

        # G7/G8: send through send_prepared_input (identity proof + draft guard).
        # D7: defer_on_dialog=True so draft presence causes DeliveryDeferredError
        # rather than stash/restore.
        from cli_agent_orchestrator.services.terminal_service import send_prepared_input

        try:
            send_prepared_input(
                terminal_id,
                DOORBELL_NUDGE_TEXT,
                defer_on_dialog=True,
                sender_id="cao-bridge",
                # D11: no orchestration_type — no attempt row, no PostSendMessageEvent.
                orchestration_type=None,
            )
        except DeliveryDeferredError as dde:
            # D7: draft present or dialog hazard — skip.
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=deferred_%s row=%s",
                terminal_id,
                str(dde)[:60],
                max_written_row_id,
            )
            return "skipped_gate"

        logger.info(
            "f168_doorbell terminal=%s decision=rang source=%s row=%s",
            terminal_id,
            _evidence_source,
            max_written_row_id,
        )
        return "rang"
    finally:
        if _owns_lock:
            delivery_lock.release()
