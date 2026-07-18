"""Caller-only watchdog for silent assigned workers."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from cli_agent_orchestrator.clients.database import (
    cancel_pending_watchdog_message,
    create_inbox_message,
    get_callback_status_since,
    get_terminal_metadata,
    insert_barrier_escalation_message,
    insert_watchdog_auto_resume_message,
    list_pending_receiver_ids,
    list_ready_backlog_observations,
    terminal_exists,
)
from cli_agent_orchestrator.constants import (
    CAO_WAITING_INBOX_GRACE_SECONDS,
    STALLED_CALLBACK_GRACE_SECONDS,
    WAITING_INBOX_PUSH_FLOOR_S,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)
WATCHDOG_SCREEN_TAIL_LINES = 45
AUTO_RESUME_PROVIDERS = frozenset({"codex"})
AUTO_RESUME_BODY = (
    "[watchdog auto-resume] your previous turn ended on a transient API error. "
    "Continue your assigned task from where you left off; do not redo work that already "
    "completed; the original callback contract stands."
)


def _filtered_liveness_tail(tail: str, patterns: list[str]) -> str:
    if not patterns:
        return tail
    compiled = [re.compile(pattern) for pattern in patterns]
    return "\n".join(
        line for line in tail.splitlines() if not any(pattern.search(line) for pattern in compiled)
    )


@dataclass
class _Episode:
    caller_id: str
    profile: str
    inbound_at: float
    episode_started_wall_at: datetime
    last_join_wall_at: datetime | None = None
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
    generation: int = 1
    revision: int = 0
    auto_resumed: bool = False
    resume_reserved_at: float | None = None
    auto_resume_attempted_at: str | None = None


@dataclass(frozen=True)
class PreflightCandidate:
    terminal_id: str
    caller_id: str
    episode: _Episode
    generation: int
    revision: int
    episode_started_wall_at: datetime
    callback_fence_at_snapshot: int
    idle_seconds: int
    idle_since: float
    last_screen_fp: str


@dataclass(frozen=True)
class AutoResumeAction:
    terminal_id: str
    caller_id: str
    episode: _Episode
    generation: int
    revision: int
    episode_started_wall_at: datetime
    callback_fence_at_snapshot: int
    idle_since: float
    last_screen_fp: str
    body: str


@dataclass(frozen=True)
class WatchdogNotice:
    terminal_id: str
    caller_id: str
    message: str
    idle_reason: str | None


@dataclass
class WaitingInboxEpisode:
    waiting_since: float
    fired: bool = False


@dataclass
class ReadyBacklogEpisode:
    started_at: float
    fingerprint: tuple[object, ...]
    fired: bool = False


class StalledCallbackWatchdog:
    def __init__(self, grace_seconds: int = STALLED_CALLBACK_GRACE_SECONDS) -> None:
        self.grace_seconds = grace_seconds
        self._lock = threading.RLock()
        self._episodes: dict[str, _Episode] = {}
        self._waiting_inbox_episodes: dict[str, WaitingInboxEpisode] = {}
        self._waiting_inbox_last_push: dict[str, float] = {}
        self._ready_backlog_episodes: dict[str, ReadyBacklogEpisode] = {}
        self._paused: set[str] = set()
        self._generation_by_terminal: dict[str, int] = {}
        self._callback_fences: dict[str, int] = {}

    @contextmanager
    def callback_insert_guard(self, sender_id: str):
        """Fence a worker-sent durable insert from before write authority through commit."""
        if sender_id.startswith("watchdog:") or not terminal_exists(sender_id):
            yield
            return
        self._lock.acquire()
        try:
            self._callback_fences[sender_id] = self._callback_fences.get(sender_id, 0) + 1
            yield
        finally:
            self._lock.release()

    @contextmanager
    def confirmed_settlement_guard(self):
        """Pre-acquire the watchdog RLock before a confirmed settlement transaction."""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    def pause_terminal(self, terminal_id: str):
        with self._lock:
            self._paused.add(terminal_id)
            return copy.deepcopy(self._episodes.get(terminal_id)), time.monotonic()

    def resume_terminal(self, terminal_id: str, snapshot) -> None:
        episode, started = snapshot
        elapsed = time.monotonic() - started
        with self._lock:
            if episode is not None and episode.idle_since is not None:
                episode.idle_since += elapsed
            if episode is not None:
                self._episodes[terminal_id] = episode
            self._paused.discard(terminal_id)

    def repair_terminal_after_resume_failure(self, terminal_id: str, snapshot) -> None:
        """Best-effort, non-raising P14 repair used before releasing quarantine locks."""
        try:
            episode, started = snapshot
            elapsed = time.monotonic() - started
        except Exception:
            episode, elapsed = None, 0.0
        with self._lock:
            if episode is not None and episode.idle_since is not None:
                episode.idle_since += elapsed
            if episode is not None:
                self._episodes[terminal_id] = episode
            self._paused.discard(terminal_id)

    def record_inbound_task(self, terminal_id: str, caller_id: str, profile: str) -> None:
        if caller_id.startswith("watchdog:"):
            return
        now = time.monotonic()
        wall_now = datetime.now()
        with self._lock:
            if terminal_id in self._paused:
                return
            episode = self._episodes.get(terminal_id)
            if (
                episode is not None
                and not episode.callback_seen
                and not episode.fired
                and episode.resume_reserved_at is None
                and not episode.auto_resumed
            ):
                episode.last_join_wall_at = wall_now
                episode.revision += 1
                return
            generation = self._generation_by_terminal.get(terminal_id, 0) + 1
            self._generation_by_terminal[terminal_id] = generation
            self._episodes[terminal_id] = _Episode(
                caller_id=caller_id,
                profile=profile,
                inbound_at=now,
                episode_started_wall_at=wall_now,
                generation=generation,
            )

    def has_episode(self, terminal_id: str) -> bool:
        with self._lock:
            return terminal_id in self._episodes

    def clear_terminal(self, terminal_id: str) -> None:
        with self._lock:
            self._episodes.pop(terminal_id, None)
            self._waiting_inbox_episodes.pop(terminal_id, None)
            self._waiting_inbox_last_push.pop(terminal_id, None)
            self._ready_backlog_episodes.pop(terminal_id, None)
            self._generation_by_terminal.pop(terminal_id, None)
            self._callback_fences.pop(terminal_id, None)

    def record_callback_if_to_caller(self, sender_id: str, receiver_id: str) -> None:
        meta = get_terminal_metadata(sender_id)
        if not meta or receiver_id not in {
            meta.get("caller_id"),
            meta.get("caller_mailbox_id"),
        }:
            return
        with self._lock:
            if sender_id in self._paused:
                return
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
            if terminal_id in self._paused:
                return
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
                if terminal_id not in self._paused
                and not episode.callback_seen
                and not episode.fired
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
                if terminal_id not in self._paused
                and not episode.callback_seen
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

    def _fresh_frame_decides_running(self, terminal_id: str) -> tuple[bool, str | None]:
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.providers.manager import provider_manager

        try:
            metadata = get_terminal_metadata(terminal_id)
            provider = provider_manager.get_provider(terminal_id)
            if metadata is None or provider is None:
                return False, None
            frame = get_backend().capture_viewport(
                metadata["tmux_session"], metadata["tmux_window"]
            )
            rows = frame.splitlines()
            classification = provider.classify_screen(rows)
            idle_reason = provider.classify_idle_reason(rows, classification)
            return (
                classification.status == TerminalStatus.PROCESSING
                and classification.provider_signal == "RUNNING_PATTERN",
                idle_reason if isinstance(idle_reason, str) else None,
            )
        except Exception:
            return False, None

    def collect_due_notifications(self, now: float | None = None) -> list[WatchdogNotice]:
        now = time.monotonic() if now is None else now
        candidates: list[PreflightCandidate] = []
        with self._lock:
            for terminal_id, episode in self._episodes.items():
                if terminal_id in self._paused:
                    continue
                if (
                    episode.callback_seen
                    or episode.fired
                    or episode.idle_since is None
                    or episode.last_screen_fp is None
                    or episode.resume_reserved_at is not None
                ):
                    continue
                idle_seconds = int(now - episode.idle_since)
                if idle_seconds < self.grace_seconds:
                    continue
                candidates.append(
                    PreflightCandidate(
                        terminal_id=terminal_id,
                        caller_id=episode.caller_id,
                        episode=episode,
                        generation=episode.generation,
                        revision=episode.revision,
                        episode_started_wall_at=episode.episode_started_wall_at,
                        callback_fence_at_snapshot=self._callback_fences.get(terminal_id, 0),
                        idle_seconds=idle_seconds,
                        idle_since=episode.idle_since,
                        last_screen_fp=episode.last_screen_fp,
                    )
                )

        due: list[WatchdogNotice] = []
        for candidate in candidates:
            metadata = get_terminal_metadata(candidate.terminal_id)
            callback_status = get_callback_status_since(
                candidate.terminal_id,
                candidate.caller_id,
                candidate.episode_started_wall_at,
            )
            provider = metadata.get("provider") if metadata is not None else None
            auto_resume_applicable = (
                self._auto_resume_enabled() and provider in AUTO_RESUME_PROVIDERS
            )
            suppress = False
            fallback_idle_reason = None
            second_callback_status = None
            if (
                metadata is not None
                and callback_status is None
                and (not auto_resume_applicable or candidate.episode.auto_resumed)
            ):
                frame_decides_running, fallback_idle_reason = self._fresh_frame_decides_running(
                    candidate.terminal_id
                )
                suppress = frame_decides_running and not auto_resume_applicable
                second_callback_status = get_callback_status_since(
                    candidate.terminal_id,
                    candidate.caller_id,
                    candidate.episode_started_wall_at,
                )
            action: AutoResumeAction | None = None
            with self._lock:
                current_episode = self._episodes.get(candidate.terminal_id)
                if metadata is None:
                    self._episodes.pop(candidate.terminal_id, None)
                    continue
                if not self._candidate_valid(candidate, current_episode):
                    continue
                assert current_episode is not None
                assert current_episode.idle_since is not None
                if int(now - current_episode.idle_since) < self.grace_seconds:
                    continue
                if callback_status in {
                    MessageStatus.PENDING,
                    MessageStatus.HELD,
                    MessageStatus.DELIVERING,
                }:
                    continue
                if callback_status == MessageStatus.DELIVERED:
                    current_episode.callback_seen = True
                    continue
                if second_callback_status in {
                    MessageStatus.PENDING,
                    MessageStatus.HELD,
                    MessageStatus.DELIVERING,
                }:
                    continue
                if second_callback_status == MessageStatus.DELIVERED:
                    current_episode.callback_seen = True
                    continue
                if suppress:
                    current_episode.idle_since = now
                    continue
                if current_episode.auto_resumed:
                    current_episode.fired = True
                    suffix = (
                        f" (auto-resume attempted at {current_episode.auto_resume_attempted_at})"
                    )
                    due.append(
                        self._push_notice(
                            candidate,
                            current_episode,
                            suffix,
                            idle_reason=fallback_idle_reason,
                        )
                    )
                    continue
                if not auto_resume_applicable:
                    current_episode.fired = True
                    due.append(
                        self._push_notice(
                            candidate,
                            current_episode,
                            idle_reason=fallback_idle_reason,
                        )
                    )
                    continue
                current_episode.resume_reserved_at = now
                action = AutoResumeAction(
                    terminal_id=candidate.terminal_id,
                    caller_id=candidate.caller_id,
                    episode=candidate.episode,
                    generation=candidate.generation,
                    revision=candidate.revision,
                    episode_started_wall_at=candidate.episode_started_wall_at,
                    callback_fence_at_snapshot=candidate.callback_fence_at_snapshot,
                    idle_since=candidate.idle_since,
                    last_screen_fp=candidate.last_screen_fp,
                    body=AUTO_RESUME_BODY,
                )
            if action is not None:
                push = self._execute_auto_resume(action, now)
                if push is not None:
                    due.append(push)
        return due

    @staticmethod
    def _auto_resume_enabled() -> bool:
        return os.environ.get("CAO_WATCHDOG_AUTO_RESUME", "").strip().casefold() not in {
            "0",
            "false",
        }

    def _candidate_valid(
        self, candidate: PreflightCandidate | AutoResumeAction, episode: _Episode | None
    ) -> bool:
        return (
            episode is not None
            and episode is candidate.episode
            and episode.generation == candidate.generation
            and episode.revision == candidate.revision
            and not episode.callback_seen
            and not episode.fired
            and episode.idle_since is not None
            and episode.idle_since == candidate.idle_since
            and episode.last_screen_fp == candidate.last_screen_fp
            and self._callback_fences.get(candidate.terminal_id, 0)
            == candidate.callback_fence_at_snapshot
        )

    @staticmethod
    def _push_notice(
        candidate: PreflightCandidate | AutoResumeAction,
        episode: _Episode,
        suffix: str = "",
        idle_seconds: int | None = None,
        idle_reason: str | None = None,
    ) -> WatchdogNotice:
        if idle_seconds is None:
            idle_seconds = (
                candidate.idle_seconds
                if isinstance(candidate, PreflightCandidate)
                else int(time.monotonic() - (episode.idle_since or time.monotonic()))
            )
        return WatchdogNotice(
            terminal_id=candidate.terminal_id,
            caller_id=candidate.caller_id,
            message=f"[watchdog] worker {candidate.terminal_id} ({episode.profile}) "
            f"idle {idle_seconds}s without callback"
            f"{f' [reason: {idle_reason}]' if idle_reason is not None else ''}{suffix}",
            idle_reason=idle_reason,
        )

    def _execute_auto_resume(
        self, action: AutoResumeAction, enqueue_monotonic: float
    ) -> WatchdogNotice | None:
        from cli_agent_orchestrator.services.auto_responder import auto_responder
        from cli_agent_orchestrator.services.inbox_service import get_delivery_lock, inbox_service
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        idle_reason = None
        try:
            probe_status, probe_meta = status_monitor.probe_screen_status(action.terminal_id)
            if isinstance(probe_meta, dict) and isinstance(probe_meta.get("idle_reason"), str):
                idle_reason = probe_meta["idle_reason"]
            applicable = (
                isinstance(probe_meta, dict)
                and probe_meta.get("transient_api_error") is True
                and probe_status == TerminalStatus.IDLE
                and auto_responder.waiting_gate(action.terminal_id) is None
            )
        except Exception:
            logger.exception("Failed auto-resume preflight for %s", action.terminal_id)
            applicable = False

        if not applicable:
            with self._lock:
                episode = self._episodes.get(action.terminal_id)
                if not self._candidate_valid(action, episode):
                    if episode is not None and episode.generation == action.generation:
                        episode.resume_reserved_at = None
                    return None
                assert episode is not None
                episode.resume_reserved_at = None
                episode.fired = True
                return self._push_notice(
                    action,
                    episode,
                    idle_seconds=int(enqueue_monotonic - (episode.idle_since or enqueue_monotonic)),
                    idle_reason=idle_reason,
                )

        delivery_lock = get_delivery_lock(action.terminal_id)
        if not delivery_lock.acquire(blocking=False):
            with self._lock:
                episode = self._episodes.get(action.terminal_id)
                if self._candidate_valid(action, episode):
                    assert episode is not None
                    episode.resume_reserved_at = None
            return None

        inserted = None
        second_status = None
        should_deliver = False
        try:
            inserted = insert_watchdog_auto_resume_message(action.terminal_id, action.body)
            second_status = get_callback_status_since(
                action.terminal_id,
                action.caller_id,
                action.episode_started_wall_at,
            )
            with self._lock:
                episode = self._episodes.get(action.terminal_id)
                valid = self._candidate_valid(action, episode) and second_status is None
                if second_status == MessageStatus.DELIVERED and episode is not None:
                    episode.callback_seen = True
                    valid = False
                if valid:
                    assert episode is not None
                    episode.resume_reserved_at = None
                    if inserted.kind in {"inserted", "uncertain"}:
                        attempted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                        episode.auto_resumed = True
                        episode.auto_resume_attempted_at = attempted_at
                        episode.idle_since = enqueue_monotonic
                        should_deliver = True
                    else:
                        episode.fired = True
                        return self._push_notice(
                            action,
                            episode,
                            idle_seconds=int(
                                enqueue_monotonic - (episode.idle_since or enqueue_monotonic)
                            ),
                            idle_reason=idle_reason,
                        )
                else:
                    if episode is not None and episode.generation == action.generation:
                        episode.resume_reserved_at = None
                    if inserted.kind == "inserted" and inserted.message_id is not None:
                        if not cancel_pending_watchdog_message(
                            inserted.message_id, action.terminal_id
                        ):
                            logger.info(
                                "auto-resume cancellation lost terminal=%s message=%s",
                                action.terminal_id,
                                inserted.message_id,
                            )
        finally:
            delivery_lock.release()
        if should_deliver:
            try:
                inbox_service.deliver_pending(action.terminal_id)
            except Exception:
                logger.exception("Failed to deliver auto-resume for %s", action.terminal_id)
        return None

    def notify_due(self, registry: PluginRegistry | None = None) -> None:
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        for notice in self.collect_due_notifications():
            try:
                handled = insert_barrier_escalation_message(
                    notice.terminal_id,
                    notice.caller_id,
                    notice.message,
                    notice.idle_reason,
                )
                if handled is None:
                    create_inbox_message(
                        f"watchdog:{notice.terminal_id}",
                        notice.caller_id,
                        notice.message,
                    )
                inbox_service.deliver_pending(notice.caller_id, registry=registry)
            except Exception:
                logger.exception("Failed to push stalled-callback watchdog notification")

    def tick_waiting_inbox(
        self,
        registry: PluginRegistry | None = None,
        now: float | None = None,
    ) -> None:
        from cli_agent_orchestrator.services.auto_responder import auto_responder
        from cli_agent_orchestrator.services.inbox_service import inbox_service
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        now = time.monotonic() if now is None else now
        pending_ids = set(list_pending_receiver_ids())
        with self._lock:
            for terminal_id in set(self._waiting_inbox_episodes) - pending_ids:
                self._waiting_inbox_episodes.pop(terminal_id, None)

        for terminal_id in pending_ids:
            metadata = get_terminal_metadata(terminal_id)
            if metadata is None:
                with self._lock:
                    self._waiting_inbox_episodes.pop(terminal_id, None)
                continue

            status = status_monitor.get_status(terminal_id)
            if status != TerminalStatus.WAITING_USER_ANSWER:
                with self._lock:
                    self._waiting_inbox_episodes.pop(terminal_id, None)
                continue

            with self._lock:
                episode = self._waiting_inbox_episodes.get(terminal_id)
                if episode is None:
                    self._waiting_inbox_episodes[terminal_id] = WaitingInboxEpisode(
                        waiting_since=now
                    )
                    continue
                if episode.fired:
                    continue
                if now - episode.waiting_since < CAO_WAITING_INBOX_GRACE_SECONDS:
                    continue

            if auto_responder.waiting_gate(terminal_id) is not None:
                continue

            caller_id = metadata.get("caller_id")
            if not caller_id or caller_id == terminal_id:
                logger.warning(
                    "waiting-inbox watchdog: refusing invalid caller for terminal %s",
                    terminal_id,
                )
                with self._lock:
                    current = self._waiting_inbox_episodes.get(terminal_id)
                    if current is episode:
                        current.fired = True
                continue

            with self._lock:
                if (
                    now - self._waiting_inbox_last_push.get(terminal_id, float("-inf"))
                    < WAITING_INBOX_PUSH_FLOOR_S
                ):
                    continue
                current = self._waiting_inbox_episodes.get(terminal_id)
                if current is not episode or current.fired:
                    continue
                current.fired = True
                self._waiting_inbox_last_push[terminal_id] = now

            age = int(now - episode.waiting_since)
            name = metadata.get("agent_profile") or "unknown"
            message = (
                f"[waiting-inbox watchdog] terminal {terminal_id} ({name}) has had pending "
                f"inbox messages while status=waiting_user_answer for {age}s with no "
                "auto-responder episode open — it may be stuck on an unrecognized dialog "
                "or a false-WAITING parse. Peek it (peek_terminal / tmux attach) and nudge "
                "or answer manually. This alert fires at most once per stuck episode "
                "(floor 300s)."
            )
            try:
                create_inbox_message(f"watchdog:{terminal_id}", caller_id, message)
                inbox_service.deliver_pending(caller_id, registry=registry)
            except Exception:
                logger.warning(
                    "Failed to push waiting-inbox watchdog notification for %s",
                    terminal_id,
                    exc_info=True,
                )

    def tick_ready_backlog(
        self,
        registry: PluginRegistry | None = None,
        now: float | None = None,
    ) -> None:
        """Alert on an idle, aged pending backlog whose attempts make no progress."""
        from cli_agent_orchestrator.services.inbox_service import inbox_service
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        now = time.monotonic() if now is None else now
        observations = {item.receiver_id: item for item in list_ready_backlog_observations()}
        with self._lock:
            for terminal_id in set(self._ready_backlog_episodes) - set(observations):
                self._ready_backlog_episodes.pop(terminal_id, None)

        for terminal_id, observation in observations.items():
            metadata = get_terminal_metadata(terminal_id)
            if metadata is None:
                with self._lock:
                    self._ready_backlog_episodes.pop(terminal_id, None)
                continue
            status = status_monitor.get_status(terminal_id)
            if (
                status not in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
                or observation.oldest_pending_age_seconds <= CAO_WAITING_INBOX_GRACE_SECONDS
                or observation.has_open_delivering_attempt
            ):
                with self._lock:
                    self._ready_backlog_episodes.pop(terminal_id, None)
                continue

            fingerprint = tuple(observation.attempt_fingerprint)
            with self._lock:
                episode = self._ready_backlog_episodes.get(terminal_id)
                if episode is None or episode.fingerprint != fingerprint:
                    self._ready_backlog_episodes[terminal_id] = ReadyBacklogEpisode(
                        started_at=now,
                        fingerprint=fingerprint,
                    )
                    continue
                if episode.fired or now - episode.started_at < CAO_WAITING_INBOX_GRACE_SECONDS:
                    continue

                caller_id = metadata.get("caller_id")
                if not caller_id or caller_id == terminal_id:
                    logger.warning(
                        "ready-backlog watchdog: refusing invalid caller for terminal %s",
                        terminal_id,
                    )
                    episode.fired = True
                    continue
                episode.fired = True

            age = int(observation.oldest_pending_age_seconds)
            message_id = observation.oldest_message_id
            message = (
                f"[ready-backlog watchdog] terminal {terminal_id} has pending message "
                f"{message_id} aged {age}s while status={status.value} with no open "
                "delivery attempt or attempt progress; inspect "
                f"`cao messages trace {message_id}`. Reconciliation remains the retry owner."
            )
            try:
                create_inbox_message(f"watchdog:{terminal_id}", caller_id, message)
                inbox_service.deliver_pending(caller_id, registry=registry)
            except Exception:
                logger.warning(
                    "Failed to push ready-backlog watchdog notification for %s",
                    terminal_id,
                    exc_info=True,
                )

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
                await asyncio.to_thread(self.tick_waiting_inbox, registry)
                await asyncio.to_thread(self.tick_ready_backlog, registry)
            except Exception:
                logger.exception("StalledCallbackWatchdog error")


stalled_callback_watchdog = StalledCallbackWatchdog()
