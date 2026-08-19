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
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from cli_agent_orchestrator.clients.database import (
    _utcnow,
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
from cli_agent_orchestrator.services import receiver_state_view
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)
WATCHDOG_SCREEN_TAIL_LINES = 45
WATCHDOG_WAITING_ESCALATE_S = 2 * STALLED_CALLBACK_GRACE_SECONDS
WATCHDOG_WAITING_REPEAT_FLOOR_S = 600
AUTO_RESUME_PROVIDERS = frozenset({"codex"})
AUTO_RESUME_BODY = (
    "[watchdog auto-resume] your previous turn ended on a transient API error. "
    "Continue your assigned task from where you left off; do not redo work that already "
    "completed; the original callback contract stands."
)
# FX181 D2 row 1: the TERMINAL statuses for the aggregate quiescence predicate,
# mapped to their message labels. Membership here IS the classification — a status
# absent from this map is indeterminate and can never contribute to a ring.
_QUIESCENT_STATUS_LABELS = {
    TerminalStatus.IDLE: "idle",
    TerminalStatus.COMPLETED: "completed",
    TerminalStatus.ERROR: "error",
}


def _supervisor_mailbox_live_terminal(caller_id: str) -> str | None:
    """FX181 D9 second half: the live terminal a supervisor mailbox routes to.

    Returns the successor terminal id when a supervisor mailbox owned by
    ``caller_id`` (either the mailbox id itself, or a mailbox this terminal is an
    incarnation of) currently points at a terminal that still resolves. Returns
    ``None`` when there is no such mailbox or its current terminal is gone too —
    only then is the caller genuinely unreachable and its stores droppable.

    Non-raising by contract: any DB fault degrades to "no successor", which keeps
    the caller's stores only if its own metadata resolved.
    """
    try:
        from cli_agent_orchestrator.clients.database import (
            MailboxIncarnationModel,
            MailboxModel,
            SessionLocal,
        )

        with SessionLocal() as db:
            mailbox = (
                db.query(MailboxModel).filter_by(id=caller_id, role="supervisor").one_or_none()
            )
            if mailbox is None:
                incarnation = (
                    db.query(MailboxIncarnationModel).filter_by(terminal_id=caller_id).one_or_none()
                )
                if incarnation is not None:
                    mailbox = (
                        db.query(MailboxModel)
                        .filter_by(id=incarnation.mailbox_id, role="supervisor")
                        .one_or_none()
                    )
            if mailbox is None:
                return None
            successor = mailbox.current_terminal_id
    except Exception:
        logger.debug(
            "quiescence: supervisor mailbox lookup failed for %s", caller_id, exc_info=True
        )
        return None

    if not successor or successor == caller_id:
        return None
    return successor if get_terminal_metadata(successor) is not None else None


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
    # FX193 D2: Live terminal status for busy-gating nudge repeats.
    # Written by record_status(); read by delivery_service._check_safety_gates().
    status: TerminalStatus = TerminalStatus.UNKNOWN
    # Fingerprint of the pane's rendered tail, used as a status-independent
    # liveness signal: a worker whose screen is still changing (spinner ticks,
    # streaming output) is NOT idle, whatever the status pipeline claims.
    # Guards against false fires when status detection latches a stale ready
    # state (observed live: pyte screen divergence latched COMPLETED through
    # a whole busy codex turn).
    last_screen_fp: str | None = None
    # FX181 B1: quiescence-scoped quiet clock. Deliberately SEPARATE from
    # idle_since: idle_since drives the per-worker stall notice (notify_due),
    # whose semantics must stay byte-unchanged (AC3), and which treats ERROR as
    # not-idle. D2 row 1 makes ERROR a TERMINAL state for the aggregate
    # quiescence predicate, so the quiescence clock runs on IDLE/COMPLETED/ERROR
    # and is cleared by every other status. Widening idle_since itself would
    # have made the per-worker notifier fire on ERROR panes.
    quiet_since: float | None = None
    generation: int = 1
    revision: int = 0
    auto_resumed: bool = False
    resume_reserved_at: float | None = None
    auto_resume_attempted_at: str | None = None
    waiting_last_push_at: float | None = None
    # F228-b: processing-no-progress tracker
    processing_since: float | None = None       # monotonic time PROCESSING was accepted
    last_np_fp: str | None = None               # last fingerprint taken WHILE processing
    last_progress_at: float | None = None       # monotonic time of last FP change while processing
    np_fired_key: tuple[int, float] | None = None  # (generation, processing_since) dedup
    last_np_hint: str | None = None             # sanitized bounded last-line hint from filtered tail
    # F295 Half 2 D9: absolute-age wedge arm (grok_cli only)
    wedge_fired_key: tuple[int, float] | None = None  # (generation, processing_since) dedup
    wedge_flagged: bool = False                 # whether wedge_suspect is currently set


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
    phase_p_waiting: bool


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
    source_generation: int = 0
    kind: str = "stall"


@dataclass(frozen=True)
class ReservedChainNotice:
    notice: WatchdogNotice
    key: tuple[str, int, str, int]


