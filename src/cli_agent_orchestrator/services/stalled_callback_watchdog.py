"""Caller-only watchdog for silent assigned workers."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from cli_agent_orchestrator.clients.database import (
    _utcnow,
    get_callback_status_since,
    get_terminal_metadata,
    insert_barrier_escalation_message,
    terminal_exists,
)
from cli_agent_orchestrator.constants import STALLED_CALLBACK_GRACE_SECONDS
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import receiver_state_view
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)


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
    generation: int = 1
    revision: int = 0


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
        episode, _started = snapshot
        with self._lock:
            if episode is not None:
                self._episodes[terminal_id] = episode
            self._paused.discard(terminal_id)

    def repair_terminal_after_resume_failure(self, terminal_id: str, snapshot) -> None:
        """Best-effort, non-raising P14 repair used before releasing quarantine locks."""
        try:
            episode, _started = snapshot
        except Exception:
            episode = None
        with self._lock:
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
        """Apply the two surviving status-event effects.

        WP-ARCH 3c removed every time-driven episode evaluator, so status,
        quiet, fingerprint, no-progress and wedge clocks no longer belong on
        an episode. Status events still collect a settled fired episode and
        trigger the boundary-pull seam on a consumption boundary.
        """
        del now
        with self._lock:
            if terminal_id in self._paused:
                return
            if terminal_id not in self._episodes:
                return
            self._gc_fired_episodes()

        if status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
            try:
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
        """Sample live panes for resync and question-marker reconciliation.

        The former episode-clock consumers were deleted with their evaluators.
        This surviving low-frequency sampler has exactly two effects: re-drive
        status from the retained pane tail and reconcile a durable question
        marker. It keeps the historical method name because the run loop and
        simulation driver call that public seam.
        """
        now = time.monotonic() if now is None else now
        try:
            from cli_agent_orchestrator.clients.database import list_all_terminals

            sample_ids = [row["id"] for row in list_all_terminals()]
        except Exception:
            logger.debug("pane sampler: live-terminal enumeration failed", exc_info=True)
            sample_ids = []
        if not sample_ids:
            return

        from cli_agent_orchestrator.services.pane_liveness import pane_liveness
        from cli_agent_orchestrator.services.status_monitor import status_monitor
        from cli_agent_orchestrator.utils.herdr_runtime_gate import (
            herdr_lifecycle_authoritative,
        )

        for terminal_id in sample_ids:
            if herdr_lifecycle_authoritative(terminal_id):
                continue
            observation = pane_liveness.observe(terminal_id, now=now, monitor=status_monitor)
            if observation is None:
                continue
            retained = pane_liveness.peek(terminal_id, now=now)
            if retained is not None:
                status_monitor.resync_from_pane_tail(terminal_id, retained.filtered_tail, now=now)
            self._reconcile_question_marker(terminal_id)

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
