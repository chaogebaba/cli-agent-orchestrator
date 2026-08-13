"""F168/FX170 — Supervisor doorbell service.

FX170 (native wake): resolve → version guard → socket write → verify wake.
Any refusal/failure falls back to the existing fx168 _attempt_gated_ring.
Single dedup cursor, never double-ring, never fail silent.

fx168 D1-D13 retained for the fallback path (pane nudge through the gate wall).
fx170 D1-D11: socket write to CC's per-session UDS, no pane touch.
"""

from __future__ import annotations

import logging
import time
import threading
from typing import Optional

from cli_agent_orchestrator.clients.database import get_terminal_metadata, update_terminal_metadata
from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# D6-as-amended (S1 fold): channel-neutral instruction that provokes a tool call.
DOORBELL_NUDGE_TEXT = "[cao] You have new callback message(s). Run any command to surface them."

# In-process fallback for last_doorbell_row_id (D4).
_last_doorbell_row_id: dict[str, int] = {}
_last_doorbell_lock = threading.Lock()

# D12: rate-limited WARN — one per terminal per 60s.
_last_warn_time: dict[str, float] = {}
_WARN_INTERVAL_S = 60.0


def _get_last_doorbell_row_id(terminal_id: str) -> int:
    """Read last_doorbell_row_id from terminal metadata, fallback to in-process dict."""
    try:
        metadata = get_terminal_metadata(terminal_id)
        if metadata:
            md = metadata.get("metadata") or {}
            stored = md.get("last_doorbell_row_id")
            if stored is not None:
                return int(stored)
    except Exception:
        pass
    with _last_doorbell_lock:
        return _last_doorbell_row_id.get(terminal_id, 0)


def _persist_last_doorbell_row_id(terminal_id: str, row_id: int) -> None:
    """Persist last_doorbell_row_id in terminal metadata (best-effort)."""
    with _last_doorbell_lock:
        _last_doorbell_row_id[terminal_id] = row_id
    try:
        metadata = get_terminal_metadata(terminal_id)
        if metadata:
            md = metadata.get("metadata") or {}
            md["last_doorbell_row_id"] = row_id
            update_terminal_metadata(terminal_id, md)
    except Exception as e:
        logger.debug("f168_doorbell persist failed for %s: %s", terminal_id, e)


def _rate_limited_warn(terminal_id: str, reason: str, row_id: int) -> None:
    """D12: rate-limited WARN log, at most one per terminal per 60s."""
    now = time.monotonic()
    last = _last_warn_time.get(terminal_id)
    if last is not None and (now - last) < _WARN_INTERVAL_S:
        return
    _last_warn_time[terminal_id] = now
    logger.warning(
        "f168_doorbell terminal=%s decision=error reason=%s row=%s",
        terminal_id, reason, row_id,
    )


def ring_supervisor_doorbell(
    terminal_id: str,
    max_written_row_id: int,
    *,
    written_count: int = 0,
) -> str:
    """Ring the supervisor after a callback write.

    FX170 order: resolve → version guard → socket write → verify wake.
    ANY refusal/failure falls back to fx168 _attempt_gated_ring (D4).
    Returns: rang, fallback, skipped_dedup, skipped_disabled, error.
    """
    # D10 (fx168): outer switch — off means no bell of any kind.
    if not ConfigService.get("supervisor.doorbell", default=True):
        logger.info(
            "f170_doorbell terminal=%s decision=skipped_disabled reason=flag_off row=%s",
            terminal_id, max_written_row_id,
        )
        return "skipped_disabled"

    # D4: cursor dedup — skip if already rang for this or higher row.
    if written_count <= 0:
        logger.info(
            "f170_doorbell terminal=%s decision=skipped_dedup reason=no_written row=%s",
            terminal_id, max_written_row_id,
        )
        return "skipped_dedup"

    last_row = _get_last_doorbell_row_id(terminal_id)
    if max_written_row_id <= last_row:
        logger.info(
            "f170_doorbell terminal=%s decision=skipped_dedup reason=row_not_higher row=%s",
            terminal_id, max_written_row_id,
        )
        return "skipped_dedup"

    # FX170 D1: attempt native socket ring first (D2: no _should_teammate_push gate).
    native_enabled = ConfigService.get("supervisor.wake.native", default=True)
    if native_enabled:
        try:
            decision = _attempt_native_ring(terminal_id, max_written_row_id)
        except Exception as exc:
            logger.debug("f170_doorbell native exception: %s", exc)
            decision = None

        if decision == "rang":
            _persist_last_doorbell_row_id(terminal_id, max_written_row_id)
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
        logger.info(
            "f170_doorbell terminal=%s decision=skipped_disabled "
            "reason=not_registered_fallback row=%s",
            terminal_id, max_written_row_id,
        )
        return "skipped_disabled"

    try:
        fallback_decision = _attempt_gated_ring(terminal_id, max_written_row_id)
    except Exception as exc:
        _rate_limited_warn(terminal_id, str(exc)[:120], max_written_row_id)
        return "error"

    if fallback_decision == "rang":
        _persist_last_doorbell_row_id(terminal_id, max_written_row_id)
        if native_refusal is not None:
            # Native was attempted but failed — this is a true fallback
            logger.info(
                "f170_doorbell terminal=%s decision=fallback transport=nudge "
                "reason=%s row=%s",
                terminal_id, native_refusal, max_written_row_id,
            )
            return "fallback"
        # Native was disabled — gated ring is the primary path
        logger.info(
            "f170_doorbell terminal=%s decision=rang transport=nudge reason=native_disabled row=%s",
            terminal_id, max_written_row_id,
        )
        return "rang"

    return fallback_decision


