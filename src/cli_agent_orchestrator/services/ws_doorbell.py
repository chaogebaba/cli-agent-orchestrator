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
    """Unregister a WS connection (only if it is the current one)."""
    async with _connections_lock:
        current = _connections.get(terminal_id)
        if current is ws:
            del _connections[terminal_id]
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

    F158-R2: The bounded wait makes transport-drop failures observable to
    the fallback decision in _f413_after_commit — a drop after the armed
    check now correctly returns False so the native fallback fires.
    """
    if not is_ws_monitor_enabled():
        return False
    if not is_armed(terminal_id):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop in this thread — try to find the server's loop
        try:
            from cli_agent_orchestrator.services.inbox_service import inbox_service

            loop = inbox_service._delivery_loop
            if loop is None or loop.is_closed():
                return False
        except Exception:
            return False

    future = asyncio.run_coroutine_threadsafe(
        push_doorbell_frame(terminal_id, message_id, sender_short, preview),
        loop,
    )
    try:
        return future.result(timeout=timeout)
    except Exception:
        # Timeout, CancelledError, or coroutine exception → not delivered
        return False


# ---------------------------------------------------------------------------
# F158-R2: WS-delivered dedup state
#
# After _f413_after_commit successfully delivers a WS frame, it marks the
# (terminal_id, row_id) pair here. The F136 post-delivery doorbell checks
# and consumes this mark to suppress the redundant native ring.
# Thread-safe: accessed from the ORM after-commit thread and the async
# delivery loop.
# ---------------------------------------------------------------------------
import threading

_ws_delivered_lock = threading.Lock()
_ws_delivered: set[tuple[str, int]] = set()

# Limit max entries to prevent unbounded growth (entries are consumed quickly)
_WS_DELIVERED_MAX = 4096


def mark_ws_delivered(terminal_id: str, row_id: int) -> None:
    """Record that a WS advisory frame was delivered for this row.

    Called by _f413_after_commit when push_doorbell_frame_sync returns True.
    """
    with _ws_delivered_lock:
        if len(_ws_delivered) >= _WS_DELIVERED_MAX:
            # Evict oldest half (unordered — just clear; delivery is fast enough)
            _ws_delivered.clear()
        _ws_delivered.add((terminal_id, row_id))


def consume_ws_delivered(terminal_id: str, row_id: int) -> bool:
    """Check and consume a WS-delivered mark.

    Returns True if the mark existed (WS already woke the supervisor for this
    row) — the caller should skip the native ring. The mark is consumed (removed)
    on True to prevent memory growth.
    """
    key = (terminal_id, row_id)
    with _ws_delivered_lock:
        if key in _ws_delivered:
            _ws_delivered.discard(key)
            return True
    return False
