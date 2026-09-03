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
import threading
import time
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

    F158-R4/S1: Only abandons delivered marks when the unregistered socket
    was ACTUALLY the current connection. A superseded old socket teardown
    must NOT clear marks belonging to the replacement connection.
    """
    was_current = False
    async with _connections_lock:
        current = _connections.get(terminal_id)
        if current is ws:
            del _connections[terminal_id]
            was_current = True
    if was_current:
        abandon_ws_delivered(terminal_id)
    logger.info("ws_doorbell disarmed terminal=%s was_current=%s", terminal_id, was_current)


def is_armed(terminal_id: str) -> bool:
    """Check if a terminal has an active WS doorbell connection (sync-safe)."""
    return terminal_id in _connections


# ---------------------------------------------------------------------------
# F158-R4/B1: Per-send cancellation tokens.
#
# Each push_doorbell_frame_sync call creates a threading.Event as a
# "send permit". The coroutine checks this permit BEFORE calling send_text.
# On timeout, the sync wrapper CLEARS the permit, guaranteeing the coroutine
# will NOT emit a frame even if asyncio cancellation fails to stop it.
# ---------------------------------------------------------------------------
_send_permits_lock = threading.Lock()
_send_permits: dict[tuple[str, int], threading.Event] = {}


async def _guarded_push_doorbell_frame(
    terminal_id: str,
    message_id: int,
    sender_short: str,
    preview: str,
    permit: threading.Event,
) -> bool:
    """Push an advisory doorbell frame, gated by a permit.

    F158-R5/B1: The permit is a threading.Event shared with the sync wrapper.
    When the sync wrapper's timeout fires, it clears the permit. This coroutine
    checks the permit BEFORE calling send_text (no yield between check and call).

    If the coroutine is cancelled (CancelledError injected), it propagates —
    the coroutine terminates and the sync wrapper observes that it didn't send.
    """
    # Check permit BEFORE acquiring connection lock
    if not permit.is_set():
        return False

    async with _connections_lock:
        ws = _connections.get(terminal_id)
    if ws is None:
        return False

    # CRITICAL: Check permit IMMEDIATELY before send_text.
    # No await between this check and the send_text call.
    if not permit.is_set():
        return False

    frame_text = f"[CAO] callback waiting: [{message_id}] from={sender_short} {preview[:120]}"
    try:
        await ws.send_text(frame_text)
    except asyncio.CancelledError:
        # Propagate cancellation — do NOT catch it. The frame did NOT complete.
        raise
    except (WebSocketDisconnect, RuntimeError, Exception) as e:
        logger.debug("ws_doorbell frame_send_failed terminal=%s: %s", terminal_id, e)
        async with _connections_lock:
            if _connections.get(terminal_id) is ws:
                del _connections[terminal_id]
        return False

    # send_text completed without exception — frame was emitted.
    logger.debug("ws_doorbell frame_sent terminal=%s msg_id=%d", terminal_id, message_id)
    return True


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

    NOTE: This unguarded version is used only by callers that don't need
    cancellation semantics (e.g. tests). Production uses _guarded_push via
    push_doorbell_frame_sync.
    """
    async with _connections_lock:
        ws = _connections.get(terminal_id)
    if ws is None:
        return False

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
    """Synchronous doorbell push that arbitrates the WS/native winner.

    F158-R5/B1: This function is the SINGLE arbitration point. It does not
    return until the submitted coroutine has TERMINATED (succeeded, failed,
    or been cancelled). The decision (True/False) is therefore made AFTER
    the send's fate is known — guaranteeing that a late WS frame and native
    fallback never coexist:

      - If coroutine returns True within timeout → WS won → return True
      - If coroutine returns False within timeout → WS failed → return False
      - If timeout fires:
          1. Clear permit (prevents send from starting if coroutine hasn't reached it)
          2. Cancel future (injects CancelledError if coroutine is awaiting)
          3. WAIT for future to settle (bounded by drain_timeout)
          4. If future settled with True → the send completed before cancellation
             took effect → WS won → return True (no native fallback!)
          5. If future settled with False/exception/cancelled → WS lost → return False

    This ensures: when this function returns False, no frame was emitted.
    When it returns True, a frame was emitted and native fallback is suppressed.
    """
    if not is_ws_monitor_enabled():
        return False
    if not is_armed(terminal_id):
        return False

    # F158-R3: Detect same-loop caller.
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
        return False

    loop = target_loop if target_loop is not None else running_loop
    if loop is None or loop.is_closed():
        return False

    # Create a send permit — the coroutine checks this before send_text
    permit = threading.Event()
    permit.set()  # initially allowed

    # F158-R5: We need to cancel the asyncio.Task (not just the concurrent Future)
    # to inject CancelledError into a running coroutine. Store the task reference.
    _task_holder: list = []

    async def _run_and_track():
        """Wrapper that stores the asyncio.Task reference for cancellation."""
        return await _guarded_push_doorbell_frame(
            terminal_id, message_id, sender_short, preview, permit
        )

    async def _create_tracked_task():
        """Create and store the task, then await it."""
        task = asyncio.current_task()
        _task_holder.append(task)
        return await _guarded_push_doorbell_frame(
            terminal_id, message_id, sender_short, preview, permit
        )

    key = (terminal_id, message_id)
    with _send_permits_lock:
        _send_permits[key] = permit

    future = asyncio.run_coroutine_threadsafe(
        _create_tracked_task(),
        loop,
    )
    try:
        result = future.result(timeout=timeout)
        return result
    except Exception:
        # Timeout or other exception — the coroutine hasn't returned yet.
        # Step 1: Clear permit (prevent send from starting if not yet reached)
        permit.clear()

        # Step 2: Cancel the asyncio Task via the event loop (not the concurrent Future).
        # This injects CancelledError into the running coroutine.
        def _cancel_task():
            if _task_holder:
                _task_holder[0].cancel()

        loop.call_soon_threadsafe(_cancel_task)
        # Step 3: WAIT for the future to settle after cancellation.
        _DRAIN_TIMEOUT = 0.2
        try:
            settled_result = future.result(timeout=_DRAIN_TIMEOUT)
            if settled_result is True:
                # Send completed (resistant send emitted frame) → WS wins
                return True
            return False
        except Exception:
            # CancelledError propagated or timeout → WS lost
            _invalidate_ws_send(terminal_id, message_id)
            return False
    finally:
        with _send_permits_lock:
            _send_permits.pop(key, None)


