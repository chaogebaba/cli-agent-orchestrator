"""F461 — Doorbell coalesce service.

Coalesces near-simultaneous worker callbacks into one wake/one bridge message.
When N workers finish within the coalesce window (supervisor.wake.coalesce_s,
default 5s), one combined doorbell ring is fired carrying all N summaries.

Design invariants:
- D1: Per-terminal buffer of pending doorbell intents.
- D2: Timer fires after coalesce_s; the first intent starts the timer.
- D3: When the timer fires, the buffer is drained and ONE ring is issued.
- D4: from-name = individual worker when N=1, 'cao-fleet' when N>1.
- D5: Ordering preserved — rows delivered oldest-first in combined digest.
- D6: The durable inbox and exactly-once ack are untouched (this is transport).
- D7: Config supervisor.wake.coalesce_s = 0 disables coalescing (immediate fire).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)


@dataclass
class _DoorbellIntent:
    """One pending doorbell ring intent."""

    terminal_id: str
    max_written_row_id: int
    written_count: int
    message_body: str | None
    sender_display_name: str | None
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class _TerminalBuffer:
    """Per-terminal coalesce buffer."""

    intents: list[_DoorbellIntent] = field(default_factory=list)
    timer_handle: Any = None  # asyncio.TimerHandle | None
    armed_at: float = 0.0


class DoorbellCoalesceService:
    """F461: Accumulates doorbell intents and fires one coalesced ring per window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buffers: dict[str, _TerminalBuffer] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fire_fn: Callable[..., str] | None = None

    def bind(
        self,
        loop: asyncio.AbstractEventLoop,
        fire_fn: Callable[..., str],
    ) -> None:
        """Bind the event loop and the actual ring function.

        fire_fn signature matches ring_supervisor_doorbell:
            (terminal_id, max_written_row_id, *, written_count, message_body, sender_display_name) -> str
        """
        self._loop = loop
        self._fire_fn = fire_fn

    @property
    def _coalesce_s(self) -> float:
        """Get the configured coalesce window in seconds."""
        return float(ConfigService.get("supervisor.wake.coalesce_s", default=5.0))

    def submit(
        self,
        terminal_id: str,
        max_written_row_id: int,
        *,
        written_count: int = 0,
        message_body: str | None = None,
        sender_display_name: str | None = None,
    ) -> None:
        """Submit a doorbell intent to the coalesce buffer.

        If coalesce_s == 0, fires immediately (no buffering).
        Otherwise, buffers the intent and arms/extends a timer.
        """
        intent = _DoorbellIntent(
            terminal_id=terminal_id,
            max_written_row_id=max_written_row_id,
            written_count=written_count,
            message_body=message_body,
            sender_display_name=sender_display_name,
        )

        window = self._coalesce_s
        if window <= 0:
            # Coalescing disabled — fire immediately
            self._fire_single(intent)
            return

        needs_arm = False
        with self._lock:
            buf = self._buffers.get(terminal_id)
            if buf is None:
                buf = _TerminalBuffer()
                self._buffers[terminal_id] = buf

            buf.intents.append(intent)

            # If no timer armed yet, mark for arming (outside lock)
            if buf.timer_handle is None:
                buf.armed_at = time.monotonic()
                needs_arm = True

        # Arm timer outside the lock to avoid deadlock in degraded (no-loop) path
        if needs_arm:
            self._arm_timer(terminal_id, window)

    def _arm_timer(self, terminal_id: str, delay: float) -> None:
        """Arm the coalesce timer on the event loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            # No loop — fire immediately (degraded mode)
            self._flush_buffer(terminal_id)
            return

        def _schedule() -> None:
            buf = self._buffers.get(terminal_id)
            if buf is not None:
                buf.timer_handle = loop.call_later(
                    delay, self._on_timer_fire, terminal_id
                )

        try:
            loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            # Loop closed — fire immediately
            self._flush_buffer(terminal_id)

    def _on_timer_fire(self, terminal_id: str) -> None:
        """Timer callback — drain the buffer and fire one coalesced ring."""
        self._flush_buffer(terminal_id)

    def _flush_buffer(self, terminal_id: str) -> None:
        """Drain the terminal's buffer and fire one coalesced doorbell ring."""
        with self._lock:
            buf = self._buffers.pop(terminal_id, None)
            if buf is None:
                return
            intents = buf.intents
            buf.timer_handle = None

        if not intents:
            return

        # D5: sort by row id (oldest first — preserves ordering)
        intents.sort(key=lambda i: i.max_written_row_id)

        if len(intents) == 1:
            # N=1: fire as individual worker
            self._fire_single(intents[0])
        else:
            # N>1: fire coalesced ring
            self._fire_coalesced(terminal_id, intents)

    def _fire_single(self, intent: _DoorbellIntent) -> None:
        """Fire a single doorbell ring (no coalescing)."""
        if self._fire_fn is None:
            return
        try:
            # F461: coalesce timer fires asynchronously — no delivery lock is held
            self._fire_fn(
                intent.terminal_id,
                intent.max_written_row_id,
                written_count=intent.written_count,
                caller_holds_no_delivery_lock=True,
                message_body=intent.message_body,
                sender_display_name=intent.sender_display_name,
            )
        except Exception as exc:
            logger.debug(
                "f461_coalesce_fire_single_error terminal=%s: %s",
                intent.terminal_id,
                exc,
            )

    def _fire_coalesced(self, terminal_id: str, intents: list[_DoorbellIntent]) -> None:
        """Fire one coalesced doorbell ring combining N intents.

        D4: from-name = 'cao-fleet', body = combined digest of all N callbacks.
        """
        if self._fire_fn is None:
            return

        # Build combined body: one summary line per callback, oldest first
        total_written = sum(i.written_count for i in intents)
        max_row_id = max(i.max_written_row_id for i in intents)

        # Build summary lines
        summary_lines: list[str] = []
        for intent in intents:
            sender = intent.sender_display_name or "worker"
            # First line of body as preview
            if intent.message_body:
                preview = intent.message_body.split("\n", 1)[0][:100]
            else:
                preview = f"(row {intent.max_written_row_id})"
            summary_lines.append(f"- [{sender}] {preview}")

        combined_body = (
            f"[cao-fleet] {len(intents)} callbacks coalesced:\n"
            + "\n".join(summary_lines)
        )

        # Append full bodies (truncated) for each
        for intent in intents:
            if intent.message_body:
                sender = intent.sender_display_name or "worker"
                combined_body += f"\n\n--- from {sender} (row {intent.max_written_row_id}) ---\n"
                combined_body += intent.message_body

        try:
            # F461: coalesce timer fires asynchronously — no delivery lock is held
            self._fire_fn(
                terminal_id,
                max_row_id,
                written_count=total_written,
                caller_holds_no_delivery_lock=True,
                message_body=combined_body,
                sender_display_name="cao-fleet",
            )
            logger.info(
                "f461_coalesced_ring terminal=%s count=%d max_row=%d",
                terminal_id,
                len(intents),
                max_row_id,
            )
        except Exception as exc:
            logger.debug(
                "f461_coalesce_fire_coalesced_error terminal=%s: %s",
                terminal_id,
                exc,
            )

    def pending_count(self, terminal_id: str) -> int:
        """Return the number of pending intents for a terminal (test helper)."""
        with self._lock:
            buf = self._buffers.get(terminal_id)
            return len(buf.intents) if buf else 0

    def flush_all(self) -> None:
        """Force-flush all buffers (for shutdown/testing)."""
        with self._lock:
            terminal_ids = list(self._buffers.keys())
        for tid in terminal_ids:
            self._flush_buffer(tid)


# Module-level singleton
doorbell_coalesce_service = DoorbellCoalesceService()
