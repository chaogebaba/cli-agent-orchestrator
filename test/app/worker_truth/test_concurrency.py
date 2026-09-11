"""One writer at a time (WP-ARCH phase 2, sub-phase 2b — the B1 blocker).

Sub-phase 2b gave the projector a second entry path on a second thread: the
sweep, on an executor thread, every ``PANE_HEARTBEAT_S``, while folds keep
arriving on whatever thread called ``emit``.  Both are read-modify-write over a
row the store replaces WHOLE — ``upsert`` is an unconditional
``INSERT … ON CONFLICT DO UPDATE`` of every column with no version guard — and
both hold their row across an event-log read.

The interleaving that matters is not exotic.  It is the sweep reading the
evidence for a silent terminal at the exact moment that terminal stops being
silent, which is to say: the event that races the sweep is precisely the event
the sweep is about.

Both arms are here on purpose.  A test that only asserts the fixed behaviour
cannot tell a working lock from a race that happened not to fire, so the second
test replaces the lock with a no-op and asserts the corruption appears — which is
what makes the first one evidence.
"""

from __future__ import annotations

import threading
from contextlib import nullcontext
from test.app.conftest import Rig

from cli_agent_orchestrator.core.events import AnyKind, EventKind, WorkerEvent
from cli_agent_orchestrator.core.states import WorkerState
from cli_agent_orchestrator.core.timing import NO_SIGNAL_S

TERMINAL = "term-race"

#: How long the sweep waits inside its evidence read for the fold to land.
#:
#: The locked arm always spends it (the fold cannot proceed, which is the point)
#: and the unlocked arm never does, so it bounds the test rather than timing it.
_HANDOFF_TIMEOUT_S = 2.0


class _GatedEvents:
    """The rig's event store, with one read that waits for the other thread.

    Wraps rather than subclasses so the delegation is total: every method the
    projector or the checks reach for goes straight through, and only the sweep's
    own evidence read is gated.
    """

    def __init__(self, inner: object, fold_done: threading.Event) -> None:
        self._inner = inner
        self._fold_done = fold_done
        self.armed = False
        self.gated = threading.Event()

    def read(self, *args: object, **kwargs: object) -> list[WorkerEvent]:
        if self.armed and "since_seq" in kwargs:
            # The sweep is holding its row and about to decide on it.  Let the
            # other thread try to fold, and wait to see whether it can.
            self.armed = False
            self.gated.set()
            self._fold_done.wait(timeout=_HANDOFF_TIMEOUT_S)
        return self._inner.read(*args, **kwargs)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _race(rig: Rig, *, locked: bool) -> tuple[WorkerState | None, int, int]:
    """Run one sweep against one concurrent fold.

    Returns the final state, the final ``last_event_seq``, and the seq of the
    ``turn.ended`` the other thread folded — the three numbers the invariant is
    about.
    """
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    assert rig.state_of(TERMINAL) is WorkerState.BUSY
    rig.clock.advance(NO_SIGNAL_S + 1)

    fold_done = threading.Event()
    gated = _GatedEvents(rig.events, fold_done)
    rig.projector._events = gated  # type: ignore[attr-defined]
    if not locked:
        # The mutant: the critical section without the critical part.
        rig.projector._lock = nullcontext()  # type: ignore[assignment]

    folded: list[int] = []

    def fold() -> None:
        gated.gated.wait(timeout=_HANDOFF_TIMEOUT_S)
        try:
            folded.append(rig.emit(TERMINAL, EventKind.TURN_ENDED).seq)
        finally:
            fold_done.set()

    worker = threading.Thread(target=fold, name="fold")
    worker.start()
    gated.armed = True
    rig.projector.sweep()
    worker.join(timeout=_HANDOFF_TIMEOUT_S * 3)

    row = rig.states.get(TERMINAL)
    return (
        (None if row is None else row.state),
        (0 if row is None else row.last_event_seq),
        folded[0],
    )


def test_a_fold_that_lands_during_a_sweep_is_not_overwritten(rig: Rig) -> None:
    """The arriving IDLE survives, and the cursor never goes backwards.

    With the lock the two writers are ordered rather than merged: the sweep
    degrades a terminal that was, at that instant, genuinely silent, and the fold
    then applies the ``turn.ended`` that ended the silence.  The final row is the
    newest truth and its ``last_event_seq`` is the newest event.
    """
    state, last_seq, folded_seq = _race(rig, locked=True)

    assert state is WorkerState.IDLE
    assert last_seq == folded_seq


