"""F168 — Idle supervisor doorbell service.

Ring a supervisor's pane after a callback write, through the existing
eight-gate safety wall. One nudge per delivery run, cursor-deduped by
highest written row id (D4). File-write-first + best-effort isolation (D3).

D1:  The doorbell is a pane nudge through the existing gate wall.
D2:  Fires from _f136_post_delivery (lock-free).
D3:  Write commits first; nudge is best-effort, may never affect delivery.
D4:  One nudge per run, deduped by last_doorbell_row_id.
D5:  Refused gate = silent skip, never retry/queue.
D6:  Fixed instruction text (channel-neutral, provokes tool call).
D7:  Draft present => skip; never stashes.
D8:  Registered path only (_should_teammate_push gate).
D9:  Single helper, three call sites.
D10: supervisor.doorbell config flag, default on, subordinate to teammate_push.
D11: Attributed to cao-bridge, no attempt row.
D12: One log line per decision, rate-limited WARN.
D13: Rebind window excluded by G1/G2; gate wall refusal is structurally correct.
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
    """Ring the supervisor's pane after a callback write (D1-D13).

    Returns the decision string: rang, skipped_gate, skipped_dedup,
    skipped_disabled, or error.
    """
    # D10: config flag check, subordinate to teammate_push (D8).
    if not ConfigService.get("supervisor.doorbell", default=True):
        logger.info(
            "f168_doorbell terminal=%s decision=skipped_disabled reason=flag_off row=%s",
            terminal_id, max_written_row_id,
        )
        return "skipped_disabled"

    # D8: registered path only — same gate as teammate_push.
    from cli_agent_orchestrator.services.teammate_push_service import _should_teammate_push

    if not _should_teammate_push(terminal_id):
        logger.info(
            "f168_doorbell terminal=%s decision=skipped_disabled reason=not_registered row=%s",
            terminal_id, max_written_row_id,
        )
        return "skipped_disabled"

    # D4: cursor dedup — skip if already rang for this or higher row.
    if written_count <= 0:
        logger.info(
            "f168_doorbell terminal=%s decision=skipped_dedup reason=no_written row=%s",
            terminal_id, max_written_row_id,
        )
        return "skipped_dedup"

    last_row = _get_last_doorbell_row_id(terminal_id)
    if max_written_row_id <= last_row:
        logger.info(
            "f168_doorbell terminal=%s decision=skipped_dedup reason=row_not_higher row=%s",
            terminal_id, max_written_row_id,
        )
        return "skipped_dedup"

    # D1/D13: attempt ring through the existing gate wall.
    try:
        decision = _attempt_gated_ring(terminal_id, max_written_row_id)
    except Exception as exc:
        # D3: isolation — any exception is caught and logged.
        _rate_limited_warn(terminal_id, str(exc)[:120], max_written_row_id)
        return "error"

    # D4: advance high-water only on successful ring.
    if decision == "rang":
        _persist_last_doorbell_row_id(terminal_id, max_written_row_id)

    return decision


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
        probe_result = native_probe(terminal_id, status_monitor)
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
            "f168_doorbell terminal=%s decision=rang row=%s",
            terminal_id, max_written_row_id,
        )
        return "rang"
    finally:
        delivery_lock.release()
