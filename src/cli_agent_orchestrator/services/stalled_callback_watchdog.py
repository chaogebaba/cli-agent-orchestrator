"""Caller-only watchdog for silent assigned workers."""

from __future__ import annotations

import asyncio
import copy
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
    DeliveryObligationModel,
    InboxModel,
    MailboxModel,
    SessionLocal,
    _utcnow,
    cancel_pending_watchdog_message,
    create_inbox_message,
    get_callback_status_since,
    get_terminal_metadata,
    insert_barrier_escalation_message,
    list_pending_receiver_ids,
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
# WP-ARCH 3c K4: the auto-resume machinery is gone. It lived inside
# ``collect_due_notifications`` — the notifier deleted with the five muted ticks
# — so its provider set, its body text, ``insert_watchdog_auto_resume_message``
# and the three ``_Episode`` fields that tracked a reservation all lost their
# only writer at once. The three fields were still READ by the join guard below,
# which made that branch permanently take its ``None``/``False`` path: a guard
# that cannot be false is not a guard, and leaving it would have read as one.
# FX181 D2 row 1: the TERMINAL statuses for the aggregate quiescence predicate,
# mapped to their message labels. Membership here IS the classification — a status
# absent from this map is indeterminate and can never contribute to a ring.
_QUIESCENT_STATUS_LABELS = {
    TerminalStatus.IDLE: "idle",
    TerminalStatus.COMPLETED: "completed",
    TerminalStatus.ERROR: "error",
}


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
    waiting_last_push_at: float | None = None
    # F228-b: processing-no-progress tracker
    processing_since: float | None = None  # monotonic time PROCESSING was accepted
    last_np_fp: str | None = None  # last fingerprint taken WHILE processing
    last_progress_at: float | None = None  # monotonic time of last FP change while processing
    np_fired_key: tuple[int, float] | None = None  # (generation, processing_since) dedup
    last_np_hint: str | None = None  # sanitized bounded last-line hint from filtered tail
    # F295 Half 2 D9: absolute-age wedge arm (grok_cli only)
    wedge_fired_key: tuple[int, float] | None = None  # (generation, processing_since) dedup
    wedge_flagged: bool = False  # whether wedge_suspect is currently set


