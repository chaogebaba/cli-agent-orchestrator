"""Caller-only watchdog for silent assigned workers."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass

from cli_agent_orchestrator.clients.database import create_inbox_message, get_terminal_metadata
from cli_agent_orchestrator.constants import STALLED_CALLBACK_GRACE_SECONDS
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)
WATCHDOG_SCREEN_TAIL_LINES = 45


def _filtered_liveness_tail(tail: str, patterns: list[str]) -> str:
    if not patterns:
        return tail
    compiled = [re.compile(pattern) for pattern in patterns]
    return "\n".join(
        line
        for line in tail.splitlines()
        if not any(pattern.search(line) for pattern in compiled)
    )


@dataclass
class _Episode:
    caller_id: str
    profile: str
    inbound_at: float
    callback_seen: bool = False
    fired: bool = False
    idle_since: float | None = None
    # Fingerprint of the pane's rendered tail, used as a status-independent
    # liveness signal: a worker whose screen is still changing (spinner ticks,
    # streaming output) is NOT idle, whatever the status pipeline claims.
    # Guards against false fires when status detection latches a stale ready
    # state (observed live: pyte screen divergence latched COMPLETED through
    # a whole busy codex turn).
    last_screen_fp: str | None = None


class StalledCallbackWatchdog:
    def __init__(self, grace_seconds: int = STALLED_CALLBACK_GRACE_SECONDS) -> None:
        self.grace_seconds = grace_seconds
        self._lock = threading.RLock()
        self._episodes: dict[str, _Episode] = {}

    def record_inbound_task(self, terminal_id: str, caller_id: str, profile: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._episodes[terminal_id] = _Episode(
                caller_id=caller_id,
                profile=profile,
                inbound_at=now,
            )

    def has_episode(self, terminal_id: str) -> bool:
        with self._lock:
            return terminal_id in self._episodes

    def clear_terminal(self, terminal_id: str) -> None:
        with self._lock:
            self._episodes.pop(terminal_id, None)

    def record_callback_if_to_caller(self, sender_id: str, receiver_id: str) -> None:
        meta = get_terminal_metadata(sender_id)
        if not meta or meta.get("caller_id") != receiver_id:
            return
        with self._lock:
            episode = self._episodes.get(sender_id)
            if episode and episode.caller_id == receiver_id:
                episode.callback_seen = True

    def record_status(
        self,
        terminal_id: str,
        status: TerminalStatus,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            episode = self._episodes.get(terminal_id)
            if episode is None:
                return
            if status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}:
                if episode.idle_since is None:
                    episode.idle_since = now
                    episode.last_screen_fp = None
            else:
                episode.idle_since = None
                episode.last_screen_fp = None

    def poll_unarmed_statuses(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            terminal_ids = [
                terminal_id
                for terminal_id, episode in self._episodes.items()
                if not episode.callback_seen and not episode.fired
            ]

        if not terminal_ids:
            return

        from cli_agent_orchestrator.services.status_monitor import status_monitor

        for terminal_id in terminal_ids:
            try:
                self.record_status(terminal_id, status_monitor.get_status(terminal_id), now=now)
            except Exception:
                logger.exception(
                    "Failed to poll status for stalled-callback watchdog: %s",
                    terminal_id,
                )

    def refresh_screen_fingerprints(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            terminal_ids = [
                terminal_id
                for terminal_id, episode in self._episodes.items()
                if not episode.callback_seen
                and not episode.fired
                and episode.idle_since is not None
            ]

        if not terminal_ids:
            return

        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.providers.manager import provider_manager

        backend = get_backend()
        for terminal_id in terminal_ids:
            metadata = get_terminal_metadata(terminal_id)
            if not metadata:
                continue
            try:
                tail = backend.get_history(
                    metadata["tmux_session"],
                    metadata["tmux_window"],
                    tail_lines=WATCHDOG_SCREEN_TAIL_LINES,
                    strip_escapes=True,
                )
                provider = provider_manager.get_provider(terminal_id)
                patterns = (
                    getattr(provider, "liveness_exclude_patterns", [])
                    if provider is not None
                    else []
                )
                tail = _filtered_liveness_tail(tail, list(patterns or []))
                fingerprint = hashlib.sha256(tail.encode("utf-8", "replace")).hexdigest()
            except Exception:
                logger.exception(
                    "Failed to fingerprint screen for stalled-callback watchdog: %s",
                    terminal_id,
                )
                continue

            with self._lock:
                episode = self._episodes.get(terminal_id)
                if (
                    episode is None
                    or episode.callback_seen
                    or episode.fired
                    or episode.idle_since is None
                ):
                    continue
                if episode.last_screen_fp is None:
                    episode.last_screen_fp = fingerprint
                elif episode.last_screen_fp != fingerprint:
                    episode.idle_since = now
                    episode.last_screen_fp = fingerprint

    def collect_due_notifications(self, now: float | None = None) -> list[tuple[str, str, str]]:
        now = time.monotonic() if now is None else now
        due: list[tuple[str, str, str]] = []
        with self._lock:
            for terminal_id, episode in list(self._episodes.items()):
                if get_terminal_metadata(terminal_id) is None:
                    self._episodes.pop(terminal_id, None)
                    continue
                if (
                    episode.callback_seen
                    or episode.fired
                    or episode.idle_since is None
                    or episode.last_screen_fp is None
                ):
                    continue
                idle_seconds = int(now - episode.idle_since)
                if idle_seconds < self.grace_seconds:
                    continue
                episode.fired = True
                due.append(
                    (
                        terminal_id,
                        episode.caller_id,
                        f"[watchdog] worker {terminal_id} ({episode.profile}) "
                        f"idle {idle_seconds}s without callback",
                    )
                )
        return due

    def notify_due(self, registry: PluginRegistry | None = None) -> None:
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        for terminal_id, caller_id, message in self.collect_due_notifications():
            try:
                create_inbox_message(f"watchdog:{terminal_id}", caller_id, message)
                inbox_service.deliver_pending(caller_id, registry=registry)
            except Exception:
                logger.exception("Failed to push stalled-callback watchdog notification")

    async def run(self, registry: PluginRegistry | None = None) -> None:
        queue = bus.subscribe("terminal.*.status")
        logger.info("StalledCallbackWatchdog started")
        interval = max(1.0, min(5.0, float(self.grace_seconds)))
        while True:
            try:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=interval)
                except asyncio.TimeoutError:
                    event = None
                if event is not None:
                    terminal_id = terminal_id_from_topic(event["topic"])
                    self.record_status(
                        terminal_id,
                        TerminalStatus(event["data"]["status"]),
                    )
                await asyncio.to_thread(self.poll_unarmed_statuses)
                await asyncio.to_thread(self.refresh_screen_fingerprints)
                await asyncio.to_thread(self.notify_due, registry)
            except Exception:
                logger.exception("StalledCallbackWatchdog error")


stalled_callback_watchdog = StalledCallbackWatchdog()
