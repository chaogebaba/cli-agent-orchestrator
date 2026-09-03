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
import threading
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
from cli_agent_orchestrator.core.timing import PANE_HEARTBEAT_S, PANE_MISS_TICKS, PROBE_FAIL_TICKS

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
    from anything.  Emitting a recovery row at startup would inflate the AC10
    content floor with rows describing an event that did not happen.
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
    """

    def __init__(
        self,
        *,
        list_panes: Callable[[], Iterable[PaneRecord]],
        fleet: Callable[[], Iterable[TerminalRef]],
        teardown_lookup: Callable[[str], bool] | None = None,
    ) -> None:
        self._list_panes = list_panes
        self._fleet = fleet
        self._teardown_lookup = teardown_lookup
        self._state = _ProbeState()
        self._task: asyncio.Task[None] | None = None
        self._stopping = threading.Event()

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
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="cao-liveness-probe")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.probe_once()
            except Exception:  # pragma: no cover - the never-break-the-server rule
                logger.debug("liveness probe tick failed", exc_info=True)
            await asyncio.sleep(PANE_HEARTBEAT_S)

    # -- one tick ------------------------------------------------------------

    def probe_once(self) -> None:
        """Run one probe: update columns, append edges.  Never raises."""
        runtime = producer_runtime()
        if runtime is None:
            return
        try:
            panes: list[PaneRecord] | None
            try:
                panes = list(self._list_panes())
            except Exception:
                panes = None
            if not panes:
                # None (the call failed) and [] (tmux answered with nothing) are
                # the SAME verdict: this probe learned nothing.  B13 forbids
                # reading either as absence.
                self._on_failed_probe(runtime)
                return
            self._on_successful_probe(runtime, panes)
        except Exception:  # pragma: no cover - the never-break-the-server rule
            logger.debug("liveness probe failed", exc_info=True)

    # -- failure path --------------------------------------------------------

    def _on_failed_probe(self, runtime: ProducerRuntime) -> None:
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
        for ref in self._safe_fleet():
            track = state.tracks.setdefault(ref.terminal_id, _Track())
            track.confirmed_present = False
            track.degraded_reason = DegradedReason.PRODUCER_ERROR.value
            self._emit_missing(ref.terminal_id, DegradedReason.PRODUCER_ERROR, now)

    # -- success path --------------------------------------------------------

    def _on_successful_probe(self, runtime: ProducerRuntime, panes: list[PaneRecord]) -> None:
        state = self._state
        now = runtime.clock.now()
        state.consecutive_failures = 0
        episode_closing = state.producer_error_open
        state.producer_error_open = False

        sessions = {pane.session for pane in panes}
        by_key = {(pane.session, pane.window): pane for pane in panes}

        for ref in self._safe_fleet():
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