@dataclass(frozen=True)
class WatchdogNotice:
    terminal_id: str
    caller_id: str
    message: str
    idle_reason: str | None
    source_generation: int = 0
    kind: str = "stall"


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
        # WP-ARCH 3c K4: the waiting-inbox, ready-backlog and dead-owed state
        # went with the ticks that were their only writers and readers.
        self._paused: set[str] = set()
        self._generation_by_terminal: dict[str, int] = {}
        self._callback_fences: dict[str, int] = {}
        self._chain_notified: set[tuple[str, int, str, int]] = set()
        self._parity_clock = clock

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
        # F487: never arm an episode for a warm-parked terminal. The
        # park_warm flag is persisted in the terminal's system metadata at
        # creation time; checking it here is the single-point-of-enforcement
        # guard that prevents false alarms regardless of how the dispatch
        # reached this point.
        meta = get_terminal_metadata(terminal_id)
        if meta is not None and isinstance(meta, dict):
            raw_metadata = meta.get("metadata")
            if isinstance(raw_metadata, dict):
                cao_ns = raw_metadata.get("cao")
                if isinstance(cao_ns, dict) and cao_ns.get("park_warm") is True:
                    return
        now = time.monotonic()
        wall_now = _utcnow()
        with self._lock:
            if terminal_id in self._paused:
                return
            episode = self._episodes.get(terminal_id)
            if episode is not None and not episode.callback_seen and not episode.fired:
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

    def emit_pre_delete_notice(self, terminal_id: str) -> WatchdogNotice | None:
        """Emit one durable notice if an open un-fired episode exists, then return it.

        Called by _delete_terminal_under_lease BEFORE clear_terminal, under the
        terminal's delivery_lock. Returns None when no notice is warranted.

        F310: Before firing, consult the durable inbox for a delivered/acked
        callback from this worker since the current episode's dispatch time.
        A follow-up send_message re-arms the episode (callback_seen=False on
        the new generation), but the worker's prior callback is authoritative —
        if the DB shows a delivered message, the result was NOT lost.
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
            episode_started = episode.episode_started_wall_at

        # F310: DB ground-truth check — outside _lock (may block on DB).
        # If a callback from this worker reached the caller since the episode
        # started, the result is safe regardless of in-memory episode state.
        try:
            status = get_callback_status_since(terminal_id, caller_id, episode_started)
            if status is not None:
                # Durable evidence: callback delivered/acked — no loss.
                return None
        except Exception:
            # DB unavailable: fall through to the conservative path (fire).
            logger.debug(
                "F310: get_callback_status_since failed for %s; " "falling through to loss warning",
                terminal_id,
                exc_info=True,
            )

        with self._lock:
            # Re-check under lock: episode may have been settled concurrently.
            episode = self._episodes.get(terminal_id)
            if episode is None or episode.callback_seen or episode.fired:
                return None
            if episode.generation != generation:
                # Episode was replaced — stale decision; abort.
                return None
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
            self._episodes.pop(terminal_id, None)
            self._generation_by_terminal.pop(terminal_id, None)
            self._callback_fences.pop(terminal_id, None)
            self._chain_notified = {
                key
                for key in self._chain_notified
                if key[0] != terminal_id and key[2] != terminal_id
            }

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
        # WP-ARCH 3c K7: the FX193 status feed into nudge discipline is gone with
        # the nudge. There is no scheduled pane nudge to coalesce or cancel.

        # FX194 D1: notify boundary pull service on consumption boundaries
        # (idle transition = a consumption boundary where pull can deliver)
        if status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
            try:
                # Look up the mailbox for this terminal
                from cli_agent_orchestrator.clients.database import MailboxModel, SessionLocal
                from cli_agent_orchestrator.services.boundary_pull_service import (
                    boundary_pull_service,
                )

                with SessionLocal() as db:
                    mailbox = (
                        db.query(MailboxModel).filter_by(current_terminal_id=terminal_id).first()
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

    # WP-ARCH 3c K7: ``_fx191_convergence_tick`` is gone. Its entire body was a
    # throttled call into ``delivery_service.convergence_tick``, and that function
    # is deleted with the obligation ladder — it had already returned immediately
    # whenever the queue owned delivery, so at the shipped position this tick did
    # nothing but read the clock. The scheduled observer is
    # ``app/delivery/tick.py``. Removing the method here is forced by K7 rather
    # than chosen: it is the deleted function's ONLY driver, so leaving it would
    # leave an ImportError behind a try/except that swallows it silently.

    def evict_vanished_episodes(self) -> list[str]:
        """Drop episodes whose terminal no longer exists. Returns the evicted ids.

        **This is a RESTORATION, not a new sweep.** Before WP-ARCH 3c K4 the
        eviction lived inside ``collect_due_notifications``: that pass resolved
        each candidate's metadata and, finding none, popped the episode (FX181 D3
        retired it into the owed set first). K4 deletes the notifier, and the
        eviction would have gone with it — leaving the episode map to grow
        without bound.

        The leak is not cosmetic. ``has_episode`` is read by
        ``inbox_service`` to decide whether a terminal is mid-episode, so a
        leaked entry answers True forever for a terminal that is gone. The two
        survivors that also remove episodes -- ``clear_terminal`` and
        ``_gc_fired_episodes`` -- are EVENT-driven: the first needs a delete to
        be observed, the second needs the callback to arrive. A terminal that
        vanishes without either event is exactly the case this covers, and it is
        the case the old notifier happened to handle on its way past.

        What is NOT restored is the owed-set retirement: ``_dead_owed`` existed
        to let the quiescence notice still name a dead member, and that notice is
        deleted. Eviction without it is the whole of the surviving behaviour.
        """
        with self._lock:
            candidates = list(self._episodes)
        vanished = [tid for tid in candidates if not terminal_exists(tid)]
        if not vanished:
            return []
        with self._lock:
            for terminal_id in vanished:
                self._episodes.pop(terminal_id, None)
        logger.info(
            "watchdog evicted %d episode(s) whose terminal is gone: %s",
            len(vanished),
            ",".join(vanished),
        )
        return vanished

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

        # F506 Fork A pick (i): the sampler widens from "terminals with an armed
        # episode" to ALL live terminals with a readable pane — the #361 incident
        # happened with no episode armed, so episode-scoped sampling could never
        # see it (AC17). The armed-episode set is still tracked separately for the
        # episode-clock / no-progress bookkeeping below.
        with self._lock:
            armed_episode_ids = {
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
                and (
                    episode.idle_since is not None
                    or episode.quiet_since is not None
                    or episode.processing_since is not None
                )
            }

        # Enumerate the widened set: all live terminals UNION armed episodes.
        # Failure to enumerate degrades to episode-only (never worse than pre-F506).
        try:
            from cli_agent_orchestrator.clients.database import list_all_terminals

            live_ids = [row["id"] for row in list_all_terminals()]
        except Exception:
            logger.debug("pane sampler: live-terminal enumeration failed", exc_info=True)
            live_ids = []
        sample_ids = list(dict.fromkeys([*live_ids, *armed_episode_ids]))
        if not sample_ids:
            return

        # AC1 / D1: the SINGLE sampler is now pane_liveness.observe — this method
        # performs no pane capture of its own. observe() owns the one
        # capture -> filter -> sha256 pipeline (net sampler count stays 1).
        from cli_agent_orchestrator.services.pane_liveness import pane_liveness
        from cli_agent_orchestrator.services.status_monitor import status_monitor
        from cli_agent_orchestrator.utils.herdr_runtime_gate import (
            herdr_lifecycle_authoritative,
        )

        for terminal_id in sample_ids:
            # WP-HERDR H1 §8 / F506 Do-NOT #1: a CERTIFIED terminal's lifecycle
            # comes from the herdr EventSource, and ``observe`` is the tree's one
            # pane sampler. Registering a certified terminal here would make seam
            # A a SECOND sampler for the same fact — the exact thing F506 was
            # built to end (net sampler count stays 1). So the certified cohort
            # is skipped: no capture, no fingerprint, no resync from the pane
            # tail. Everything for an uncertified terminal is unchanged.
            if herdr_lifecycle_authoritative(terminal_id):
                continue
            observation = pane_liveness.observe(terminal_id, now=now, monitor=status_monitor)
            if observation is None:
                # No usable sample this tick (capture outage / unreadable pane).
                # Nothing to reconcile — matches the pre-F506 `continue`.
                continue

            # D15: re-derive from the independent pane sample after a signalled
            # stream drop, plus the low-frequency PROCESSING/ERROR backstop
            # (F794 #651 added ERROR). This adds no capture: peek() returns the
            # tail observe() already retained.
            retained = pane_liveness.peek(terminal_id, now=now)
            if retained is not None:
                status_monitor.resync_from_pane_tail(terminal_id, retained.filtered_tail, now=now)

            # F507: reconcile the question marker for terminals holding an open
            # marker or classified WAITING (level-triggered, D9). Cheap and
            # sampler-independent.
            self._reconcile_question_marker(terminal_id)

            if terminal_id not in armed_episode_ids:
                continue

            fingerprint = observation.fingerprint
            fp_changed = observation.fp_changed
            with self._lock:
                episode = self._episodes.get(terminal_id)
                if (
                    episode is None
                    or episode.callback_seen
                    or episode.fired
                    or (
                        episode.idle_since is None
                        and episode.quiet_since is None
                        and episode.processing_since is None
                    )
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
                    # Sanitized hint from the SAME filtered tail (no extra I/O).
                    hint_lines = [
                        ln.strip() for ln in observation.filtered_tail.splitlines() if ln.strip()
                    ]
                    raw_hint = hint_lines[-1] if hint_lines else ""
                    # Sanitize: terminal text is untrusted
                    sanitized_hint = (
                        raw_hint.replace('"', "'").replace("\n", " ").replace("\r", " ")
                    )
                    sanitized_hint = "".join(c if c.isprintable() else "?" for c in sanitized_hint)
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
            del fp_changed  # bookkeeping uses last_screen_fp deltas directly

    def _reconcile_question_marker(self, terminal_id: str) -> None:
        """F507 level-triggered reconcile: run only when there is something to do.

        Reconciles for terminals holding an open marker OR classified WAITING —
        walks the transcript, heals a lost clear (AC10), and applies the TTL
        (AC14). Best-effort; never raises into the sampler loop.
        """
        try:
            from cli_agent_orchestrator.services.question_state import question_state
            from cli_agent_orchestrator.services.status_monitor import status_monitor

            interesting = question_state.is_open(terminal_id)
            if not interesting:
                published = status_monitor.get_published_status(terminal_id)
                interesting = published is TerminalStatus.WAITING_USER_ANSWER
            if not interesting:
                return
            metadata = get_terminal_metadata(terminal_id)
            if metadata is None:
                return
            question_state.reconcile(terminal_id, metadata)
        except Exception:
            logger.debug("question_marker reconcile failed for %s", terminal_id, exc_info=True)

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

    async def run(self, registry: PluginRegistry | None = None) -> None:
        from cli_agent_orchestrator.services import seam_parity

        queue = bus.subscribe("terminal.*.status")
        logger.info("StalledCallbackWatchdog started")
        interval = max(1.0, min(5.0, float(self.grace_seconds)))
        # F351: self-tuning idle backoff — stretch the tick interval when no
        # episodes are active, reset on any status event.
        _idle_consecutive = 0
        _IDLE_BACKOFF_MAX_S = 5.0  # Cap: never exceed the normal tick cadence
        next_parity_sweep = self._parity_clock() + 60.0
        while True:
            try:
                # F351: use stretched interval when idle
                effective_interval = (
                    min(interval + (_idle_consecutive * 0.5), _IDLE_BACKOFF_MAX_S)
                    if _idle_consecutive > 0
                    else interval
                )
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=effective_interval)
                except asyncio.TimeoutError:
                    event = None
                if event is not None:
                    terminal_id = terminal_id_from_topic(event["topic"])
                    self.record_status(
                        terminal_id,
                        TerminalStatus(event["data"]["status"]),
                    )
                    _idle_consecutive = 0  # F351: reset backoff on event
                else:
                    # F351: no event — check if episodes exist before running the
                    # episode-driven heavy ticks. F506 Fork A relaxes this: the
                    # pane sampler MUST still run with no episode armed (the #361
                    # false-idle happened with no episode), so refresh_screen_
                    # fingerprints runs unconditionally below. The idle-backoff
                    # interval stretch is retained (the F351 optimisation), but the
                    # early `continue` no longer skips the sampler — F351's
                    # idle-backoff tests are re-derived accordingly (§9).
                    with self._lock:
                        _has_episodes = bool(self._episodes)
                    if not _has_episodes:
                        _idle_consecutive = min(_idle_consecutive + 1, 10)
                    else:
                        _idle_consecutive = 0

                # WP-ARCH 3c K4: the idle FORK is gone with the ticks that made it
                # worth having. It existed to skip five heavy episode ticks while
                # no episode was armed, keeping only the liveness sample and the
                # wedge probe. All five are deleted, so both branches did the same
                # two things and the fork was a fork over nothing. What is left is
                # the liveness half of this watchdog, which has no idle case: an
                # unarmed terminal still has a pane to sample.
                await asyncio.to_thread(self.evict_vanished_episodes)
                await asyncio.to_thread(self.poll_unarmed_statuses)
                await asyncio.to_thread(self.refresh_screen_fingerprints)
                parity_now = self._parity_clock()
                if parity_now >= next_parity_sweep:
                    next_parity_sweep = parity_now + 60.0
                    await asyncio.to_thread(seam_parity.sweep)
            except Exception:
                logger.exception("StalledCallbackWatchdog error")


stalled_callback_watchdog = StalledCallbackWatchdog()
