"""Fleet liveness probe — heartbeats as columns, edges as rows (AC4b (ii)).

One ``tmux list-panes -a -F ...`` per tick for the WHOLE fleet, every
``PANE_HEARTBEAT_S``.  What it learns lands in the projection's liveness COLUMNS
(``last_probe_at``, ``pane_pid``, ``pane_present``, ``miss_count``); only edges
become event rows.  r9 retired the periodic ``pane.alive`` kind for exactly this
reason — a row per terminal per tick would dominate the log, and "the probe
appends a row per tick" is a phase-1 mutant the heartbeat-columns test kills.

The distinction B13 insists on, and the one that is easiest to get wrong: **a
failed probe is not pane absence.**  If tmux cannot be reached, or answers with
nothing at all, that says something about the PROBE, not about any worker.
Treating it as absence would tear down a healthy fleet the first time tmux
hiccups.  So:

* a failed (or empty) probe appends ONE ``probe.failed`` row against
  ``core.events.FLEET_TERMINAL_ID`` and touches no terminal — a statement about
  the probe, attributed to no worker, because attributing it to a real terminal
  would be a lie ``cao diag <terminal_id>`` would then repeat;
* ``PROBE_FAIL_TICKS`` consecutive failures open a fleet-wide
  ``degraded(producer_error)`` episode — one ``pane.missing`` per terminal
  carrying that reason, once per episode, never once per tick;
* the next successful probe closes the episode, appending ``pane.recovered`` for
  every terminal it lists, which is how B16 restores ``prior_state``.

On a SUCCESSFUL probe a terminal is judged against the sessions tmux actually
listed.  A terminal whose tmux session is absent from the listing is
``degraded(pane_unreadable)`` and never counts toward an exit: the session might
be detached, renamed or momentarily unlisted, and an exit is unrecoverable in the
projection.  Only a terminal whose SESSION is listed while its own pane is not
accrues a miss, and only after ``PANE_MISS_TICKS`` of those does this producer
append ``process.exited`` — of which it is the sole owner, in phase 1 and after.

The exit ``reason`` is read out of our own log rather than from a service:
``teardown`` iff a ``teardown.intended`` decision row (hook 7) is still within its
TTL for that terminal, else ``crash``.  #571 is the case that demands it — a
healthy teardown that rendered as ERROR, reconstructable afterwards only from
pane archaeology.

Nothing here imports the fork's legacy tree.  tmux, the terminal roster and the
clock all arrive as injected callables from ``bootstrap.py``, which keeps the
``new-code-never-imports-legacy`` contract true and makes every branch above
reachable from a test without a tmux server.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Protocol

from cli_agent_orchestrator.adapters.truth.wiring import ProducerRuntime, emit, producer_runtime
from cli_agent_orchestrator.core.events import (
    FLEET_TERMINAL_ID,
    Confidence,
    DecisionKind,
    EventDraft,
    EventKind,
    Producer,
)
from cli_agent_orchestrator.core.states import DegradedReason
from cli_agent_orchestrator.core.timing import (
    PANE_HEARTBEAT_S,
    PANE_MISS_TICKS,
    PANE_SAMPLE_S,
    PROBE_FAIL_TICKS,
)

__all__ = [
    "LivenessProbe",
    "PaneRecord",
    "TerminalRef",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaneRecord:
    """One row of the ``tmux list-panes -a`` output, already parsed."""

    session: str
    window: str
    pid: int | None = None


class TerminalRef(Protocol):
    """The three fields the probe needs about a fleet member."""

    @property
    def terminal_id(self) -> str: ...

    @property
    def tmux_session(self) -> str: ...

    @property
    def tmux_window(self) -> str: ...


@dataclass
class _Track:
    """Per-terminal probe memory.

    ``confirmed_present`` starts as ``None`` — "never yet judged".  That third
    value matters: ``pane.recovered`` fires when a pane that was confirmed ABSENT
    is listed again, and a terminal seen for the first time has not recovered
    from anything.  Emitting a recovery row at startup would fill the log with
    rows describing an event that did not happen.
    """

    confirmed_present: bool | None = None
    miss_count: int = 0
    exited: bool = False
    degraded_reason: str | None = None


@dataclass
class _ProbeState:
    consecutive_failures: int = 0
    producer_error_open: bool = False
    tracks: dict[str, _Track] = field(default_factory=dict)


class LivenessProbe:
    """The AC4b (ii) producer.  One tick is :meth:`probe_once`.

    ``list_panes`` returns the whole fleet's panes, or raises, or returns an
    empty iterable — the latter two are both "the probe failed".  ``fleet``
    returns the terminals to judge.  ``teardown_lookup`` is optional and exists
    only for tests that want to bypass the event-log read; production leaves it
    ``None`` and the exit reason comes from the ``teardown.intended`` rows.

    ``list_panes`` draws a distinction B13 draws one level down, and it has
    THREE answers, not two.  Records are a listing.  A raise, or an empty
    iterable, is a FAILED probe — a statement that the probe ran and learned
    nothing, and ``PROBE_FAIL_TICKS`` of those degrade the entire fleet with
    ``producer_error``.  ``None`` is neither: it means there is no listing to be
    had, which is the answer on a backend that cannot enumerate the fleet's panes
    at all (herdr inherits ``enumerate_windows``'s fail-closed default).  Reading
    that as a failure would degrade every terminal forever on the strength of a
    feature nobody implemented.  The whole callable is optional too, for a probe
    built with no listing at all; either way it runs as the sampler's driver:
    less information, never wrong information.

    The callable is handed the ROSTER the tick already read, and so is
    ``sampler_tick``.  One tick, one roster query: it is a full table read
    through the ORM, and a tick that asked four callers for it separately paid
    four times for one answer.

    ``sampler_tick`` is WP-ARCH phase 2's §12 seam, and it is here because phase 2
    is the phase that notices a cross-phase defect neither phase owns.  The
    pane-delta sampler that ``fuse_status``'s rules 3a/3b read is driven from
    exactly one place, the stalled-callback watchdog's tick — and that module is
    phase 3's D6 K4, deleted in 3c.  After that deletion the rules would read a
    sample nothing refreshes, which by the sampler's own no-evidence rule degrades
    to ``None`` and silently disables the pane-delta downgrade for every UNSOURCED
    terminal: the ones I7 promises are unaffected, and with no acceptance
    criterion in phase 3 to catch it.  This probe already owns the fleet's
    periodic tmux work on the same ``PANE_HEARTBEAT_S`` cadence, so it is where
    the drive belongs.

    The probe supplies the TICK, not the capture.  The F506 single-sampler ban
    stands: the callable handed in is the sampler's own entry point, and nothing
    here reads a pane.  A ``sampler_tick`` that raises cannot reach the probe's
    own work — a re-drive that could break the liveness probe would be a strictly
    worse trade than the regression it prevents.
    """

    def __init__(
        self,
        *,
        list_panes: Callable[[Sequence[TerminalRef]], Iterable[PaneRecord] | None] | None = None,
        fleet: Callable[[], Iterable[TerminalRef]],
        teardown_lookup: Callable[[str], bool] | None = None,
        sampler_tick: Callable[[Sequence[TerminalRef]], None] | None = None,
        sleeper: Callable[[float], Awaitable[bool]] | None = None,
    ) -> None:
        self._list_panes = list_panes
        self._fleet = fleet
        self._teardown_lookup = teardown_lookup
        self._sampler_tick = sampler_tick
        self._sleeper = sleeper
        self._state = _ProbeState()
        self._task: asyncio.Task[None] | None = None
        self._stopping: asyncio.Event | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def name(self) -> str:
        return "liveness_probe"

    @property
    def is_authoritative(self) -> bool:
        """False.  The probe observes the process, not the agent's turn.

        It owns ``process.exited`` outright, but that ownership is about who may
        WRITE the kind, not about precedence: a pane listing cannot tell a busy
        worker from an idle one, so it must never outrank a rollout.
        """
        return False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="cao-liveness-probe")

    async def stop(self) -> None:
        """Ask the loop to finish and WAIT for the tick that is in flight.

        Not a cancel.  A task parked on ``asyncio.to_thread`` raises
        ``CancelledError`` in the coroutine while the worker thread runs on to
        completion, so cancelling would return here with a real ``capture-pane``
        still in progress against a server that believes it has shut down.  The
        stop flag ends the sleep instead, the tick finishes, and the loop exits.
        """
        stopping = self._stopping
        if stopping is not None:
            stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, or wake early to stop.  True when the loop should exit."""
        if self._sleeper is not None:
            return await self._sleeper(seconds)
        stopping = self._stopping
        if stopping is None:  # pragma: no cover - start() always sets it
            await asyncio.sleep(seconds)
            return False
        try:
            await asyncio.wait_for(stopping.wait(), timeout=seconds)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return True

    async def _run(self) -> None:
        # Two cadences, one task.  The pane listing is a HEARTBEAT and the
        # pane-delta sample is a SAMPLE: the first answers "is the pane there",
        # which changes on the scale of a terminal's life, and the second feeds
        # rules that call their own evidence stale after
        # ``PANE_LIVENESS_STALENESS_S``.  Running the sample at the heartbeat
        # would leave those rules blind for half of every window; running the
        # listing at the sample rate would quadruple the fleet's tmux work for
        # nothing.  ``check_orderings`` keeps the heartbeat a whole multiple of
        # the sample so this counter never drifts.
        every = max(1, int(PANE_HEARTBEAT_S // PANE_SAMPLE_S))
        tick = 0
        while True:
            try:
                # OFF the event loop.  One tick shells out to the backend for the
                # pane listing and then drives the pane-delta sampler across the
                # whole fleet, both of which are blocking subprocess work: run
                # inline, a slow tmux or herdr call would stall every request the
                # server is serving.  ``RetentionTask`` offloads its sweep for the
                # same reason, and the producers are already called from the
                # legacy monitor's own threads, so nothing here is loop-affine.
                if tick % every == 0:
                    await asyncio.to_thread(self.probe_once)
                else:
                    await asyncio.to_thread(self.sample_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - the never-break-the-server rule
                logger.debug("liveness probe tick failed", exc_info=True)
            tick += 1
            if await self._sleep(PANE_SAMPLE_S):
                return

    # -- one tick ------------------------------------------------------------

    def sample_once(self) -> None:
        """Drive the pane-delta sampler and nothing else.  Never raises.

        The between-heartbeats tick.  It reads the roster because the sampler
        needs one, and that is the whole of its work.
        """
        if producer_runtime() is None:
            return
        self._drive_sampler(self._safe_fleet())

    def probe_once(self) -> None:
        """Run one probe: update columns, append edges.  Never raises.

        Reads the roster ONCE and threads it through the sampler drive and the
        pane listing.  The roster is a full table read through the ORM, so a tick
        that asked for it four times paid four times for one answer.
        """
        runtime = producer_runtime()
        if runtime is None:
            return
        fleet = self._safe_fleet()
        self._drive_sampler(fleet)
        if self._list_panes is None:
            # No pane listing on this backend — see the class docstring.  The
            # sampler drive above still ran, which is the half of the tick that
            # has nothing to do with tmux.
            return
        if not fleet:
            # An empty fleet is not a failed probe either.  A server with no
            # terminals would otherwise file one ``probe.failed`` row every tick
            # forever and then declare a fleet-wide ``producer_error`` episode
            # over a fleet of nobody.
            return
        try:
            panes: list[PaneRecord] | None
            try:
                listed = self._list_panes(fleet)
                if listed is None:
                    # A THIRD answer, distinct from both failure shapes below:
                    # "there is no listing to be had on this backend right now".
                    # A backend that cannot enumerate has not failed a probe, and
                    # reading it as one would degrade the fleet for
                    # ``producer_error`` on the strength of a missing feature.
                    return
                panes = list(listed)
            except Exception:
                panes = None
            if not panes:
                # None (the call failed) and [] (tmux answered with nothing) are
                # the SAME verdict: this probe learned nothing.  B13 forbids
                # reading either as absence.
                self._on_failed_probe(runtime, fleet)
                return
            self._on_successful_probe(runtime, panes, fleet)
        except Exception:  # pragma: no cover - the never-break-the-server rule
            logger.debug("liveness probe failed", exc_info=True)

    def _drive_sampler(self, fleet: Sequence[TerminalRef]) -> None:
        """WP-ARCH phase 2 §12 — one pane-delta sample per tick.  Never raises.

        Runs BEFORE the probe's own work rather than after, so a probe that fails
        and returns early still refreshes the sample: the sampler's freshness is
        about the pane, and a tmux listing that could not be read says nothing
        about whether an individual pane changed.
        """
        tick = self._sampler_tick
        if tick is None:
            return
        try:
            tick(fleet)
        except Exception:  # pragma: no cover - the never-break-the-probe rule
            logger.debug("pane-delta sampler tick failed", exc_info=True)

    # -- failure path --------------------------------------------------------

    def _on_failed_probe(self, runtime: ProducerRuntime, fleet: Sequence[TerminalRef]) -> None:
        state = self._state
        state.consecutive_failures += 1
        now = runtime.clock.now()
        emit(
            EventDraft(
                terminal_id=FLEET_TERMINAL_ID,
                kind=DecisionKind.PROBE_FAILED,
                producer=Producer.SERVER,
                confidence=Confidence.DERIVED,
                observed_at=now,
                decision=DecisionKind.PROBE_FAILED,
                payload={"consecutive_failures": state.consecutive_failures},
            )
        )
        if state.consecutive_failures < PROBE_FAIL_TICKS or state.producer_error_open:
            return
        # Open the fleet-wide episode exactly once: one row per terminal naming
        # producer_error, not one row per terminal per tick.
        state.producer_error_open = True
        for ref in fleet:
            track = state.tracks.setdefault(ref.terminal_id, _Track())
            track.confirmed_present = False
            track.degraded_reason = DegradedReason.PRODUCER_ERROR.value
            self._emit_missing(ref.terminal_id, DegradedReason.PRODUCER_ERROR, now)

    # -- success path --------------------------------------------------------

    def _on_successful_probe(
        self, runtime: ProducerRuntime, panes: list[PaneRecord], fleet: Sequence[TerminalRef]
    ) -> None:
        state = self._state
        now = runtime.clock.now()
        state.consecutive_failures = 0
        episode_closing = state.producer_error_open
        state.producer_error_open = False

        sessions = {pane.session for pane in panes}
        by_key = {(pane.session, pane.window): pane for pane in panes}

        for ref in fleet:
            track = state.tracks.setdefault(ref.terminal_id, _Track())
            pane = by_key.get((ref.tmux_session, ref.tmux_window))

            if pane is not None:
                self._on_pane_present(runtime, ref, track, pane, now, episode_closing)
                continue

            if ref.tmux_session not in sessions:
                # The whole session is unlisted.  Unreadable, never exited: an
                # exit is a one-way door in the projection and this evidence does
                # not justify walking through it.
                self._on_pane_unreadable(runtime, ref, track, now)
                continue

            self._on_pane_absent(runtime, ref, track, now)

    def _on_pane_present(
        self,
        runtime: ProducerRuntime,
        ref: TerminalRef,
        track: _Track,
        pane: PaneRecord,
        now: datetime,
        episode_closing: bool,
    ) -> None:
        recovered = track.confirmed_present is False
        track.confirmed_present = True
        track.miss_count = 0
        track.exited = False
        previous_reason = track.degraded_reason
        track.degraded_reason = None
        self._touch(runtime, ref.terminal_id, now, present=True, pid=pane.pid, miss_count=0)
        if recovered:
            emit(
                EventDraft(
                    terminal_id=ref.terminal_id,
                    kind=EventKind.PANE_RECOVERED,
                    producer=Producer.PANE,
                    confidence=Confidence.DERIVED,
                    observed_at=now,
                    payload={
                        "pane_pid": pane.pid,
                        "recovered_from": previous_reason,
                        "closed_producer_error_episode": episode_closing,
                    },
                )
            )

    def _on_pane_unreadable(
        self, runtime: ProducerRuntime, ref: TerminalRef, track: _Track, now: datetime
    ) -> None:
        self._touch(
            runtime,
            ref.terminal_id,
            now,
            present=False,
            pid=None,
            miss_count=track.miss_count,
        )
        if track.confirmed_present is False:
            return
        track.confirmed_present = False
        track.degraded_reason = DegradedReason.PANE_UNREADABLE.value
        self._emit_missing(ref.terminal_id, DegradedReason.PANE_UNREADABLE, now)

    def _on_pane_absent(
        self, runtime: ProducerRuntime, ref: TerminalRef, track: _Track, now: datetime
    ) -> None:
        first_miss = track.confirmed_present is not False
        track.miss_count += 1
        track.confirmed_present = False
        self._touch(
            runtime,
            ref.terminal_id,
            now,
            present=False,
            pid=None,
            miss_count=track.miss_count,
        )
        if first_miss:
            track.degraded_reason = DegradedReason.PANE_UNREADABLE.value
            self._emit_missing(ref.terminal_id, DegradedReason.PANE_UNREADABLE, now)
        if track.miss_count < PANE_MISS_TICKS or track.exited:
            return
        track.exited = True
        reason = "teardown" if self._teardown_is_live(ref.terminal_id, now) else "crash"
        emit(
            EventDraft(
                terminal_id=ref.terminal_id,
                kind=EventKind.PROCESS_EXITED,
                producer=Producer.PANE,
                confidence=Confidence.DERIVED,
                observed_at=now,
                payload={"reason": reason, "miss_count": track.miss_count},
            )
        )

    # -- helpers -------------------------------------------------------------

    def _emit_missing(self, terminal_id: str, reason: DegradedReason, now: datetime) -> None:
        emit(
            EventDraft(
                terminal_id=terminal_id,
                kind=EventKind.PANE_MISSING,
                producer=Producer.PANE,
                confidence=Confidence.DERIVED,
                observed_at=now,
                payload={"reason": reason.value},
            )
        )

    def _touch(
        self,
        runtime: ProducerRuntime,
        terminal_id: str,
        now: datetime,
        *,
        present: bool,
        pid: int | None,
        miss_count: int,
    ) -> None:
        """Write the liveness COLUMNS.  Silently skipped when no StateStore is wired."""
        state_store = runtime.state_store
        if state_store is None:
            return
        try:
            state_store.touch_probe(
                terminal_id,
                probed_at=now,
                pane_present=present,
                pane_pid=pid,
                miss_count=miss_count,
            )
        except Exception:
            logger.debug("touch_probe failed for %s", terminal_id, exc_info=True)

    def _safe_fleet(self) -> list[TerminalRef]:
        try:
            return list(self._fleet())
        except Exception:
            logger.debug("fleet roster unavailable to the liveness probe", exc_info=True)
            return []

    def _teardown_is_live(self, terminal_id: str, now: datetime) -> bool:
        """True when a ``teardown.intended`` row for this terminal is still in TTL.

        Read out of our OWN log rather than from ``teardown_intent_service``:
        adapters may not import the legacy tree, and more importantly the log is
        the thing ``cao diag`` will replay.  If the reason a process exited cannot
        be re-derived from the stored rows, the diagnosability decision (U9) has
        not been met — asking a service at probe time would give the right answer
        now and no answer at all in six weeks.
        """
        if self._teardown_lookup is not None:
            try:
                return bool(self._teardown_lookup(terminal_id))
            except Exception:
                return False
        runtime = producer_runtime()
        if runtime is None:
            return False
        try:
            rows = runtime.store.read(
                terminal_id, kinds=frozenset({DecisionKind.TEARDOWN_INTENDED})
            )
        except Exception:
            logger.debug("teardown intent lookup failed for %s", terminal_id, exc_info=True)
            return False
        if not rows:
            return False
        latest = rows[-1]
        ttl_s = latest.payload.get("ttl_s")
        if not isinstance(ttl_s, (int, float)):
            # An intent with no usable TTL is treated as live.  Mislabelling a
            # teardown as a crash raises a false alarm; the other way round hides
            # a real one.
            return True
        try:
            age = (now - latest.observed_at).total_seconds()
        except Exception:
            return True
        return age <= float(ttl_s)
