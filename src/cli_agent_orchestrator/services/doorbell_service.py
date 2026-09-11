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
    get_terminal_metadata,
)
from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# D6-as-amended (S1 fold): channel-neutral instruction that provokes a tool call.
DOORBELL_NUDGE_TEXT = "[cao] You have new callback message(s). Run any command to surface them."

# D12: rate-limited WARN — one per terminal per 60s.
_last_warn_time: dict[str, float] = {}
_WARN_INTERVAL_S = 60.0


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


def _queue_owns_delivery() -> bool:
    """Is sub-phase 3b's write-through position live? (D6's muting.)

    Asked through the delivery wiring rather than the environment: the boot guard
    can demote a requested position, and a surface that read the variable itself
    could mute on a position the guard already refused. Never raises — a wiring
    module that cannot answer leaves legacy behaving exactly as it does today,
    which is the safe direction for a mute.
    """
    try:
        from cli_agent_orchestrator.services.queue_carrier import queue_owns_delivery

        return queue_owns_delivery()
    except Exception:  # pragma: no cover — an unimportable switch is "not on"
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
    # WP-ARCH 3b: K3 is MUTED while the queue owns delivery (D6, §7b).
    #
    # The module is still here — 3c deletes it — so the mute is what stops it
    # emitting, and case 17's emitter count is what tests the mute. A second
    # emitter in the `on` arm is a LEAKY MUTE rather than a missing deletion.
    # `drain` deliberately does not mute: there the tick finishes rows already
    # enqueued while NEW traffic goes back to the legacy inbox, so muting would
    # leave that traffic with no carrier at all (§6).
    if _queue_owns_delivery():
        logger.info(
            "f170_doorbell terminal=%s decision=skipped_muted reason=queue_owns_delivery row=%s",
            terminal_id,
            max_written_row_id,
        )
        return "skipped_disabled"

    # D10 (fx168): outer switch — off means no bell of any kind.
    if not ConfigService.get("supervisor.doorbell"):
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
    from cli_agent_orchestrator.services.cc_session_registry import WAKE_NATIVE_DEFAULT

    native_enabled = ConfigService.get("supervisor.wake.native", default=WAKE_NATIVE_DEFAULT)
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
            # F459: mark row as socket-delivered (best-effort).
            # F803 #660 r2: socket_delivered is TRANSPORT truth — the native
            # ring returned "rang", i.e. the socket write succeeded — and is NOT
            # body-gated. The drain hook's duplicate-suppression is driven by the
            # CONSUMPTION decision (NATIVE SUCCEEDED / row flip), which is the
            # body-gated part and is recorded inside `_attempt_native_ring` keyed
            # on `body_carried`. An ids-only wake there records `wake_only`
            # (non-muting), so the hook still surfaces the text — marking the
            # transport signal here does not starve it. (The r1 body-gate on this
            # marker broke the F547 rung-1 socket_delivered pin.)
            try:
                _mark_socket_delivered(max_written_row_id)
            except Exception:
                pass
            # F783 #640: consumption is NOT recorded here. It attaches at the
            # socket-WRITE success inside `_attempt_native_ring` (which fires for
            # a body-carrying wake even when verify_wake later reports
            # wake_unverified — a busy seat still rendered the body). Recording
            # it on the verified "rang" return would miss every busy-seat
            # delivery, which the live traces show is the common case.
            return "rang"
        # decision is None or a refusal reason — fall through to fx168.
        # F783 #640 point 3: NATIVE FAILED for a body-carrying attempt is
        # recorded INSIDE `_attempt_native_ring` at its socket-write-failure arm
        # (the one place that knows write vs verify). A pre-write refusal
        # (no metadata / no coordinates / resolve / version / socket_unpublished)
        # is recorded there too via the same failure recorder when a body was in
        # flight, so the fallback stays auditable without double-recording here.
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
        # F810 (#667) D3: when native refused with `socket_unpublished` AND the
        # fallback rung is not registered, the seat is unreachable on BOTH rings
        # — the exact silent-ejection combination the evidence pack shows (182
        # deferred attempts, no operator signal). Surface ONE typed
        # `native_unreachable` fleet condition per ejection episode (idempotent
        # inside the service), rather than deferring silently. Best-effort: an
        # emit failure never changes the delivery decision below.
        if native_refusal == "socket_unpublished":
            try:
                transport_ejection_service.emit_native_unreachable(
                    terminal_id, "fallback"
                )
            except Exception:
                pass
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
        normalize_wake_body,
        read_peer_token,
        resolve_target,
        verify_wake,
        write_to_socket,
    )

    # F803 #660: the AUTHORITATIVE "did the socket carry the body?" signal is the
    # SAME normalization `build_wake_payload` applies to its own body argument —
    # `normalize_wake_body`. A non-None `message_body` argument does NOT mean the
    # seat received text: with `teammate_push=false` the coalescer synthesizes a
    # `[cao-fleet]` digest that is still an ids-only wake to the seat, and a
    # `[CONDITION]`/`[watchdog]` body collapses to None here (F790). Consumption
    # (F783) may attach ONLY when the body is actually carried; an ids-only ring
    # records a `wake_only` NATIVE emission that leaves the row pending for the
    # drain hook. This is the single decision point both arms below key off.
    body_carried = normalize_wake_body(message_body) is not None

    def _f783_note_native_failure(reason: str) -> None:
        """F783 #640 point 3: record a typed NATIVE FAILED emission for a
        body-carrying attempt that could not deliver, so the fallback
        (doorbell -> re-push -> hook) is auditable. No-op for a bodyless ids-only
        ping (not a missed body delivery)."""
        if not body_carried:
            return
        try:
            from cli_agent_orchestrator.services.mailbox_service import (
                record_native_delivery_failure,
            )

            record_native_delivery_failure(max_written_row_id, reason)
        except Exception:
            logger.debug(
                "f783 record-native-failure (%s) failed row %s",
                reason,
                max_written_row_id,
                exc_info=True,
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
    # F547 #403 point 1: bind msg_id to the receiver's live process incarnation
    # (procStart, else pid) so a re-push for the same row+seat is a stable,
    # de-dupable id rather than a fresh uuid4 every ring.
    incarnation = str(record.proc_start if record.proc_start is not None else record.pid)
    payload = build_wake_payload(
        terminal_id,
        max_written_row_id,
        message_body=message_body,
        sender_display_name=sender_display_name,
        incarnation=incarnation,
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
        _f783_note_native_failure("socket_unpublished")
        return "socket_unpublished"

    # F337: read auth token from per-session key file
    # F337-r2 B2: bind to the resolved process incarnation
    auth_token = read_peer_token(record.pid, expected_proc_start=record.proc_start)

    # D5: socket write (F337: auth handshake first line when token available)
    write_err = write_to_socket(record.messaging_socket_path, payload, auth_token=auth_token)
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
        # F783 #640 point 3: a native BODY envelope that failed to write leaves
        # the id pending and records a typed NATIVE FAILED emission so the
        # fallback (doorbell -> re-push -> hook) is auditable.
        _f783_note_native_failure(str(write_err))
        return write_err

    # F783 #640: THE BODY ENVELOPE HAS BEEN WRITTEN TO THE SEAT SOCKET.
    #
    # This is the "bridge's successful body envelope write" the contract counts
    # as consumption (user decision 2026-09-06): a native cross-session-message
    # that carried the callback body is guaranteed to render in the seat's TUI
    # — "as long as it showed up in the TUI screen it should be counted as a
    # consumed message" — EVEN IF verify_wake below cannot confirm it (a busy /
    # compacting seat does not move statusUpdatedAt, and wake_unverified is the
    # common live outcome). So consumption attaches HERE, at write success, not
    # at the verified "rang" return — otherwise every busy-seat delivery would
    # be missed.
    #
    # F803 #660: consumption keys off `body_carried` (== normalize_wake_body(...)
    # is not None), NOT the raw `message_body` argument. An ids-only wake ping
    # (teammate_push=false digest, or a [CONDITION]/[watchdog] body collapsed by
    # F790) reaches the seat as ids + a count and NO text, so the seat still
    # needs the drain to surface the body: the row must stay PENDING. Recording
    # consumption there was the #660 defect — it flipped the row to
    # native_consumed and starved the hook. Instead we record a `wake_only`
    # NATIVE emission (the wake DID fire) that leaves the row pending and does
    # NOT mute the hook.
    if body_carried:
        try:
            from cli_agent_orchestrator.services.mailbox_service import (
                consume_on_native_delivery,
            )

            consume_on_native_delivery(max_written_row_id)
        except Exception:
            logger.debug(
                "f783 consume-on-native (write success) failed row %s",
                max_written_row_id,
                exc_info=True,
            )
    else:
        try:
            from cli_agent_orchestrator.services.mailbox_service import (
                record_native_wake_only,
            )

            record_native_wake_only(max_written_row_id)
        except Exception:
            logger.debug(
                "f803 record-native-wake-only (write success) failed row %s",
                max_written_row_id,
                exc_info=True,
            )

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
