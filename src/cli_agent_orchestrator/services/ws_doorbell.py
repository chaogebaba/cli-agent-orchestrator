"""W1 — WebSocket doorbell plane for supervisor callback wake.

Provides a lightweight localhost WS endpoint that pushes advisory frames
to armed supervisor terminals when inbox rows target them. The frame is
NOT authoritative — the single source of truth stays the inbox DB. The
model must call list_messages (triggering the drain hook) on frame receipt.

Flag-gated: only active when supervisor.wake.ws_monitor=True.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# Per-terminal WS connection tracking: terminal_id -> WebSocket
_connections: dict[str, WebSocket] = {}
_connections_lock = asyncio.Lock()


def is_ws_monitor_enabled() -> bool:
    """Return True when the ws_monitor flag is on."""
    return bool(ConfigService.get("supervisor.wake.ws_monitor", default=False))


async def register_connection(terminal_id: str, ws: WebSocket) -> None:
    """Register an active WS connection for a supervisor terminal."""
    async with _connections_lock:
        old = _connections.get(terminal_id)
        if old is not None:
            try:
                await old.close(code=4008, reason="superseded")
            except Exception:
                pass
        _connections[terminal_id] = ws
    logger.info("ws_doorbell armed terminal=%s", terminal_id)


async def unregister_connection(terminal_id: str, ws: WebSocket) -> None:
    """Unregister a WS connection (only if it is the current one).

    F158-R3/S1: Also abandons any pending delivered marks for this terminal
    to prevent leaked state when the terminal disconnects.
    """
    async with _connections_lock:
        current = _connections.get(terminal_id)
        if current is ws:
            del _connections[terminal_id]
    abandon_ws_delivered(terminal_id)
    logger.info("ws_doorbell disarmed terminal=%s", terminal_id)


def is_armed(terminal_id: str) -> bool:
    """Check if a terminal has an active WS doorbell connection (sync-safe)."""
    return terminal_id in _connections


async def push_doorbell_frame(
    terminal_id: str,
    message_id: int,
    sender_short: str,
    preview: str,
) -> bool:
    """Push an advisory doorbell frame to a connected supervisor terminal.

    Returns True if frame was sent, False if not armed or send failed.
    The frame is advisory only — it previews the callback but is NOT the
    authoritative message body.
    """
    async with _connections_lock:
        ws = _connections.get(terminal_id)
    if ws is None:
        return False

    # S3: frame is advisory digest, not content
    frame_text = f"[CAO] callback waiting: [{message_id}] from={sender_short} {preview[:120]}"
    try:
        await ws.send_text(frame_text)
        logger.debug(
            "ws_doorbell frame_sent terminal=%s msg_id=%d",
            terminal_id,
            message_id,
        )
        return True
    except (WebSocketDisconnect, RuntimeError, Exception) as e:
        logger.debug("ws_doorbell frame_send_failed terminal=%s: %s", terminal_id, e)
        # Connection is dead — unregister
        async with _connections_lock:
            if _connections.get(terminal_id) is ws:
                del _connections[terminal_id]
        return False


def push_doorbell_frame_sync(
    terminal_id: str,
    message_id: int,
    sender_short: str,
    preview: str,
    *,
    timeout: float = 0.5,
) -> bool:
    """Synchronous doorbell push that observes the actual send result.

    Posts the coroutine to the running event loop and waits up to *timeout*
    seconds for the real ws.send_text outcome. Returns True ONLY when the
    frame was actually delivered over the WebSocket. Returns False when the
    WS plane is disabled, unarmed, the loop is unavailable, the send raised,
    or the timeout expired.

    F158-R3: On timeout, the future is cancelled AND the (terminal_id, row_id)
    is added to the invalidation set. If the coroutine completes despite the
    cancel (already past its await point), the invalidation prevents a late
    mark_ws_delivered from being set — ensuring no late WS success can coexist
    with the native fallback that fires when this returns False.
    """
    if not is_ws_monitor_enabled():
        return False
    if not is_armed(terminal_id):
        return False

    # F158-R3: Detect same-loop caller. If we are ON the event loop that would
    # run the coroutine, scheduling + blocking deadlocks. Return False so the
    # native fallback fires without a 0.5s stall.
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    # Resolve the target loop for the send
    try:
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        target_loop = inbox_service._delivery_loop
        if target_loop is None or target_loop.is_closed():
            target_loop = None
    except Exception:
        target_loop = None

    if running_loop is not None and (target_loop is None or target_loop is running_loop):
        # Same-loop caller — cannot block; return False immediately
        return False

    loop = target_loop if target_loop is not None else running_loop
    if loop is None or loop.is_closed():
        return False

    future = asyncio.run_coroutine_threadsafe(
        push_doorbell_frame(terminal_id, message_id, sender_short, preview),
        loop,
    )
    try:
        result = future.result(timeout=timeout)
        return result
    except Exception:
        # Timeout, CancelledError, or coroutine exception → not delivered.
        # F158-R3: Cancel the future to prevent late delivery AND invalidate
        # so any already-in-flight completion cannot produce a mark.
        future.cancel()
        _invalidate_ws_send(terminal_id, message_id)
        return False


# ---------------------------------------------------------------------------
# F158-R3: WS-delivered dedup state + invalidation set
#
# After _f413_after_commit successfully delivers a WS frame, it marks the
# (terminal_id, row_id) pair here. The F136 post-delivery doorbell checks
# and consumes this mark to suppress the redundant native ring.
#
# The invalidation set records (terminal_id, row_id) pairs where a timeout
# fired and the native fallback was selected. If a late-completing WS send
# tries to mark delivery after the timeout, the invalidation prevents it.
#
# Thread-safe: accessed from the ORM after-commit thread and the async
# delivery loop.
# ---------------------------------------------------------------------------
import threading
import time

_ws_delivered_lock = threading.Lock()
_ws_delivered: dict[tuple[str, int], float] = {}  # key → timestamp

_ws_invalidated_lock = threading.Lock()
_ws_invalidated: dict[tuple[str, int], float] = {}  # key → timestamp

# Limit max entries; use targeted eviction based on age
_WS_DELIVERED_MAX = 2048
_WS_INVALIDATED_MAX = 2048
_WS_ENTRY_TTL = 30.0  # entries older than 30s are stale and evictable


def _evict_stale(store: dict[tuple[str, int], float], max_size: int) -> None:
    """Remove entries older than TTL; if still over max, remove oldest half."""
    now = time.monotonic()
    # Remove expired entries
    expired = [k for k, ts in store.items() if now - ts > _WS_ENTRY_TTL]
    for k in expired:
        del store[k]
    # If still over capacity, evict oldest half
    if len(store) >= max_size:
        sorted_keys = sorted(store, key=store.get)  # type: ignore[arg-type]
        for k in sorted_keys[: len(sorted_keys) // 2]:
            del store[k]


def _invalidate_ws_send(terminal_id: str, row_id: int) -> None:
    """Record that this (terminal_id, row_id) timed out and native fallback was chosen.

    Prevents a late-completing WS send from setting a delivered mark.
    """
    key = (terminal_id, row_id)
    with _ws_invalidated_lock:
        if len(_ws_invalidated) >= _WS_INVALIDATED_MAX:
            _evict_stale(_ws_invalidated, _WS_INVALIDATED_MAX)
        _ws_invalidated[key] = time.monotonic()


def _is_invalidated(terminal_id: str, row_id: int) -> bool:
    """Check if a send was invalidated (timed out). Does NOT consume."""
    key = (terminal_id, row_id)
    with _ws_invalidated_lock:
        return key in _ws_invalidated


def _consume_invalidation(terminal_id: str, row_id: int) -> None:
    """Consume (remove) an invalidation entry after it has been observed."""
    key = (terminal_id, row_id)
    with _ws_invalidated_lock:
        _ws_invalidated.pop(key, None)


def mark_ws_delivered(terminal_id: str, row_id: int) -> None:
    """Record that a WS advisory frame was delivered for this row.

    Called by _f413_after_commit when push_doorbell_frame_sync returns True.

    F158-R3: If the (terminal_id, row_id) was invalidated (timeout fired,
    native fallback chosen), the mark is NOT set — the late WS success is
    discarded to prevent it coexisting with the already-fired fallback.
    """
    key = (terminal_id, row_id)
    # Check invalidation FIRST
    if _is_invalidated(terminal_id, row_id):
        _consume_invalidation(terminal_id, row_id)
        return  # late success after timeout — discard
    with _ws_delivered_lock:
        if len(_ws_delivered) >= _WS_DELIVERED_MAX:
            _evict_stale(_ws_delivered, _WS_DELIVERED_MAX)
        _ws_delivered[key] = time.monotonic()


def consume_ws_delivered(terminal_id: str, row_id: int) -> bool:
    """Check and consume a WS-delivered mark.

    Returns True if the mark existed (WS already woke the supervisor for this
    row) — the caller should skip the native ring. The mark is consumed (removed)
    on True to prevent memory growth.

    F158-R3/S1: Also consumes any marks for this terminal with row_id <= the
    given row_id (earlier rows that were individually marked but whose max wasn't
    checked). This prevents leaked marks from batched deliveries.
    """
    key = (terminal_id, row_id)
    found = False
    with _ws_delivered_lock:
        if key in _ws_delivered:
            del _ws_delivered[key]
            found = True
        # S1: consume any earlier marks for this terminal (batch cleanup)
        stale = [k for k in _ws_delivered if k[0] == terminal_id and k[1] <= row_id]
        for k in stale:
            del _ws_delivered[k]
    return found


def abandon_ws_delivered(terminal_id: str) -> None:
    """Remove all delivered marks for a terminal (e.g. on disconnect/abandon).

    F158-R3/S1: Prevents leaked marks when a terminal disconnects, the loop
    closes, or F136 never reaches a successful write outcome for marked rows.
    """
    with _ws_delivered_lock:
        to_remove = [k for k in _ws_delivered if k[0] == terminal_id]
        for k in to_remove:
            del _ws_delivered[k]
    with _ws_invalidated_lock:
        to_remove = [k for k in _ws_invalidated if k[0] == terminal_id]
        for k in to_remove:
            del _ws_invalidated[k]
