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
) -> None:
    """Fire-and-forget doorbell push from synchronous code.

    Posts the coroutine to the running event loop. Best-effort: if the loop
    is unavailable, the frame is silently dropped (tier-2 still delivers).
    """
    if not is_ws_monitor_enabled():
        return
    if not is_armed(terminal_id):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop in this thread — try to find the server's loop
        try:
            from cli_agent_orchestrator.services.inbox_service import inbox_service

            loop = inbox_service._delivery_loop
            if loop is None or loop.is_closed():
                return
        except Exception:
            return

    asyncio.run_coroutine_threadsafe(
        push_doorbell_frame(terminal_id, message_id, sender_short, preview),
        loop,
    )