@dataclass
class _RetiredMember:
    """D3: a dead worker that still owes a callback."""

    terminal_id: str
    caller_id: str
    generation: int
    profile: str
    retired_at: float
    last_status: str = "dead"


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
    def __init__(
        self,
        grace_seconds: int = STALLED_CALLBACK_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.grace_seconds = grace_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._episodes: dict[str, _Episode] = {}
        self._waiting_inbox_episodes: dict[str, WaitingInboxEpisode] = {}
        self._waiting_inbox_last_push: dict[str, float] = {}
        self._ready_backlog_episodes: dict[str, ReadyBacklogEpisode] = {}
        self._paused: set[str] = set()
        self._generation_by_terminal: dict[str, int] = {}
        self._callback_fences: dict[str, int] = {}
        self._chain_notified: set[tuple[str, int, str, int]] = set()
        self._parity_clock = clock
        # FX181 D3: dead members still owed, keyed by caller_id -> terminal_id -> _RetiredMember
        self._dead_owed: dict[str, dict[str, _RetiredMember]] = {}
        # FX181 D5: dedup key per caller, set only after successful persist
        self._quiescence_last_fired: dict[str, tuple[tuple[str, int], ...]] = {}
        # F203 D16: cadence gate — next_tick_due monotonic stamp
        self._next_tick_due: float = 0.0

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
            # FX181 B1: the quiescence clock is shifted by the same pause span
            if episode is not None and episode.quiet_since is not None:
                episode.quiet_since += elapsed
            # F228-b D4: shift NP clocks by pause duration
            if episode is not None and episode.processing_since is not None:
                episode.processing_since += elapsed
            if episode is not None and episode.last_progress_at is not None:
                episode.last_progress_at += elapsed
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
            if episode is not None and episode.quiet_since is not None:
                episode.quiet_since += elapsed
            # F228-b D4: shift NP clocks by pause duration
            if episode is not None and episode.processing_since is not None:
                episode.processing_since += elapsed
            if episode is not None and episode.last_progress_at is not None:
                episode.last_progress_at += elapsed
            if episode is not None:
                self._episodes[terminal_id] = episode
            self._paused.discard(terminal_id)

    def record_inbound_task(self, terminal_id: str, caller_id: str, profile: str) -> None:
        if caller_id.startswith("watchdog:"):
            return
        now = time.monotonic()
        wall_now = _utcnow()
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
            # FX181 D5 re-arm: new assign changes set composition
            self._quiescence_last_fired.pop(caller_id, None)

    def has_episode(self, terminal_id: str) -> bool:
        with self._lock:
            return terminal_id in self._episodes

    def emit_pre_delete_notice(self, terminal_id: str) -> WatchdogNotice | None:
        """Emit one durable notice if an open un-fired episode exists, then return it.

        Called by _delete_terminal_under_lease BEFORE clear_terminal, under the
        terminal's delivery_lock. Returns None when no notice is warranted.
        """
        with self._lock:
            episode = self._episodes.get(terminal_id)
            if episode is None:
                return None
            if episode.callback_seen:
                return None
            if episode.fired:
                return None
            caller_id = episode.caller_id
            profile = episode.profile
            generation = episode.generation
            episode.fired = True  # under _lock — atomic with the decision

        # Outside _lock: durable insert (may block on DB)
        notice = WatchdogNotice(
            terminal_id=terminal_id,
            caller_id=caller_id,
            message=(
                f"[watchdog] worker {terminal_id} ({profile}) deleted "
                f"before callback — task result may be lost"
            ),
            idle_reason=None,
            source_generation=generation,
            kind="deletion",
        )
        self._persist_notice(notice)
        return notice

    def clear_terminal(self, terminal_id: str) -> None:
        with self._lock:
            # FX181 S2: collect the callers whose owed set this delete actually
            # changes, so the dedup clear below is caller-scoped. A blanket
            # .clear() re-armed every unrelated supervisor (AC8 violation).
            affected_callers: set[str] = set()
            popped_episode = self._episodes.pop(terminal_id, None)
            if popped_episode is not None:
                affected_callers.add(popped_episode.caller_id)
            self._waiting_inbox_episodes.pop(terminal_id, None)
            self._waiting_inbox_last_push.pop(terminal_id, None)
            self._ready_backlog_episodes.pop(terminal_id, None)
            self._generation_by_terminal.pop(terminal_id, None)
            self._callback_fences.pop(terminal_id, None)
            self._chain_notified = {
                key
                for key in self._chain_notified
                if key[0] != terminal_id and key[2] != terminal_id
            }
            # FX181 D3: remove from _dead_owed in every caller's bucket
            for caller_id, caller_bucket in self._dead_owed.items():
                if caller_bucket.pop(terminal_id, None) is not None:
                    affected_callers.add(caller_id)
            # Purge empty caller buckets
            self._dead_owed = {cid: bucket for cid, bucket in self._dead_owed.items() if bucket}
            # FX181 D5 re-arm, caller-scoped: only the callers that actually owned
            # this terminal saw their set composition change.
            for caller_id in affected_callers:
                self._quiescence_last_fired.pop(caller_id, None)

    def _blockers_locked(self, worker_id: str) -> list[tuple[str, _Episode]]:
        return [
            (terminal_id, episode)
            for terminal_id, episode in self._episodes.items()
            if episode.caller_id == worker_id
            and not episode.callback_seen
            and terminal_id not in self._paused
            and terminal_exists(terminal_id)
        ]

    def record_callback_if_to_caller(self, sender_id: str, receiver_id: str) -> None:
        meta = get_terminal_metadata(sender_id)
        if not meta:
            return
        caller_identities = {meta.get("caller_id"), meta.get("caller_mailbox_id")}
        if receiver_id not in caller_identities:
            return
        with self._lock:
            if sender_id in self._paused:
                return
            episode = self._episodes.get(sender_id)
            # The episode stores ONE caller identity, but a reply may legitimately
            # arrive addressed to either the caller terminal or its mailbox -- a
            # barrier-routed callback lands on the mailbox id. Comparing only
            # against episode.caller_id silently dropped those, leaving
            # callback_seen False and firing a false "idle ... without callback"
            # push at the next episode. Accept any identity the outer gate did.
            if episode and episode.caller_id in caller_identities:
                episode.callback_seen = True
                # FX181 D5 re-arm: settlement shrinks the owed set
                self._quiescence_last_fired.pop(episode.caller_id, None)

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
            # FX193 D2: persist status on the episode for safety-gate reads
            episode.status = status
            if status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}:
                if episode.idle_since is None:
                    episode.idle_since = now
                    episode.last_screen_fp = None
            else:
                episode.idle_since = None
                episode.last_screen_fp = None
                # FX181 D5 re-arm: member back to PROCESSING clears dedup key
                if status == TerminalStatus.PROCESSING:
                    self._quiescence_last_fired.pop(episode.caller_id, None)
            # FX181 B1 / D2 row 1: the quiescence quiet clock runs on every
            # TERMINAL status — IDLE, COMPLETED **and ERROR** — and is cleared by
            # anything else. Maintained alongside idle_since so notify_due keeps
            # its exact pre-FX181 semantics (AC3).
            if status in {
                TerminalStatus.IDLE,
                TerminalStatus.COMPLETED,
                TerminalStatus.ERROR,
            }:
                if episode.quiet_since is None:
                    episode.quiet_since = now
            else:
                episode.quiet_since = None
            # F228-b: track PROCESSING entry/exit for no-progress clock
            if status == TerminalStatus.PROCESSING:
                if episode.processing_since is None:
                    # New uninterrupted processing episode begins
                    episode.processing_since = now
                    episode.last_np_fp = None
                    episode.last_progress_at = None
                    episode.np_fired_key = None
                    episode.last_np_hint = None
            else:
                # Any non-PROCESSING status ends the uninterrupted processing episode
                if episode.processing_since is not None:
                    episode.processing_since = None
                    episode.last_np_fp = None
                    episode.last_progress_at = None
                    episode.np_fired_key = None
                    episode.last_np_hint = None
                    # F295 Half 2: clear wedge state on status transition
                    episode.wedge_flagged = False
                    episode.wedge_fired_key = None
            # F97: garbage-collect completed episodes
            self._gc_fired_episodes()
        # FX193 D2: notify nudge discipline of status change (outside lock)
        try:
            from cli_agent_orchestrator.services.nudge_discipline import nudge_discipline

            nudge_discipline.record_status(terminal_id, status)
        except Exception:
            pass

        # FX194 D1: notify boundary pull service on consumption boundaries
        # (idle transition = a consumption boundary where pull can deliver)
        if status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
            try:
                from cli_agent_orchestrator.services.boundary_pull_service import (
                    boundary_pull_service,
                )

                # Look up the mailbox for this terminal
                from cli_agent_orchestrator.clients.database import SessionLocal, MailboxModel

                with SessionLocal() as db:
                    mailbox = (
                        db.query(MailboxModel)
                        .filter_by(current_terminal_id=terminal_id)
                        .first()
                    )
                    if mailbox:
                        boundary_pull_service.notify_boundary(terminal_id, mailbox.id)
            except Exception:
                pass

    def _gc_fired_episodes(self) -> None:
        """Remove episodes that have both fired and seen their callback (F97)."""
        dead = [tid for tid, ep in self._episodes.items() if ep.callback_seen and ep.fired]
        for tid in dead:
            del self._episodes[tid]

    def _fx191_convergence_tick(self) -> None:
        """FX191 D5: convergence loop — first sibling tick in the run loop.

        F203 D16: guarded by a monotonic next-due stamp of delivery.tick_s.
        The run loop re-enters faster than tick_s because asyncio.wait_for
        returns early on every status event — the loop is correct, the
        unconditional tick call was the defect.
        """
        now = time.monotonic()
        if now < self._next_tick_due:
            return
        try:
            from cli_agent_orchestrator.services.config_service import ConfigService

            tick_s = float(ConfigService.get("delivery.tick_s", 5.0))
            self._next_tick_due = now + tick_s

            from cli_agent_orchestrator.services.delivery_service import convergence_tick

            convergence_tick()
        except Exception:
            logger.debug("fx191 convergence_tick error", exc_info=True)

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
                status = receiver_state_view.snapshot_view(
                    "watchdog.cached_status",
                    terminal_id,
                    max_age_s=30.0,
                    none_behavior="watchdog",
                    monitor=status_monitor,
                )
                if status is not None:
                    self.record_status(terminal_id, status, now=now)
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
                # FX181 S1: an episode is fingerprint-tracked when EITHER clock is
                # armed. ERROR members carry quiet_since only (idle_since stays
                # None so notify_due keeps its pre-FX181 semantics, AC3), and
                # without this they had no anti-false-idle protection at all.
                # F228-b B1: PROCESSING terminals included for NP fingerprint tracking.
                and (episode.idle_since is not None or episode.quiet_since is not None
                     or episode.processing_since is not None)
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
                    or (episode.idle_since is None and episode.quiet_since is None
                        and episode.processing_since is None)
                ):
                    continue
                if episode.last_screen_fp is None:
                    episode.last_screen_fp = fingerprint
                elif episode.last_screen_fp != fingerprint:
                    # A visibly-changing pane restarts whichever clocks are armed.
                    # idle_since is only restarted, never started: an ERROR pane
                    # must not become idle-notifiable (AC3).
                    if episode.idle_since is not None:
                        episode.idle_since = now
                    # FX181 B1: the quiescence clock inherits the same
                    # anti-false-idle reset (AC7)
                    if episode.quiet_since is not None:
                        episode.quiet_since = now
                    episode.last_screen_fp = fingerprint

                # F228-b: update no-progress fingerprint for PROCESSING terminals
                if episode.processing_since is not None and episode.np_fired_key is None:
                    # Capture sanitized hint from the filtered tail (same read, no extra I/O)
                    hint_lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
                    raw_hint = hint_lines[-1] if hint_lines else ""
                    # Sanitize: terminal text is untrusted
                    sanitized_hint = raw_hint.replace('"', "'").replace('\n', ' ').replace('\r', ' ')
                    sanitized_hint = ''.join(c if c.isprintable() else '?' for c in sanitized_hint)
                    if len(sanitized_hint) > 80:
                        sanitized_hint = sanitized_hint[:77] + "..."
                    episode.last_np_hint = sanitized_hint if sanitized_hint else None

                    if episode.last_np_fp is None:
                        # First baseline (AWAITING_BASELINE -> CLOCK_RUNNING)
                        episode.last_np_fp = fingerprint
                        episode.last_progress_at = now
                    elif episode.last_np_fp != fingerprint:
                        # Progress: screen changed — reset stall clock
                        episode.last_np_fp = fingerprint
                        episode.last_progress_at = now
                    # else: same fingerprint — clock keeps running (no-op)

    def _fresh_frame_decides_running(self, terminal_id: str) -> tuple[bool, str | None]:
        from cli_agent_orchestrator.backends.registry import get_backend
        from cli_agent_orchestrator.providers.manager import provider_manager
        from cli_agent_orchestrator.services.seam_activation import receiver_state_active
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        try:
            metadata = get_terminal_metadata(terminal_id)
            provider = provider_manager.get_provider(terminal_id)
            if metadata is None or provider is None:
                return False, None
            if not receiver_state_active("watchdog.pane_classify"):
                frame = get_backend().capture_viewport(
                    metadata["tmux_session"], metadata["tmux_window"]
                )
                rows = frame.splitlines()
                from cli_agent_orchestrator.providers.screen_classification import (
                    ScreenClassification,
                    ScreenClassificationResult,
                    screen_classification_result,
                )

                if status_monitor._signal_emitting(provider):
                    classification = screen_classification_result(
                        provider.emit_screen_signals(rows),
                        (),
                        provider.capabilities.liveness_anchor,
                    )
                else:
                    classification = ScreenClassificationResult(
                        ScreenClassification(
                            provider.get_status_from_screen(rows), "none", None, None
                        ),
                        (),
                    )
                idle_reason = provider.classify_idle_reason(rows, classification)
                return (
                    classification.status == TerminalStatus.PROCESSING
                    and classification.provider_signal == "RUNNING_PATTERN",
                    idle_reason if isinstance(idle_reason, str) else None,
                )
            proof = status_monitor.prove_terminal_identity(terminal_id)
            frame = get_backend().capture_viewport(
                metadata["tmux_session"], metadata["tmux_window"]
            )
            captured_at = time.monotonic()
            rows = frame.splitlines()
            if status_monitor._signal_emitting(provider):
                from cli_agent_orchestrator.providers.screen_classification import (
                    screen_classification_result,
                )

                prior = status_monitor.receiver_state_store.prior_classification(
                    (
                        terminal_id,
                        int(metadata["lifecycle_generation"]),
                        str(metadata["tmux_window"]),
                    ),
                    prefer_fresh=True,
                )
                classification = screen_classification_result(
                    provider.emit_screen_signals(rows),
                    () if prior is None else prior.signals,
                    provider.capabilities.liveness_anchor,
                )
            else:
                from cli_agent_orchestrator.providers.screen_classification import (
                    ScreenClassification,
                    ScreenClassificationResult,
                )

                legacy_status = provider.get_status_from_screen(rows)
                classification = ScreenClassificationResult(
                    ScreenClassification(legacy_status, "none", None, None), ()
                )
            token = status_monitor.publish_fresh_observation(
                terminal_id,
                rows,
                captured_at,
                classification,
                "fresh_capture",
                proof,
            )
            view = status_monitor.receiver_state_store.snapshot_view(
                (
                    terminal_id,
                    int(metadata["lifecycle_generation"]),
                    str(metadata["tmux_window"]),
                ),
                require_fresh=True,
                max_age_s=2.0,
                recovery_state=metadata.get("recovery_state"),
                token=token,
            )
            if view is None or view.raw_classification is None:
                return False, None
            classification = view.raw_classification
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
                        phase_p_waiting=bool(self._blockers_locked(terminal_id)),
                    )
                )

        due: list[WatchdogNotice] = []
        for candidate in candidates:
            metadata = get_terminal_metadata(candidate.terminal_id)
            callback_status = None
            provider = metadata.get("provider") if metadata is not None else None
            auto_resume_applicable = (
                self._auto_resume_enabled() and provider in AUTO_RESUME_PROVIDERS
            )
            suppress = False
            fallback_idle_reason = None
            second_callback_status = None
            if not candidate.phase_p_waiting:
                callback_status = get_callback_status_since(
                    candidate.terminal_id,
                    candidate.caller_id,
                    candidate.episode_started_wall_at,
                )
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
                    # FX181 D3: retire into _dead_owed instead of bare pop.
                    # The per-worker notice path keeps existing semantics (cannot
                    # nag about a terminal it cannot describe); only owed-ness survives.
                    episode_to_retire = self._episodes.pop(candidate.terminal_id, None)
                    if episode_to_retire is not None and not episode_to_retire.callback_seen:
                        caller = episode_to_retire.caller_id
                        bucket = self._dead_owed.setdefault(caller, {})
                        bucket[candidate.terminal_id] = _RetiredMember(
                            terminal_id=candidate.terminal_id,
                            caller_id=caller,
                            generation=episode_to_retire.generation,
                            profile=episode_to_retire.profile,
                            retired_at=now,
                            last_status="dead",
                        )
                        # D5 re-arm: composition changed
                        self._quiescence_last_fired.pop(caller, None)
                    continue
                if not self._candidate_valid(candidate, current_episode):
                    continue
                assert current_episode is not None
                assert current_episode.idle_since is not None
                if int(now - current_episode.idle_since) < self.grace_seconds:
                    continue
                blockers = self._blockers_locked(candidate.terminal_id)
                if candidate.phase_p_waiting and not blockers:
                    continue
                if blockers:
                    oldest_inbound_at = min(episode.inbound_at for _, episode in blockers)
                    last_push = current_episode.waiting_last_push_at
                    if now - oldest_inbound_at >= WATCHDOG_WAITING_ESCALATE_S and (
                        last_push is None or now - last_push >= WATCHDOG_WAITING_REPEAT_FLOOR_S
                    ):
                        current_episode.waiting_last_push_at = now
                        blocker_ids = ", ".join(sorted(terminal_id for terminal_id, _ in blockers))
                        due.append(
                            WatchdogNotice(
                                terminal_id=candidate.terminal_id,
                                caller_id=candidate.caller_id,
                                message=(
                                    f"[watchdog] worker {current_episode.profile}-{candidate.terminal_id} "
                                    f"lawfully waiting on delegated "
                                    f"sub-workers [{blocker_ids}], oldest outstanding "
                                    f"{int(now - oldest_inbound_at)}s — not stalled; chain may need "
                                    "a peek if this repeats."
                                ),
                                idle_reason=None,
                                source_generation=current_episode.generation,
                                kind="waiting",
                            )
                        )
                    continue
                if callback_status in {
                    MessageStatus.PENDING,
                    MessageStatus.HELD,
                    MessageStatus.DELIVERING,
                }:
                    continue
                if callback_status in {MessageStatus.DELIVERED, MessageStatus.DIGESTED}:
                    current_episode.callback_seen = True
                    continue
                if second_callback_status in {
                    MessageStatus.PENDING,
                    MessageStatus.HELD,
                    MessageStatus.DELIVERING,
                }:
                    continue
                if second_callback_status in {MessageStatus.DELIVERED, MessageStatus.DIGESTED}:
                    current_episode.callback_seen = True
                    continue
                if suppress:
                    current_episode.idle_since = now
                    # FX181 B1: the fresh frame that proved this pane running is a
                    # liveness proof for the quiescence clock too. Resetting only
                    # idle_since let the aggregate predicate ring off a stale
                    # quiet_since immediately after this very tick suppressed its
                    # own per-worker notice.
                    if current_episode.quiet_since is not None:
                        current_episode.quiet_since = now
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
            message=f"[watchdog] worker {episode.profile}-{candidate.terminal_id} "
            f"idle {idle_seconds}s without callback"
            f"{f' [reason: {idle_reason}]' if idle_reason is not None else ''}{suffix}",
            idle_reason=idle_reason,
            source_generation=episode.generation,
        )

    def _execute_auto_resume(
        self, action: AutoResumeAction, enqueue_monotonic: float
    ) -> WatchdogNotice | None:
        from cli_agent_orchestrator.services.auto_responder import auto_responder
        from cli_agent_orchestrator.services.inbox_service import get_delivery_lock, inbox_service
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        idle_reason = None
        try:
            probe_result = status_monitor.probe_screen_status(action.terminal_id)
            if hasattr(probe_result, "status"):
                probe_status, probe_meta = probe_result.status, probe_result.meta
            else:
                probe_status, probe_meta = probe_result[0], probe_result[1]
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
                if (
                    second_status in {MessageStatus.DELIVERED, MessageStatus.DIGESTED}
                    and episode is not None
                ):
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
                        # FX181 B1: an accepted auto-resume nudge is a liveness
                        # proof — the quiescence clock restarts with idle_since.
                        if episode.quiet_since is not None:
                            episode.quiet_since = enqueue_monotonic
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
                # F136-D17: use request_delivery, never synchronous on loop
                from cli_agent_orchestrator.services.inbox_service import request_delivery

                request_delivery(action.terminal_id)
            except Exception:
                logger.exception("Failed to deliver auto-resume for %s", action.terminal_id)
        return None

    def _reserve_chain_notice(
        self, notice: WatchdogNotice, now: float
    ) -> ReservedChainNotice | None:
        if notice.kind != "stall":
            return None
        with self._lock:
            target_episode = self._episodes.get(notice.terminal_id)
            if target_episode is None or target_episode.generation != notice.source_generation:
                return None
            worker_id = target_episode.caller_id
            worker_episode = self._episodes.get(worker_id)
            if (
                worker_episode is None
                or worker_episode.callback_seen
                or worker_episode.fired
                or worker_episode.idle_since is None
            ):
                return None
            blockers = dict(self._blockers_locked(worker_id))
            if blockers.get(notice.terminal_id) is not target_episode:
                return None
            key = (
                worker_id,
                worker_episode.generation,
                notice.terminal_id,
                notice.source_generation,
            )
            if key in self._chain_notified:
                return None
            self._chain_notified.add(key)
            chain_notice = WatchdogNotice(
                terminal_id=worker_id,
                caller_id=worker_episode.caller_id,
                message=(
                    f"[watchdog] chain stalled: worker {worker_episode.profile}-{worker_id} "
                    f"has been waiting "
                    f"{int(now - worker_episode.inbound_at)}s on its sub-worker "
                    f"{target_episode.profile}-{notice.terminal_id}, which is now idle "
                    f"{int(now - target_episode.inbound_at)}s without callback — "
                    f"{worker_episode.profile}-{worker_id} cannot return until this is resolved."
                ),
                idle_reason=None,
                source_generation=worker_episode.generation,
                kind="chain",
            )
            return ReservedChainNotice(chain_notice, key)

    def _release_chain_reservation(self, key: tuple[str, int, str, int]) -> None:
        with self._lock:
            self._chain_notified.discard(key)

    @staticmethod
    def _persist_notice(notice: WatchdogNotice) -> None:
        handled = insert_barrier_escalation_message(
            notice.terminal_id,
            notice.caller_id,
            notice.message,
            notice.idle_reason,
        )
        if handled is None:
            from cli_agent_orchestrator.services.mailbox_service import create_routed_inbox_message

            create_routed_inbox_message(
                f"watchdog:{notice.terminal_id}",
                notice.caller_id,
                notice.message,
            )

    def notify_due(self, registry: PluginRegistry | None = None) -> None:
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        now = self._clock()
        notices = self.collect_due_notifications(now=now)
        jobs = [(notice, self._reserve_chain_notice(notice, now)) for notice in notices]
        chain_pairs = {
            (reservation.notice.terminal_id, reservation.notice.caller_id)
            for _, reservation in jobs
            if reservation is not None
        }
        jobs = [
            (notice, reservation)
            for notice, reservation in jobs
            if notice.kind != "waiting" or (notice.terminal_id, notice.caller_id) not in chain_pairs
        ]

        for notice, reservation in jobs:
            try:
                self._persist_notice(notice)
            except Exception:
                logger.exception("Failed to push stalled-callback watchdog notification")
                if reservation is not None:
                    self._release_chain_reservation(reservation.key)
                continue

            chain_persisted = False
            if reservation is not None:
                try:
                    self._persist_notice(reservation.notice)
                    chain_persisted = True
                except Exception:
                    self._release_chain_reservation(reservation.key)
                    logger.exception("Failed to persist watchdog chain notification")

            try:
                # F136-D17: request_delivery, never synchronous delivery on loop
                from cli_agent_orchestrator.services.inbox_service import request_delivery

                request_delivery(notice.caller_id)
            except Exception:
                logger.exception("Failed to deliver stalled-callback watchdog notification")
            if chain_persisted and reservation is not None:
                try:
                    from cli_agent_orchestrator.services.inbox_service import (
                        request_delivery as _rd,
                    )

                    _rd(reservation.notice.caller_id)
                except Exception:
                    logger.exception("Failed to deliver watchdog chain notification")

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

            status = receiver_state_view.snapshot_view(
                "watchdog.waiting_inbox_gate",
                terminal_id,
                max_age_s=30.0,
                none_behavior="watchdog",
                monitor=status_monitor,
            )
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
                # F203 D15: supervisor terminals have no caller_id by construction.
                # Check if this is a supervisor-role terminal — if so, self-notify
                # via the obligation path instead of refusing.
                agent_profile = metadata.get("agent_profile", "")
                if not caller_id and agent_profile in ("supervisor", "code_supervisor", "chao_supervisor"):
                    # Supervisor self-notify: create obligation targeting own mailbox
                    try:
                        from cli_agent_orchestrator.services.delivery_service import (
                            _create_self_notify_obligation,
                        )
                        _create_self_notify_obligation(terminal_id)
                        logger.debug(
                            "waiting-inbox watchdog: supervisor self-notify for %s",
                            terminal_id,
                        )
                    except Exception:
                        logger.debug(
                            "waiting-inbox watchdog: supervisor self-notify failed for %s",
                            terminal_id, exc_info=True,
                        )
                    continue
                # Worker with corrupt caller_id — original refusal behavior
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
            dn = f"{name}-{terminal_id}" if name != "unknown" else terminal_id
            message = (
                f"[waiting-inbox watchdog] {dn} has had pending "
                f"inbox messages while status=waiting_user_answer for {age}s with no "
                "auto-responder episode open — it may be stuck on an unrecognized dialog "
                "or a false-WAITING parse. Peek it (peek_terminal / tmux attach) and nudge "
                "or answer manually. This alert fires at most once per stuck episode "
                "(floor 300s)."
            )
            try:
                from cli_agent_orchestrator.services.mailbox_service import (
                    create_routed_inbox_message,
                )

                create_routed_inbox_message(f"watchdog:{terminal_id}", caller_id, message)
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
            status = receiver_state_view.snapshot_view(
                "watchdog.ready_backlog_gate",
                terminal_id,
                max_age_s=30.0,
                none_behavior="watchdog",
                monitor=status_monitor,
            )
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
                    # F203 D15: supervisor self-notify for caller-less supervisor terminals
                    agent_profile = metadata.get("agent_profile", "")
                    if not caller_id and agent_profile in ("supervisor", "code_supervisor", "chao_supervisor"):
                        try:
                            from cli_agent_orchestrator.services.delivery_service import (
                                _create_self_notify_obligation,
                            )
                            _create_self_notify_obligation(terminal_id)
                            logger.debug(
                                "ready-backlog watchdog: supervisor self-notify for %s",
                                terminal_id,
                            )
                        except Exception:
                            logger.debug(
                                "ready-backlog watchdog: supervisor self-notify failed for %s",
                                terminal_id, exc_info=True,
                            )
                        continue
                    # Worker with corrupt caller_id
                    logger.warning(
                        "ready-backlog watchdog: refusing invalid caller for terminal %s",
                        terminal_id,
                    )
                    episode.fired = True
                    continue
                episode.fired = True

            age = int(observation.oldest_pending_age_seconds)
            message_id = observation.oldest_message_id
            profile = metadata.get("agent_profile") or ""
            dn = f"{profile}-{terminal_id}" if profile else terminal_id
            message = (
                f"[ready-backlog watchdog] {dn} has pending message "
                f"{message_id} aged {age}s while status={status.value} with no open "
                "delivery attempt or attempt progress; inspect "
                f"`cao messages trace {message_id}`. Reconciliation remains the retry owner."
            )
            try:
                from cli_agent_orchestrator.services.mailbox_service import (
                    create_routed_inbox_message,
                )

                create_routed_inbox_message(f"watchdog:{terminal_id}", caller_id, message)
            except Exception:
                logger.warning(
                    "Failed to push ready-backlog watchdog notification for %s",
                    terminal_id,
                    exc_info=True,
                )

    def tick_quiescence(self, now: float | None = None) -> None:
        """FX181 D4/D8: evaluate the RING predicate for each caller with owed callbacks.

        Wrapped fail-silent (D8): exceptions never escape, never starve sibling ticks.
        Per-caller evaluation is isolated: one caller's exception never suppresses another's ring.
        """
        try:
            self._tick_quiescence_inner(now)
        except Exception:
            logger.exception("tick_quiescence outer fault (fail-silent D8)")

    def _retire_dead_episodes(self, now: float) -> None:
        """F185: fast-path retirement of dead workers into _dead_owed.

        Death is already a terminal signal — waiting out the per-worker notifier
        grace (self.grace_seconds) is only meaningful for live-but-quiet workers.
        This method probes each tracked episode for terminal death (metadata gone)
        and retires it immediately so the quiescence clock starts from death, not
        from notifier-grace expiry.
        """
        # Collect candidates under lock (IDs only), probe metadata outside lock.
        with self._lock:
            candidates = [
                tid
                for tid, ep in self._episodes.items()
                if tid not in self._paused and not ep.callback_seen and not ep.fired
            ]

        dead_ids: list[str] = []
        for tid in candidates:
            if get_terminal_metadata(tid) is None:
                dead_ids.append(tid)

        if not dead_ids:
            return

        with self._lock:
            for tid in dead_ids:
                episode = self._episodes.pop(tid, None)
                if episode is None:
                    continue  # already removed by another path
                if episode.callback_seen:
                    # Callback arrived between the probe and the lock — restore.
                    self._episodes[tid] = episode
                    continue
                caller = episode.caller_id
                bucket = self._dead_owed.setdefault(caller, {})
                bucket[tid] = _RetiredMember(
                    terminal_id=tid,
                    caller_id=caller,
                    generation=episode.generation,
                    profile=episode.profile,
                    retired_at=now,
                    last_status="dead",
                )
                # D5 re-arm: composition changed
                self._quiescence_last_fired.pop(caller, None)

    def _tick_quiescence_inner(self, now: float | None = None) -> None:
        from cli_agent_orchestrator.services.config_service import ConfigService

        # D7: check flag per-tick
        if not ConfigService.get("supervisor.watchdog.quiescence", False):
            return

        now = now if now is not None else time.monotonic()
        grace = float(ConfigService.get("supervisor.watchdog.quiescence_grace_s", 120.0))

        # F185: retire dead workers immediately so they enter the quiescence
        # clock on the quiescence grace, not the notifier grace.
        self._retire_dead_episodes(now)

        # Build per-caller owed sets (live + dead members)
        with self._lock:
            callers: dict[str, list[tuple[str, int, str, float | None, str]]] = {}
            # Live members from _episodes
            for terminal_id, episode in self._episodes.items():
                if episode.callback_seen:
                    continue
                if terminal_id in self._paused:
                    # N6: paused terminals are deliberately excluded. A paused
                    # episode's clocks are frozen mid-quarantine (pause_terminal /
                    # resume_terminal), so its quiet age is not a real observation;
                    # counting it would let a quarantine window masquerade as
                    # quiescence. It rejoins the owed set on resume.
                    continue
                cid = episode.caller_id
                callers.setdefault(cid, []).append(
                    (
                        terminal_id,
                        episode.generation,
                        episode.profile,
                        # B1: the quiescence-scoped clock (IDLE/COMPLETED/ERROR), not
                        # idle_since — see _Episode.quiet_since.
                        episode.quiet_since,
                        "live",
                    )
                )
            # Dead members from _dead_owed
            for cid, bucket in self._dead_owed.items():
                for terminal_id, member in bucket.items():
                    callers.setdefault(cid, []).append(
                        (
                            terminal_id,
                            member.generation,
                            member.profile,
                            member.retired_at,
                            "dead",
                        )
                    )
            # Snapshot dedup keys
            last_fired_snapshot = dict(self._quiescence_last_fired)

        if not callers:
            return

        for caller_id, members in callers.items():
            try:
                self._evaluate_caller_quiescence(
                    caller_id, members, now, grace, last_fired_snapshot.get(caller_id)
                )
            except Exception:
                # D8: per-caller isolation — one caller's exception cannot suppress another's
                logger.exception(
                    "tick_quiescence per-caller fault for %s (fail-silent D8)", caller_id
                )

    def _evaluate_caller_quiescence(
        self,
        caller_id: str,
        members: list[tuple[str, int, str, float | None, str]],
        now: float,
        grace: float,
        last_fired_key: tuple[tuple[str, int], ...] | None,
    ) -> None:
        """D4: evaluate the RING predicate for a single caller."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        if not members:
            return  # D4: empty set never rings

        # D9: the ring target. BOTH halves are required before a caller's stores
        # are dropped: the terminal row must be gone AND no supervisor mailbox may
        # route to a live terminal. A supervisor mid-rebind has metadata None while
        # its mailbox already points at the successor — dropping there would lose
        # the dead-lane ledger and silence the ring the successor is owed.
        ring_target = caller_id
        if get_terminal_metadata(caller_id) is None:
            successor = _supervisor_mailbox_live_terminal(caller_id)
            if successor is None:
                # Dead supervisor, no mailbox successor — drop stores (D9)
                # N8 divergence, deliberate: only the FX181-owned stores are
                # dropped here. The live `_episodes` rows are left to the
                # per-worker notifier, which owns their lifecycle and retires
                # them on its own metadata-None path — touching them from the
                # quiescence tick would change existing notifier behavior, which
                # AC3 forbids.
                with self._lock:
                    self._dead_owed.pop(caller_id, None)
                    self._quiescence_last_fired.pop(caller_id, None)
                return
            # The debt follows the mailbox, not the dead pane: stores are retained
            # and the ring is addressed to the live successor terminal.
            ring_target = successor

        # AC6/D4: a settlement discovered by the probe shrinks the set, and the
        # shrunken set is re-evaluated in the SAME tick (loop until stable or ring).
        remaining = list(members)
        while True:
            # D4 step 1: every member must be terminal (D2) and quiet >= grace.
            # The status resolved here is carried forward to the message build
            # (N7) — re-sampling it after the predicate passed could report a
            # different status than the one the predicate accepted.
            classified: list[tuple[str, int, str, float | None, str, str]] = []
            for terminal_id, generation, profile, quiet_since, kind in remaining:
                if kind == "live":
                    status = receiver_state_view.snapshot_view(
                        "watchdog.cached_status",
                        terminal_id,
                        max_age_s=30.0,
                        none_behavior="watchdog",
                        monitor=status_monitor,
                    )
                    if status is None:
                        return  # indeterminate — no ring
                    if status == TerminalStatus.PROCESSING:
                        return  # busy — not quiescent
                    if status == TerminalStatus.WAITING_USER_ANSWER:
                        return  # not terminal — no ring
                    # D2: IDLE, COMPLETED and ERROR are the terminal statuses;
                    # everything else (UNKNOWN, RENDER_UNCERTAIN, anything the
                    # enum grows later) is indeterminate — fail toward silence.
                    last_status = _QUIESCENT_STATUS_LABELS.get(status)
                    if last_status is None:
                        return  # indeterminate — no ring
                else:
                    last_status = "dead"  # quiet clock runs from retired_at
                if quiet_since is None:
                    return  # no quiet clock
                if now - quiet_since < grace:
                    return  # inside grace
                classified.append(
                    (terminal_id, generation, profile, quiet_since, kind, last_status)
                )

            # D4 step 2: no member has an undelivered queued callback
            settled: set[str] = set()
            for terminal_id, generation, profile, quiet_since, kind, last_status in classified:
                if kind == "dead":
                    continue  # dead members have no pending rows by definition
                with self._lock:
                    ep = self._episodes.get(terminal_id)
                    if ep is None:
                        continue
                    episode_started_wall_at = ep.episode_started_wall_at
                cb_status = get_callback_status_since(
                    terminal_id, caller_id, episode_started_wall_at
                )
                if cb_status in {
                    MessageStatus.PENDING,
                    MessageStatus.HELD,
                    MessageStatus.DELIVERING,
                }:
                    return  # queued callback exists — suppress the whole set
                if cb_status in {MessageStatus.DELIVERED, MessageStatus.DIGESTED}:
                    # Settlement: mark callback_seen, shrink set
                    with self._lock:
                        ep = self._episodes.get(terminal_id)
                        if ep is not None:
                            ep.callback_seen = True
                            self._quiescence_last_fired.pop(caller_id, None)
                    settled.add(terminal_id)

            if settled:
                remaining = [m for m in remaining if m[0] not in settled]
                # The settlement above cleared the dedup key, so the re-evaluated
                # (smaller) set is compared against an empty key.
                last_fired_key = None
                if not remaining:
                    return  # D4: empty set never rings
                continue
            break

        # D5 step 3: dedup key check
        current_key = tuple(sorted((tid, gen) for tid, gen, _, _, _, _ in classified))
        if current_key == last_fired_key:
            return  # unchanged key — at-most-once

        # All predicates pass — RING (D6)
        # Build aggregated message from the predicate-time observations
        lines = []
        for terminal_id, generation, profile, quiet_since, kind, last_status in sorted(
            classified, key=lambda m: m[0]
        ):
            quiet_s = int(now - quiet_since) if quiet_since is not None else 0
            lines.append(
                f"  - {profile}-{terminal_id} last={last_status} quiet={quiet_s}s gen={generation}"
            )

        n = len(classified)
        message = (
            f"[quiescence watchdog] All {n} lane(s) you assigned have gone quiet "
            f"without a delivered callback:\n"
            + "\n".join(lines)
            + "\nNo callback is queued from any of them. Likely: finished-but-swallowed "
            "delivery, or worker death.\n"
            "Check each lane (list_messages / peek_terminal / fleet) before assuming failure."
        )

        # D6: persist via create_routed_inbox_message, then request_delivery
        try:
            from cli_agent_orchestrator.services.mailbox_service import create_routed_inbox_message

            create_routed_inbox_message(f"watchdog:quiescence:{caller_id}", ring_target, message)
        except Exception:
            # D5 ordering: key NOT set on persist failure — next tick retries
            logger.warning(
                "Failed to persist quiescence ring for caller %s", caller_id, exc_info=True
            )
            return

        # D5: set dedup key ONLY after successful persist
        with self._lock:
            self._quiescence_last_fired[caller_id] = current_key

        # Request delivery (F136-D17: never synchronous on loop)
        try:
            from cli_agent_orchestrator.services.inbox_service import request_delivery

            request_delivery(ring_target)
        except Exception:
            logger.warning(
                "Failed to request delivery for quiescence ring to %s",
                ring_target,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # F228-b: processing-no-progress watchdog
    # ------------------------------------------------------------------

    def tick_no_progress(self, now: float | None = None) -> None:
        """F228-b: heuristic advisory for workers stuck at PROCESSING with no screen change."""
        try:
            self._tick_no_progress_inner(now)
        except Exception:
            logger.exception("tick_no_progress outer fault (fail-silent)")

    def _tick_no_progress_inner(self, now: float | None = None) -> None:
        from cli_agent_orchestrator.services.config_service import ConfigService

        if not ConfigService.get("supervisor.watchdog.no_progress", True):
            return

        now = now if now is not None else time.monotonic()
        grace = float(ConfigService.get("supervisor.watchdog.no_progress_grace_s", 300.0))
        grace = max(60.0, min(3600.0, grace))

        # Collect candidates: PROCESSING, baseline taken, stall >= grace, not fired
        candidates: list[tuple[str, _Episode]] = []
        with self._lock:
            for terminal_id, episode in self._episodes.items():
                if terminal_id in self._paused:
                    continue
                if episode.processing_since is None:
                    continue
                if episode.last_progress_at is None:
                    continue  # no baseline yet (AWAITING_BASELINE)
                if episode.np_fired_key is not None:
                    continue  # already fired this processing episode
                if now - episode.last_progress_at < grace:
                    continue
                candidates.append((terminal_id, episode))

        if not candidates:
            return

        for terminal_id, episode in candidates:
            try:
                self._evaluate_no_progress(terminal_id, episode, now, grace)
            except Exception:
                logger.exception(
                    "tick_no_progress per-terminal fault for %s (fail-silent)", terminal_id
                )

    def _evaluate_no_progress(
        self, terminal_id: str, episode: _Episode, now: float, grace: float
    ) -> None:
        """D5 recheck + publish for a single terminal."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        # D5: recheck status immediately before publish
        status = receiver_state_view.snapshot_view(
            "watchdog.np_recheck",
            terminal_id,
            max_age_s=5.0,
            none_behavior="watchdog",
            monitor=status_monitor,
        )
        if status != TerminalStatus.PROCESSING:
            # Status transitioned — clear NP state, no alert
            with self._lock:
                ep = self._episodes.get(terminal_id)
                if ep is episode:
                    ep.processing_since = None
                    ep.last_np_fp = None
                    ep.last_progress_at = None
                    ep.np_fired_key = None
                    ep.last_np_hint = None
            return

        # Confirm generation hasn't changed and episode is still candidate
        with self._lock:
            ep = self._episodes.get(terminal_id)
            if ep is not episode:
                return  # episode replaced
            if ep.np_fired_key is not None:
                return  # fired between candidacy and recheck
            if ep.processing_since is None or ep.last_progress_at is None:
                return  # cleared between candidacy and recheck
            stall_generation = ep.generation
            processing_since = ep.processing_since
            last_progress_at = ep.last_progress_at
            caller_id = ep.caller_id
            profile = ep.profile
            hint = ep.last_np_hint or "<none>"

        # D7: check caller is alive
        if get_terminal_metadata(caller_id) is None:
            return  # dead caller — no one to alert

        # Compose alert (D6)
        processing_age = int(now - processing_since)
        stall_age = int(now - last_progress_at)

        message = (
            f"[no-progress advisory] worker {profile}-{terminal_id} has been processing "
            f"for {processing_age}s with no visible output change for {stall_age}s "
            f"(gen={stall_generation}, last_visible=\"{hint}\").\n"
            f"This is a HEURISTIC — a silent legitimate tool can produce a static screen.\n"
            f"Check: peek_terminal {terminal_id} | If confirmed stuck: Ctrl-C or delete_terminal."
        )

        # D7: persist via create_routed_inbox_message (persists + signals delivery internally)
        try:
            from cli_agent_orchestrator.services.mailbox_service import create_routed_inbox_message

            create_routed_inbox_message(
                f"watchdog:no_progress:{terminal_id}", caller_id, message
            )
        except Exception:
            # D2 ordering: fired key NOT set on persist failure — next tick retries
            logger.warning(
                "Failed to persist no-progress alert for %s", terminal_id, exc_info=True
            )
            return

        # D2: set fired key ONLY after successful persist
        with self._lock:
            ep = self._episodes.get(terminal_id)
            if ep is episode and ep.generation == stall_generation:
                ep.np_fired_key = (stall_generation, processing_since)

    # -----------------------------------------------------------------------
    # F295 Half 2 D9: grok wedge watchdog (absolute-age arm, flag+notify only)
    # -----------------------------------------------------------------------

    def tick_wedge(self) -> None:
        """F295 Half 2: absolute-age arm for grok_cli terminals (D7/D9/D10)."""
        try:
            self._tick_wedge_inner()
        except Exception:
            logger.exception("tick_wedge outer fault (fail-silent)")

    def _tick_wedge_inner(self) -> None:
        from cli_agent_orchestrator.services.config_service import ConfigService

        if not ConfigService.get("supervisor.watchdog.grok_wedge", True):
            return

        now = time.monotonic()
        wedge_age_s = float(ConfigService.get("supervisor.watchdog.grok_wedge_age_s", 900.0))
        wedge_age_s = max(300.0, min(7200.0, wedge_age_s))
        if wedge_age_s == 0:
            return  # 0 disables (D14)

        # Collect candidates: grok_cli, PROCESSING, age >= wedge_age_s, not fired
        candidates: list[tuple[str, _Episode]] = []
        with self._lock:
            for terminal_id, episode in self._episodes.items():
                if terminal_id in self._paused:
                    continue
                if episode.processing_since is None:
                    continue
                if episode.wedge_fired_key is not None:
                    continue  # already fired for this processing episode
                if now - episode.processing_since < wedge_age_s:
                    continue
                candidates.append((terminal_id, episode))

        if not candidates:
            return

        for terminal_id, episode in candidates:
            try:
                self._evaluate_wedge(terminal_id, episode, now)
            except Exception:
                logger.exception(
                    "tick_wedge per-terminal fault for %s (fail-silent)", terminal_id
                )

    def _evaluate_wedge(self, terminal_id: str, episode: _Episode, now: float) -> None:
        """D9/D10/D11: recheck + flag + notify for a single grok_cli terminal."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        # D9: scope to grok_cli only
        meta = get_terminal_metadata(terminal_id)
        if meta is None:
            # D11: terminal reaped between candidacy and recheck → no flag, no notice
            with self._lock:
                ep = self._episodes.get(terminal_id)
                if ep is episode:
                    ep.wedge_fired_key = None
                    ep.wedge_flagged = False
            return
        if meta.get("provider") != "grok_cli":
            return

        # D5-style recheck: status must still be PROCESSING
        from cli_agent_orchestrator.services.stalled_callback_watchdog import receiver_state_view

        status = receiver_state_view.snapshot_view(
            "watchdog.wedge_recheck",
            terminal_id,
            max_age_s=5.0,
            none_behavior="watchdog",
            monitor=status_monitor,
        )
        if status != TerminalStatus.PROCESSING:
            # Status transitioned — clear wedge state
            self._clear_wedge_flag(terminal_id, episode)
            return

        # Confirm episode is still the same
        with self._lock:
            ep = self._episodes.get(terminal_id)
            if ep is not episode:
                return
            if ep.wedge_fired_key is not None:
                return  # fired between candidacy and recheck
            if ep.processing_since is None:
                return
            generation = ep.generation
            processing_since = ep.processing_since
            caller_id = ep.caller_id
            profile = ep.profile

        processing_age = int(now - processing_since)

        # D12: flag wedge_suspect in system metadata
        try:
            from cli_agent_orchestrator.clients.database import merge_terminal_system_metadata

            merge_terminal_system_metadata(terminal_id, {"wedge_suspect": True})
        except Exception:
            logger.warning("F295: failed to set wedge_suspect for %s", terminal_id, exc_info=True)

        # D11: notify caller, fallback to supervisor
        recipient = caller_id
        if get_terminal_metadata(caller_id) is None:
            from cli_agent_orchestrator.services.mailbox_service import (
                get_current_supervisor_terminal_id,
            )

            recipient = get_current_supervisor_terminal_id()
        if not recipient:
            logger.info("F295 wedge: no recipient for %s, dropping notice", terminal_id)
        else:
            message = (
                f"[wedge-watchdog] grok worker {profile}-{terminal_id} has been PROCESSING "
                f"for {processing_age}s (gen={generation}). Possible relay wedge.\n"
                f"Check: peek_terminal {terminal_id}\n"
                f"Remedy: delete_terminal {terminal_id} then re-assign."
            )
            try:
                from cli_agent_orchestrator.services.mailbox_service import (
                    create_routed_inbox_message,
                )

                create_routed_inbox_message(
                    f"watchdog:wedge:{terminal_id}", recipient, message
                )
            except Exception:
                logger.warning(
                    "F295 wedge: failed to send notice for %s", terminal_id, exc_info=True
                )
                return  # D2 ordering: don't set fired key if persist fails

        # Set fired key ONLY after successful persist (D2 ordering)
        with self._lock:
            ep = self._episodes.get(terminal_id)
            if ep is episode and ep.generation == generation:
                ep.wedge_fired_key = (generation, processing_since)
                ep.wedge_flagged = True

    def _clear_wedge_flag(self, terminal_id: str, episode: _Episode) -> None:
        """Clear wedge_suspect from system metadata and episode state."""
        with self._lock:
            ep = self._episodes.get(terminal_id)
            if ep is episode and ep.wedge_flagged:
                ep.wedge_flagged = False
        try:
            from cli_agent_orchestrator.clients.database import merge_terminal_system_metadata

            merge_terminal_system_metadata(terminal_id, {"wedge_suspect": None})
        except Exception:
            pass  # best effort

    async def run(self, registry: PluginRegistry | None = None) -> None:
        from cli_agent_orchestrator.services import seam_parity

        queue = bus.subscribe("terminal.*.status")
        logger.info("StalledCallbackWatchdog started")
        interval = max(1.0, min(5.0, float(self.grace_seconds)))
        next_parity_sweep = self._parity_clock() + 60.0
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
                await asyncio.to_thread(self._fx191_convergence_tick)
                await asyncio.to_thread(self.poll_unarmed_statuses)
                await asyncio.to_thread(self.refresh_screen_fingerprints)
                await asyncio.to_thread(self.notify_due, registry)
                await asyncio.to_thread(self.tick_waiting_inbox, registry)
                await asyncio.to_thread(self.tick_ready_backlog, registry)
                await asyncio.to_thread(self.tick_quiescence)
                await asyncio.to_thread(self.tick_no_progress)
                await asyncio.to_thread(self.tick_wedge)
                parity_now = self._parity_clock()
                if parity_now >= next_parity_sweep:
                    next_parity_sweep = parity_now + 60.0
                    await asyncio.to_thread(seam_parity.sweep)
            except Exception:
                logger.exception("StalledCallbackWatchdog error")


stalled_callback_watchdog = StalledCallbackWatchdog()