def test_without_the_lock_the_sweep_clobbers_the_fold(rig: Rig) -> None:
    """The failure the lock exists to prevent, reproduced.

    The sweep writes its stale snapshot over the transition that landed while it
    was reading: the IDLE is lost, ``last_event_seq`` goes BACKWARDS to the event
    before it, and the log keeps a ``busy -> degraded`` transition describing a
    move the worker never made — which the ghost- and pane-disagreement checks
    then report as real.
    """
    state, last_seq, folded_seq = _race(rig, locked=False)

    assert state is WorkerState.DEGRADED
    assert last_seq < folded_seq


def test_the_sweep_re_reads_every_row_inside_the_lock(rig: Rig) -> None:
    """``all_terminals`` supplies the roster, never the decision input.

    The loop does an event-log read per silent terminal, so by the time it
    reaches the last one its snapshot of that row can be seconds old.  Acting on
    the snapshot is how the sweep would clobber a transition even with a lock
    held — the lock has to cover the READ as well as the write.
    """
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.clock.advance(NO_SIGNAL_S + 1)
    reads: list[str] = []
    inner_get = rig.states.get

    def counting_get(terminal_id: str) -> object:
        reads.append(terminal_id)
        return inner_get(terminal_id)

    rig.states.get = counting_get  # type: ignore[method-assign]
    rig.projector.sweep()

    assert TERMINAL in reads


def test_a_terminal_deleted_mid_sweep_is_forgotten_not_degraded(rig: Rig) -> None:
    """The roster can name a row that is gone by the time the loop reaches it."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.clock.advance(NO_SIGNAL_S + 1)
    rig.health.mark(TERMINAL, projected=True)

    class _Vanishing:
        def __getattr__(self, name: str) -> object:
            return getattr(rig.states, name)

        def get(self, terminal_id: str) -> object:
            return None

        def all_terminals(self) -> list[object]:
            return [_Row(TERMINAL)]

    class _Row:
        def __init__(self, terminal_id: str) -> None:
            self.terminal_id = terminal_id

    rig.projector._states = _Vanishing()  # type: ignore[attr-defined]
    outcomes = rig.projector.sweep()

    assert outcomes == []
    assert rig.health.is_projected(TERMINAL) is False


def _kinds(rig: Rig) -> list[AnyKind]:  # pragma: no cover - debugging helper
    return [row.kind for row in rig.events.read(TERMINAL)]


# ------------------------------------------- the liveness columns (R2)


def test_a_heartbeat_that_lands_mid_fold_is_not_overwritten(rig: Rig) -> None:
    """The projector's lock cannot help here, so the WRITE has to be narrower.

    ``touch_probe`` and ``touch_source_probe`` are partial-column writes owned by
    the liveness probe and the rollout tailer, and neither takes the projector's
    lock — neither has any business waiting on a fold.  So a full-row ``upsert``
    would overwrite a heartbeat that landed between ``_load`` and the write with
    a value seconds old, and both readers of those stamps make that expensive:
    ``_last_signal`` would judge a live terminal silent and degrade it, and
    ``_source_healthy`` would flip ``is_projected`` off for a healthy lane.
    """
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    probed_at = rig.clock.advance(1)
    rig.states.touch_probe(
        TERMINAL, probed_at=probed_at, pane_present=True, pane_pid=4242, miss_count=0
    )
    rig.states.touch_source_probe(TERMINAL, probed_at=probed_at)

    rig.emit(TERMINAL, EventKind.TURN_ENDED)  # a fold whose snapshot predates them

    row = rig.states.get(TERMINAL)
    assert row.state is WorkerState.IDLE
    assert row.last_probe_at == probed_at
    assert row.last_source_probe_at == probed_at
    assert row.pane_pid == 4242
    assert row.pane_present is True


def test_a_heartbeat_that_lands_mid_sweep_is_not_overwritten(rig: Rig) -> None:
    """Same property on the sweep's write, which is the one that degrades."""
    rig.emit(TERMINAL, EventKind.TURN_STARTED)
    rig.clock.advance(NO_SIGNAL_S + 1)
    probed_at = rig.states.rows[TERMINAL].last_probe_at

    rig.projector.sweep()

    assert rig.state_of(TERMINAL) is WorkerState.DEGRADED
    assert rig.states.get(TERMINAL).last_probe_at == probed_at