def _attempt_native_ring(terminal_id: str, max_written_row_id: int) -> Optional[str]:
    """FX170 D1: resolve → version guard → socket write → verify.

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
            "f170_doorbell terminal=%s decision=fallback transport=socket "
            "reason=%s row=%s",
            terminal_id, result.refusal_reason, max_written_row_id,
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
            terminal_id, ver_refusal, record.version, max_written_row_id,
        )
        return ver_refusal

    # D5: build payload
    # Worker name: use the terminal_id as the sender name
    payload = build_wake_payload(terminal_id, max_written_row_id)

    # D8: sample pre-write status for verification
    pre_status_updated_at = record.status_updated_at

    # D5: socket write
    write_err = write_to_socket(record.messaging_socket_path, payload)
    if write_err:
        logger.info(
            "f170_doorbell terminal=%s decision=fallback transport=socket "
            "reason=%s pid=%s ver=%s row=%s",
            terminal_id, write_err, record.pid, record.version, max_written_row_id,
        )
        return write_err

    # D8: verify wake
    woke = verify_wake(record, pre_status_updated_at)
    if not woke:
        logger.info(
            "f170_doorbell terminal=%s decision=fallback transport=socket "
            "reason=wake_unverified pid=%s ver=%s row=%s",
            terminal_id, record.pid, record.version, max_written_row_id,
        )
        return "wake_unverified"

    # Success
    logger.info(
        "f170_doorbell terminal=%s decision=rang transport=socket "
        "pid=%s ver=%s row=%s",
        terminal_id, record.pid, record.version, max_written_row_id,
    )
    return "rang"


def _attempt_gated_ring(terminal_id: str, max_written_row_id: int) -> str:
    """Run through the gate wall and ring if safe.

    D13: G1 (delivery_lock) and G2 (recovery_state) exclude the rebind window.
    G4-G8 are checked via probe + _inject_safe + send_prepared_input.
    """
    from cli_agent_orchestrator.services.inbox_service import (
        InjectSafetyResult,
        get_delivery_lock,
    )
    from cli_agent_orchestrator.services.receiver_state_view import native_probe
    from cli_agent_orchestrator.services.status_monitor import TerminalStatus, status_monitor
    from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError

    # G1: delivery-lock non-blocking acquire (rebind exclusion, D13).
    delivery_lock = get_delivery_lock(terminal_id)
    if not delivery_lock.acquire(blocking=False):
        logger.info(
            "f168_doorbell terminal=%s decision=skipped_gate reason=delivery_lock row=%s",
            terminal_id, max_written_row_id,
        )
        return "skipped_gate"
    try:
        # G2: recovery_state check.
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=no_metadata row=%s",
                terminal_id, max_written_row_id,
            )
            return "skipped_gate"

        md = metadata.get("metadata") or {}
        recovery_state = md.get("recovery_state")
        if recovery_state not in (None, "rebound"):
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=recovery_state row=%s",
                terminal_id, max_written_row_id,
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
                    probe_result = type("_TmuxProbe", (), {
                        "status": tmux_probe.status,
                        "meta": tmux_probe.meta,
                    })()
            except Exception:
                pass

        if probe_result is None:
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=probe_failed row=%s",
                terminal_id, max_written_row_id,
            )
            return "skipped_gate"

        # G6: status must be IDLE or COMPLETED for doorbell (no eager PROCESSING).
        if probe_result.status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=not_idle status=%s row=%s",
                terminal_id, probe_result.status.value if hasattr(probe_result.status, 'value') else probe_result.status, max_written_row_id,
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
            terminal_id, provider, probe_result.meta,
        )
        if safety.verdict == "veto":
            logger.info(
                "f168_doorbell terminal=%s decision=skipped_gate reason=%s row=%s",
                terminal_id, safety.reason, max_written_row_id,
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
                terminal_id, str(dde)[:60], max_written_row_id,
            )
            return "skipped_gate"

        logger.info(
            "f168_doorbell terminal=%s decision=rang source=%s row=%s",
            terminal_id, _evidence_source, max_written_row_id,
        )
        return "rang"
    finally:
        delivery_lock.release()
