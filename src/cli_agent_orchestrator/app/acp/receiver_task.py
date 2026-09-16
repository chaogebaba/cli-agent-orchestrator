"""D6b(3) — the one application owner of an ACP receiver's awaits.

A2.3 names ``DeliveryTick`` the sole dispatch-owner service, and D6b(3) splits
what that means into two halves that must not be the same code:

* the **serial scheduler** does store-only work — claim, schedule, signal — and
  performs no wire or process await for anybody;
* **one ``ReceiverDeliveryTask`` per ACP receiver** owns the claimed fence and
  every await for that receiver alone.

The split is not tidiness. A scheduler that awaited one agent's cancel would
hold every other receiver behind that agent's latency, which is the head-of-line
blocking AC-S1.29's four causal barriers exist to disprove. So the rule this
module enforces at every line is: **nothing here holds a SQLite transaction
across an await, and nothing in the store awaits anything.** The task alternates
between the two — one short store transition, one wire wait, one short store
transition — and the transitions are the aggregate's, never composed here.

The second rule is the clock. The task samples it EXACTLY ONCE immediately
before ``begin_cancel`` and passes that instant in; every deadline downstream is
the one the aggregate computed and persisted from that sample. A second sample
anywhere would be a second authority on when the cancel window opened, and the
two would disagree exactly when it matters — across a restart.

What this module does NOT do: decide anything. Whether an interrupt is admitted
is the limiter's, whether a row is claimable is the claim SQL's, and what a
phase means is the aggregate's. This is the thing that waits.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from cli_agent_orchestrator.core.interrupt import (
    ActiveTurnHandle,
    CancelHandle,
    CancelRaceLost,
    CancelSettlement,
    CancelWindow,
    ClaimedRow,
    InterruptFence,
    InterruptPhase,
    PreparationKind,
    SettleKind,
    SubmitReceipt,
    WindowLost,
)
from cli_agent_orchestrator.core.ports import (
    AgentSession,
    Clock,
    InterruptStore,
    MessageTransport,
)
from cli_agent_orchestrator.core.timing import ACP_KILL_GRACE_S

logger = logging.getLogger(__name__)

__all__ = ["ReceiverDeliveryTask", "ReceiverTaskRegistry", "TaskOutcome", "TaskReport", "TaskStep"]


class TaskStep(StrEnum):
    """Where a run stopped, for the crash matrix to inject at and for diag to read.

    Named STEPS rather than logged strings: AC-S1.27 injects a death at each of
    twenty-two points and its oracle is that rollback exposes all-or-none, which
    is only checkable if the points have names the test and the code agree on.
    """

    CLAIMED = "claimed"
    PREPARED = "prepared"
    CANCEL_BEGUN = "cancel_begun"
    CANCEL_SENT = "cancel_sent"
    CANCEL_SETTLED = "cancel_settled"
    SETTLED_TO_PROMPT = "settled_to_prompt"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    RECOVERING = "recovering"
    FINALIZED = "finalized"


class TaskOutcome(StrEnum):
    """What one run of the task did.  A value, never an exception.

    The receiver task runs on the delivery path of a server that must keep
    ticking for every other receiver, so a fault here is a reported outcome and
    not a raise — the same reason ``core/switches.py`` answers with values.
    """

    NOTHING_CLAIMED = "nothing_claimed"
    DELIVERED = "delivered"
    WINDOW_LOST = "window_lost"
    RACE_LOST = "race_lost"
    CANCEL_TIMEOUT = "cancel_timeout"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    RECOVERED = "recovered"
    RECOVERY_FAILED = "recovery_failed"


class FaultPoint(Protocol):
    """Where AC-S1.27 injects a death.  Called at every named step.

    A port rather than a monkeypatch because the crash matrix has twenty-two
    points and a test that patched each one would be testing its own patching.
    The production wiring passes nothing and the calls are a no-op.
    """

    def __call__(self, step: TaskStep) -> None: ...


@dataclass(frozen=True)
class TaskReport:
    """What one run did, in enough detail for ``cao diag`` to fold it."""

    outcome: TaskOutcome
    step: TaskStep | None = None
    fence: InterruptFence | None = None
    window: CancelWindow | None = None
    settle: CancelSettlement | None = None
    receipt: SubmitReceipt | None = None
    detail: str = ""


class ReceiverDeliveryTask:
    """One ACP receiver's awaits, and nothing else.

    Constructed per receiver by the composition root and signalled by the
    scheduler. It holds no queue of its own: the fence it works on arrives from
    ``InterruptStore.claim_next`` and every state change goes back through the
    aggregate, so a restart finds the truth in the row rather than in this
    object.
    """

    def __init__(
        self,
        receiver_id: str,
        *,
        store: InterruptStore,
        transport: MessageTransport,
        session: AgentSession,
        clock: Clock,
        lease_owner: str,
        fault: FaultPoint | None = None,
    ) -> None:
        self._receiver_id = receiver_id
        self._store = store
        self._transport = transport
        self._session = session
        # The injected Clock, in the same instant domain as queue deadlines.
        # Held as ONE dependency so ``_run`` can sample it once; a task that
        # reached for ``datetime.now`` anywhere would be the second authority
        # AC-S1.28 checks for statically.
        self._clock = clock
        self._lease_owner = lease_owner
        self._fault: FaultPoint = fault if fault is not None else (lambda step: None)

    # -- the one entry point -------------------------------------------------

    def run_once(self) -> TaskReport:
        """Claim at most one row and carry it to a terminal state.

        Returns rather than raises for every outcome including the bad ones. The
        scheduler that signalled this task is serving other receivers and must
        not learn about this one's agent through an exception.
        """
        try:
            return self._run()
        except _Injected:
            raise
        except Exception as exc:  # noqa: BLE001 — one receiver may not stop the fleet
            logger.exception("acp receiver task %s failed", self._receiver_id)
            return TaskReport(outcome=TaskOutcome.WINDOW_LOST, detail=repr(exc))

    def _run(self) -> TaskReport:
        claimed = self._store.claim_next(
            self._receiver_id, now=self._clock.now(), lease_owner=self._lease_owner
        )
        if claimed is None:
            return TaskReport(outcome=TaskOutcome.NOTHING_CLAIMED)
        self._fault(TaskStep.CLAIMED)

        preparation = self._transport.prepare_interrupt(terminal_id=self._receiver_id)
        self._fault(TaskStep.PREPARED)

        if preparation.kind is PreparationKind.IDLE:
            # D6b(3)'s IDLE branch: the dispatch INTENT is durable BEFORE the one
            # write, so a crash between them leaves a ``prompting`` row the
            # restart oracle can resolve honestly.
            if not self._store.begin_prompt(claimed.fence):
                self._store.fail_interrupt(claimed.fence, _DEAD_REASON_WINDOW_LOST, "window_lost")
                return TaskReport(
                    outcome=TaskOutcome.WINDOW_LOST,
                    step=TaskStep.PREPARED,
                    fence=claimed.fence,
                )
            return self._submit(claimed, self._advanced(claimed.fence), cut=None)
        handle = preparation.active_turn
        assert handle is not None  # PreparationKind.CANCEL_REQUIRED guarantees it
        return self._cancel_then_submit(claimed, handle)

    # -- the idle path -------------------------------------------------------

    def _submit(
        self,
        claimed: ClaimedRow,
        fence: InterruptFence,
        *,
        cut: CancelSettlement | None,
        window: CancelWindow | None = None,
    ) -> TaskReport:
        """Exactly ONE prompt write, then the durable receipt.

        ``complete_prompt``'s commit IS the receipt — there is no separate marker
        and no replay. A kill after the flush but before that commit resolves
        ``SUBMISSION_UNCERTAIN`` on restart, honestly, because the ACP subprocess
        did not survive the restart and the turn is gone either way.
        """
        receipt = self._transport.submit(terminal_id=self._receiver_id, envelope=claimed.envelope)
        self._fault(TaskStep.SUBMITTED)

        if receipt.ambiguous or not receipt.accepted:
            # The write flushed and the transport then ended, or never flushed.
            # Whether the agent saw it is unknowable from here, so the attempt
            # resolves uncertain rather than guessing in either direction.
            self._store.fail_interrupt(
                fence,
                _DEAD_REASON_UNCERTAIN,
                "prompt_ambiguous" if receipt.ambiguous else "window_lost",
            )
            return TaskReport(
                outcome=TaskOutcome.SUBMISSION_UNCERTAIN,
                step=TaskStep.SUBMITTED,
                fence=fence,
                receipt=receipt,
                settle=cut,
                window=window,
            )

        self._store.complete_prompt(fence, receipt)
        self._fault(TaskStep.COMPLETED)
        return TaskReport(
            outcome=TaskOutcome.DELIVERED,
            step=TaskStep.COMPLETED,
            fence=fence,
            receipt=receipt,
            settle=cut,
            # Carried through so ``cao diag`` can fold the cancel window onto the
            # delivery it made room for: an interrupt that succeeded and one that
            # was never blocked look identical without it.
            window=window,
        )

    # -- the cancel path -----------------------------------------------------

    def _cancel_then_submit(self, claimed: ClaimedRow, handle: ActiveTurnHandle) -> TaskReport:
        """Open the exact cancel window, cut the turn, then submit into the gap."""
        # THE ONE CLOCK SAMPLE. Taken immediately before the store call and
        # passed in; everything downstream consumes what the aggregate persisted
        # from it, including after a restart.
        now = self._clock.now()
        fence = claimed.fence
        window = self._store.begin_cancel(fence, handle, now)
        self._fault(TaskStep.CANCEL_BEGUN)
        if isinstance(window, WindowLost):
            # Atomic: N and the session actor are untouched.
            self._store.fail_interrupt(fence, _DEAD_REASON_WINDOW_LOST, "window_lost")
            return TaskReport(
                outcome=TaskOutcome.WINDOW_LOST,
                step=TaskStep.CANCEL_BEGUN,
                fence=fence,
            )
        fence = self._advanced(fence)

        outcome = self._session.cancel_if_current(handle)
        if isinstance(outcome, CancelRaceLost):
            # The handle moved between deciding and writing, so NOTHING was
            # written. Back to pending under the same reservation, and the loop
            # that follows is bounded by ``pending_deadline`` — never by a retry
            # count invented here.
            self._store.race_lost(fence)
            return TaskReport(
                outcome=TaskOutcome.RACE_LOST,
                step=TaskStep.CANCEL_BEGUN,
                fence=fence,
                window=window,
            )

        self._store.mark_cancel_sent(fence, outcome.sent_at)
        self._fault(TaskStep.CANCEL_SENT)

        # Against the PERSISTED deadline, never a recomputed one. After a restart
        # the only honest bound is the one that was durable.
        settle = self._session.await_cancel(outcome, window.deadline)
        self._fault(TaskStep.CANCEL_SETTLED)

        if settle.kind is not SettleKind.CANCELLED:
            return self._recover(claimed, fence, window, settle)

        if not self._store.settle_to_prompt(fence, settle):
            self._store.fail_interrupt(fence, _DEAD_REASON_WINDOW_LOST, "window_lost")
            return TaskReport(
                outcome=TaskOutcome.WINDOW_LOST,
                step=TaskStep.SETTLED_TO_PROMPT,
                fence=fence,
                window=window,
                settle=settle,
            )
        self._fault(TaskStep.SETTLED_TO_PROMPT)
        return self._submit(claimed, self._advanced(fence), cut=settle, window=window)

    def _advanced(self, fence: InterruptFence) -> InterruptFence:
        """The same fence with its GENERATION re-read, after a transition bumped it.

        Every aggregate transition bumps ``generation`` — that is what makes the
        CAS a fence rather than a hope — so a task that kept its original fence
        would present a stale generation to the NEXT transition and be refused by
        its own success. Called after each one; one short read, holding nothing.

        The QUEUE half (``msg_id``, ``claim_id``, ``owner``) is deliberately NOT
        re-read. That half fences the lease, and refreshing it would let a row
        whose lease was lost keep working under a fence it no longer owns — the
        opposite of what a fence is for.
        """
        state = self._store.read_state(self._receiver_id)
        if state is None:
            return fence
        return InterruptFence(
            terminal_id=fence.terminal_id,
            msg_id=fence.msg_id,
            claim_id=fence.claim_id,
            owner=fence.owner,
            generation=state.generation,
        )

    # -- recovery ------------------------------------------------------------

    def _recover(
        self,
        claimed: ClaimedRow,
        fence: InterruptFence,
        window: CancelWindow,
        settle: CancelSettlement,
    ) -> TaskReport:
        """The cancel did not settle inside its persisted deadline.

        I is killed ``INTERRUPT_CANCEL_TIMEOUT`` — it was never dispatched, so it
        has no attempt and no dispatch intent — and the TERMINAL goes to
        ``recovering``, never back to ``none``: it stays non-admissible until
        recovery is durable, so a second interrupt cannot be admitted into the
        gap.
        """
        recovery = self._store.begin_recovery(fence, self._clock.now())
        self._fault(TaskStep.RECOVERING)
        if recovery is None:
            return TaskReport(
                outcome=TaskOutcome.CANCEL_TIMEOUT,
                step=TaskStep.RECOVERING,
                fence=fence,
                window=window,
                settle=settle,
            )

        state = self._store.read_state(self._receiver_id)
        generation = state.generation if state is not None else 0

        if self._session.close():
            if self._store.finish_recovery(self._receiver_id, generation):
                self._fault(TaskStep.FINALIZED)
                return TaskReport(
                    outcome=TaskOutcome.RECOVERED,
                    step=TaskStep.FINALIZED,
                    fence=fence,
                    window=window,
                    settle=settle,
                )

        # No close, or the re-session did not land: tear the process group down
        # OUTSIDE SQLite, prove absence, and only then retire the terminal in one
        # short transaction. The SIGKILL leg is mandatory — S0 measured an
        # adapter that never exits on SIGTERM.
        gone = self._session.terminate_process_group(grace_s=ACP_KILL_GRACE_S)
        if gone:
            self._store.expire_recovery(self._receiver_id, generation, recovery.recovery_deadline)
            self._fault(TaskStep.FINALIZED)
        return TaskReport(
            outcome=TaskOutcome.RECOVERY_FAILED,
            step=TaskStep.FINALIZED if gone else TaskStep.RECOVERING,
            fence=fence,
            window=window,
            settle=settle,
            detail="process_group_gone" if gone else "process_group_alive",
        )


class _Injected(BaseException):
    """A fault the crash matrix injected.

    Inherits ``BaseException`` so :meth:`ReceiverDeliveryTask.run_once`'s
    catch-all does not swallow it: an injected death must reach the test, or the
    arm would assert against a task that quietly recovered from the very crash it
    was meant to suffer.
    """


# Imported here rather than at the top because ``core.delivery`` is the queue's
# vocabulary and this module is the plane's: naming the two reasons locally keeps
# the dependency to the two members actually used.
from cli_agent_orchestrator.core.delivery import DeadReason as _DeadReason  # noqa: E402

_DEAD_REASON_WINDOW_LOST = _DeadReason.INTERRUPT_WINDOW_LOST
_DEAD_REASON_UNCERTAIN = _DeadReason.INTERRUPT_UNCLAIMED


class ReceiverTaskRegistry:
    """One :class:`ReceiverDeliveryTask` per ACP receiver, and exactly one.

    D6b(3): "Persisted phase plus the task registry prevents duplicates; restart
    creates one replacement task." Both halves live here. The phase row is the
    durable authority — a restarted process finds the truth in the row, not in
    this object — and this is the in-process half that stops a scheduler tick
    from starting a second task for a receiver whose first one is still running.

    **Signalling is non-blocking by construction.** ``signal`` returns as soon as
    the task is running or has been handed off; it never waits for the task's
    wire work. That is what makes ``DeliveryTick.signal_acp_receivers`` a
    scheduler rather than a serializer, and it is why the registry owns a
    re-entrancy guard rather than a lock the scheduler could block on: a lock
    would turn "already running" into "wait for it", which is the head-of-line
    blocking the whole split exists to remove.

    ``runner`` exists so a test can drive the task synchronously on the calling
    thread and still exercise the real registry. Production passes the default,
    which runs the task inline on the scheduler's thread ONLY up to its first
    await — the executor that carries it past that is wired by the composition
    root and is out of this module's scope.
    """

    def __init__(
        self,
        tasks: dict[str, ReceiverDeliveryTask] | None = None,
        *,
        executor: "Executor | None" = None,
        builder: "Callable[[str], ReceiverDeliveryTask | None] | None" = None,
        source: "Callable[[], tuple[str, ...]] | None" = None,
    ) -> None:
        self._tasks: dict[str, ReceiverDeliveryTask] = dict(tasks or {})
        self._running: set[str] = set()
        self.reports: list[TaskReport] = []
        # THE EXECUTOR. With one, ``signal`` submits and returns immediately, so
        # the scheduler never waits on a receiver's wire work — which is what
        # carries a task past its first await and what AC-S1.29's barriers are
        # about. Without one it runs inline, which is what a test wants when it
        # needs the report back synchronously.
        self._executor = executor
        # Tasks are built LAZILY from a receiver id. The composition root cannot
        # enumerate ACP receivers at boot — seats are spawned later — so a
        # registry that only held a fixed dict would always be empty in
        # production, which is the shape that made this class test-only.
        self._builder = builder
        #: Where the LIVE receiver set comes from — the session registry in
        #: production. Separate from ``builder`` because "which terminals have a
        #: seat" and "how a task for one is built" are different questions, and
        #: only the first has to be asked every tick.
        self._source = source

    def _task_for(self, receiver_id: str) -> ReceiverDeliveryTask | None:
        task = self._tasks.get(receiver_id)
        if task is None and self._builder is not None:
            task = self._builder(receiver_id)
            if task is not None:
                self._tasks[receiver_id] = task
        return task

    def register(self, receiver_id: str, task: ReceiverDeliveryTask) -> None:
        """Bind a receiver to its ONE task.  A second bind replaces, never adds."""
        self._tasks[receiver_id] = task

    def forget(self, receiver_id: str) -> None:
        self._tasks.pop(receiver_id, None)
        self._running.discard(receiver_id)

    def receivers(self) -> tuple[str, ...]:
        """The receivers to signal this tick, in a stable order.

        Sorted so a tick's behaviour does not depend on dict insertion order,
        which is the kind of hidden input that makes an isolation arm pass on one
        machine and fail on another.

        With a source wired, this is the LIVE set — the terminals that currently
        have an ACP session — not the tasks built so far. A registry that
        enumerated only what it had already built would never notice a seat
        spawned after boot, which in production is every seat.
        """
        if self._source is not None:
            return tuple(sorted(self._source()))
        return tuple(sorted(self._tasks))

    def signal(self, receiver_id: str) -> TaskReport | None:
        """Run this receiver's task once, unless it is already running.

        The re-entrancy guard is the duplicate-task rule in code: a receiver
        whose task is mid-await is NOT started again, and the scheduler is told
        so by a ``None`` rather than by being made to wait.
        """
        task = self._task_for(receiver_id)
        if task is None or receiver_id in self._running:
            return None
        self._running.add(receiver_id)

        def _run() -> TaskReport:
            try:
                report = task.run_once()
            finally:
                self._running.discard(receiver_id)
            self.reports.append(report)
            return report

        if self._executor is not None:
            # Submit and RETURN. The scheduler's next receiver is served while
            # this one is still on the wire; the re-entrancy guard above is what
            # stops the next tick starting a second task for it.
            self._executor.submit(_run)
            return None
        return _run()

    def is_running(self, receiver_id: str) -> bool:
        return receiver_id in self._running