# ---------------------------------------------------------------------------
# F158-R4: WS-delivered dedup state + invalidation set
# ---------------------------------------------------------------------------

_ws_delivered_lock = threading.Lock()
_ws_delivered: dict[tuple[str, int], float] = {}  # key → monotonic timestamp

_ws_invalidated_lock = threading.Lock()
_ws_invalidated: dict[tuple[str, int], float] = {}  # key → monotonic timestamp

# Limit max entries; use targeted eviction based on age
_WS_DELIVERED_MAX = 2048
_WS_INVALIDATED_MAX = 2048
_WS_ENTRY_TTL = 30.0  # entries older than 30s are stale and evictable


def _evict_stale(store: dict[tuple[str, int], float], max_size: int) -> None:
    """Remove entries older than TTL; if still over max, remove oldest half."""
    now = time.monotonic()
    expired = [k for k, ts in store.items() if now - ts > _WS_ENTRY_TTL]
    for k in expired:
        del store[k]
    if len(store) >= max_size:
        sorted_keys = sorted(store, key=store.get)  # type: ignore[arg-type]
        for k in sorted_keys[: len(sorted_keys) // 2]:
            del store[k]


def _is_expired(ts: float) -> bool:
    """Check if a timestamp is beyond the TTL."""
    return (time.monotonic() - ts) > _WS_ENTRY_TTL


def _invalidate_ws_send(terminal_id: str, row_id: int) -> None:
    """Record that this (terminal_id, row_id) timed out and native fallback was chosen."""
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
    native fallback chosen), the mark is NOT set.
    """
    key = (terminal_id, row_id)
    if _is_invalidated(terminal_id, row_id):
        _consume_invalidation(terminal_id, row_id)
        return
    with _ws_delivered_lock:
        if len(_ws_delivered) >= _WS_DELIVERED_MAX:
            _evict_stale(_ws_delivered, _WS_DELIVERED_MAX)
        _ws_delivered[key] = time.monotonic()


def consume_ws_delivered(terminal_id: str, row_id: int) -> bool:
    """Check and consume a WS-delivered mark.

    Returns True if the mark existed AND is not expired (WS already woke the
    supervisor for this row) — the caller should skip the native ring.

    F158-R4/S1: Expired entries are treated as absent and evicted on access.
    Also consumes any marks for this terminal with row_id <= the given row_id.
    """
    key = (terminal_id, row_id)
    found = False
    with _ws_delivered_lock:
        ts = _ws_delivered.get(key)
        if ts is not None:
            if _is_expired(ts):
                # Expired — treat as absent, evict
                del _ws_delivered[key]
            else:
                del _ws_delivered[key]
                found = True
        # Batch cleanup: remove expired or earlier marks for this terminal
        stale = [
            k
            for k in _ws_delivered
            if k[0] == terminal_id and (k[1] <= row_id or _is_expired(_ws_delivered[k]))
        ]
        for k in stale:
            del _ws_delivered[k]
    return found


def abandon_ws_delivered(terminal_id: str) -> None:
    """Remove all delivered marks for a terminal (e.g. on disconnect/abandon).

    F158-R3/S1: Prevents leaked marks when a terminal disconnects.
    """
    with _ws_delivered_lock:
        to_remove = [k for k in _ws_delivered if k[0] == terminal_id]
        for k in to_remove:
            del _ws_delivered[k]
    with _ws_invalidated_lock:
        to_remove = [k for k in _ws_invalidated if k[0] == terminal_id]
        for k in to_remove:
            del _ws_invalidated[k]
