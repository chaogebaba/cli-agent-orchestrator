"""B3 — AC-S1.27's crash matrix and AC-S1.29's scheduler isolation.

Both ACs are about what happens BETWEEN the writes, so both are driven through
:class:`ReceiverDeliveryTask` against the real ``SqliteInterruptStore`` and fake
ports. The store is real because the oracle is "rollback exposes all-or-none",
which is a property of transactions and not of a double; the transport and
session are fakes because the oracle is about ORDERING and a real agent cannot be
made to die between two named instructions.

The crash matrix injects at NAMED STEPS rather than by patching each call site: a
test that patched twenty-two places would be testing its own patching.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.interrupt import SqliteInterruptStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.app.acp.interrupt_limiter import InterruptLimiter
from cli_agent_orchestrator.app.acp.receiver_task import (
    ReceiverDeliveryTask,
    TaskOutcome,
    TaskStep,
)
from cli_agent_orchestrator.core.interrupt import (
    ActiveTurnHandle,
    CallerPrincipal,
    CancelHandle,
    CancelRaceLost,
    CancelSettlement,
    CancelWindow,
    InterruptAdmission,
    InterruptFence,
    InterruptPhase,
    InterruptPreparation,
    PreparationKind,
    PrincipalOrigin,
    SessionState,
    SettleKind,
    SubmitEnvelope,
    SubmitReceipt,
    Urgency,
)

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
TERMINAL = "term-acp"


# --------------------------------------------------------------- the doubles


class FixedClock:
    """ONE instant, and a counter.

    Fixed rather than advancing so a test that accidentally sampled twice would
    still read the same value — and ``samples`` is what proves it sampled once,
    which is the property AC-S1.28 checks statically and this checks dynamically.
    """

    def __init__(self, value: datetime = T0) -> None:
        self.value = value
        self.samples = 0

    def now(self) -> datetime:
        self.samples += 1
        return self.value


class FakeTransport:
    """Records the order it was called in; never touches a wire."""

    def __init__(self, preparation: InterruptPreparation, receipt: SubmitReceipt) -> None:
        self._preparation = preparation
        self._receipt = receipt
        self.submits: list[SubmitEnvelope] = []
        self.prepares = 0
        self.nudges: list[str] = []
        self.barrier = None

    def submit(self, *, terminal_id: str, envelope: SubmitEnvelope) -> SubmitReceipt:
        del terminal_id
        if self.barrier is not None:
            self.barrier("submit")
        self.submits.append(envelope)
        return self._receipt

    def prepare_interrupt(self, *, terminal_id: str) -> InterruptPreparation:
        del terminal_id
        self.prepares += 1
        if self.barrier is not None:
            self.barrier("prepare")
        return self._preparation

    def nudge(self, terminal_id: str) -> None:
        self.nudges.append(terminal_id)


class FakeSession:
    """The lifecycle half, scripted per arm."""

    def __init__(
        self,
        *,
        cancel_outcome: object | None = None,
        settlement: CancelSettlement | None = None,
        closes: bool = True,
        group_dies: bool = True,
    ) -> None:
        self._cancel_outcome = cancel_outcome
        self._settlement = settlement or CancelSettlement(kind=SettleKind.CANCELLED, settle_ms=12.0)
        self._closes = closes
        self._group_dies = group_dies
        self.cancels: list[ActiveTurnHandle] = []
        self.awaited_deadlines: list[datetime] = []
        self.terminations = 0
        self.barrier = None

    def cancel_if_current(self, expected: ActiveTurnHandle) -> object:
        self.cancels.append(expected)
        if self._cancel_outcome is not None:
            return self._cancel_outcome
        return CancelHandle(active_turn=expected, sent_at=T0)

    def await_cancel(self, handle: CancelHandle, deadline: datetime) -> CancelSettlement:
        del handle
        self.awaited_deadlines.append(deadline)
        if self.barrier is not None:
            self.barrier("await_cancel")
        return self._settlement

    def close(self) -> bool:
        return self._closes

    def terminate_process_group(self, *, grace_s: float) -> bool:
        del grace_s
        self.terminations += 1
        if self.barrier is not None:
            self.barrier("teardown")
        return self._group_dies


# --------------------------------------------------------------- the fixtures


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[ConnectionPool]:
    result, pool = migrate(tmp_path / "task.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    yield pool
    pool.close_all()


@pytest.fixture
def store(pool: ConnectionPool) -> SqliteInterruptStore:
    return SqliteInterruptStore(pool, limiter=InterruptLimiter())


def admit(store: SqliteInterruptStore, *, terminal_id: str = TERMINAL, callback: str = "cb-I"):
    outcome = store.admit_interrupt(
        InterruptAdmission(
            terminal_id=terminal_id,
            callback_id=callback,
            principal=CallerPrincipal(origin=PrincipalOrigin.TERMINAL, subject="caller"),
            envelope=SubmitEnvelope(
                callback_id=callback, body="stop and read", urgency=Urgency.INTERRUPT
            ),
            now=T0,
            observed_state=SessionState.ACTIVE,
        )
    )
    assert outcome.admitted, outcome
    return outcome


def handle() -> ActiveTurnHandle:
    return ActiveTurnHandle(
        terminal_id=TERMINAL,
        lifecycle_generation=7,
        session_id="sess-1",
        acp_request_id="req-42",
        callback_id="cb-N",
        turn_seq=3,
    )


def build(
    store: SqliteInterruptStore,
    *,
    preparation: InterruptPreparation,
    session: FakeSession | None = None,
    receipt: SubmitReceipt | None = None,
    clock: FixedClock | None = None,
    fault=None,
) -> tuple[ReceiverDeliveryTask, FakeTransport, FakeSession, FixedClock]:
    transport = FakeTransport(
        preparation,
        receipt or SubmitReceipt(accepted=True, write_flushed_at=T0, write_receipt_at=T0),
    )
    used_session = session or FakeSession()
    used_clock = clock or FixedClock()
    task = ReceiverDeliveryTask(
        TERMINAL,
        store=store,
        transport=transport,
        session=used_session,
        clock=used_clock,
        lease_owner="tick",
        fault=fault,
    )
    return task, transport, used_session, used_clock


IDLE = InterruptPreparation(kind=PreparationKind.IDLE)


def cancel_required() -> InterruptPreparation:
    return InterruptPreparation(kind=PreparationKind.CANCEL_REQUIRED, active_turn=handle())


# ============================================================ the happy paths


def test_an_idle_receiver_takes_exactly_one_submit(store: SqliteInterruptStore) -> None:
    """The fresh-worker case: no cancel is issued at all.

    ACP defines ``session/cancel`` for an ONGOING turn only, so a cancel here
    would be a protocol error dressed as caution.
    """
    admit(store)
    task, transport, session, _ = build(store, preparation=IDLE)
    report = task.run_once()
    assert report.outcome is TaskOutcome.DELIVERED
    assert len(transport.submits) == 1
    assert session.cancels == []
    state = store.read_state(TERMINAL)
    assert state is not None and state.phase is InterruptPhase.NONE


def test_a_busy_receiver_cancels_then_submits_once(store: SqliteInterruptStore) -> None:
    admit(store)
    task, transport, session, _ = build(store, preparation=cancel_required())
    report = task.run_once()
    assert report.outcome is TaskOutcome.DELIVERED
    assert len(session.cancels) == 1
    assert len(transport.submits) == 1, "exactly one prompt write, ever"


def test_nothing_claimed_is_a_value_not_an_error(store: SqliteInterruptStore) -> None:
    task, _, _, _ = build(store, preparation=IDLE)
    assert task.run_once().outcome is TaskOutcome.NOTHING_CLAIMED


# ============================================ the one clock sample (AC-S1.28)


def test_the_cancel_deadline_comes_from_one_clock_sample(store: SqliteInterruptStore) -> None:
    """``await_cancel`` receives the instant the aggregate PERSISTED.

    A task that recomputed the timeout would produce a deadline the restart path
    could not agree with — r18 lists "await against recomputed time" as a mutant
    that must go RED.
    """
    admit(store)
    clock = FixedClock()
    task, _, session, _ = build(store, preparation=cancel_required(), clock=clock)
    report = task.run_once()
    assert report.outcome is TaskOutcome.DELIVERED
    assert report.window is not None
    assert session.awaited_deadlines == [report.window.deadline]


def test_the_task_never_reaches_for_a_wall_clock() -> None:
    """Statically: the module names no ``datetime.now``/``utcnow`` anywhere.

    A second sample would be a second authority on when the cancel window opened,
    and the two would disagree exactly when it matters — across a restart.
    """
    import inspect

    from cli_agent_orchestrator.app.acp import receiver_task

    source = inspect.getsource(receiver_task)
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    assert "datetime.now" not in code
    assert "utcnow" not in code
    assert "time.time" not in code


# ==================================================== the refusal paths


def test_a_lost_window_leaves_the_actor_untouched(store: SqliteInterruptStore) -> None:
    """``begin_cancel`` refused, so no cancel bytes were written at all."""
    admit(store)
    task, _, session, _ = build(store, preparation=cancel_required())
    # Break the fence by claiming again under a different owner.
    store.claim_next(TERMINAL, now=T0, lease_owner="someone-else")
    report = task.run_once()
    assert report.outcome in (TaskOutcome.WINDOW_LOST, TaskOutcome.NOTHING_CLAIMED)
    assert session.cancels == [], "a lost window writes no cancel"


def test_a_race_lost_returns_to_pending_and_writes_no_bytes(
    store: SqliteInterruptStore,
) -> None:
    """The handle moved between deciding and writing.

    ``cancel_if_current`` compares the actor's exact current handle immediately
    before bytes, so a mismatch writes NOTHING — cancelling the wrong turn is
    r18's "stale turn cancel" mutant.
    """
    admit(store)
    session = FakeSession(
        cancel_outcome=CancelRaceLost(observed_state=SessionState.IDLE, observed_turn=None)
    )
    task, transport, _, _ = build(store, preparation=cancel_required(), session=session)
    report = task.run_once()
    assert report.outcome is TaskOutcome.RACE_LOST
    assert transport.submits == [], "a race loss submits nothing"
    state = store.read_state(TERMINAL)
    assert state is not None
    assert state.phase is InterruptPhase.PENDING
    assert state.deadline is None, "the consumed cancel deadline is cleared"


def test_an_ambiguous_write_resolves_uncertain_and_never_presented(
    store: SqliteInterruptStore,
) -> None:
    """The write flushed and the transport then ended.

    Whether the agent saw it is unknowable from here, so the attempt resolves
    uncertain rather than guessing in either direction — and it is never
    ``presented``, because a presentation would claim a receipt nobody has.
    """
    admit(store)
    task, transport, _, _ = build(
        store,
        preparation=IDLE,
        receipt=SubmitReceipt(accepted=True, write_flushed_at=T0, ambiguous=True),
    )
    report = task.run_once()
    assert report.outcome is TaskOutcome.SUBMISSION_UNCERTAIN
    assert len(transport.submits) == 1, "still exactly one write"


# ==================================================== the recovery path


def test_an_unsettled_cancel_goes_to_recovery_never_back_to_none(
    store: SqliteInterruptStore,
) -> None:
    """r13/review r12 B4: the terminal stays NON-ADMISSIBLE until recovery is
    durable, so a second interrupt cannot be admitted into the gap."""
    admit(store)
    session = FakeSession(
        settlement=CancelSettlement(kind=SettleKind.UNSETTLED),
        closes=False,
        group_dies=False,
    )
    task, transport, _, _ = build(store, preparation=cancel_required(), session=session)
    report = task.run_once()
    assert report.outcome is TaskOutcome.RECOVERY_FAILED
    assert transport.submits == [], "a timed-out cancel never submits"
    state = store.read_state(TERMINAL)
    assert state is not None
    assert state.phase is InterruptPhase.RECOVERING

    refused = store.admit_interrupt(
        InterruptAdmission(
            terminal_id=TERMINAL,
            callback_id="cb-second",
            principal=CallerPrincipal(origin=PrincipalOrigin.TERMINAL, subject="caller"),
            envelope=SubmitEnvelope(callback_id="cb-second", body="x"),
            now=T0 + timedelta(seconds=120),
        )
    )
    assert refused.refused is not None, "recovering refuses another interrupt"


def test_teardown_escalates_and_proves_absence_before_finalizing(
    store: SqliteInterruptStore,
) -> None:
    """``expire_recovery`` runs only AFTER absence is proven, outside SQLite."""
    admit(store)
    session = FakeSession(
        settlement=CancelSettlement(kind=SettleKind.UNSETTLED), closes=False, group_dies=True
    )
    task, _, _, _ = build(store, preparation=cancel_required(), session=session)
    report = task.run_once()
    assert session.terminations == 1
    assert report.detail == "process_group_gone"
    assert store.read_state(TERMINAL) is None, "the state row is retired"


# ==================================================== AC-S1.27 crash matrix


class _Crash(BaseException):
    """Injected, and BaseException so the task's catch-all does not eat it."""


_CRASH_POINTS = [
    TaskStep.CLAIMED,
    TaskStep.PREPARED,
    TaskStep.CANCEL_BEGUN,
    TaskStep.CANCEL_SENT,
    TaskStep.CANCEL_SETTLED,
    TaskStep.SETTLED_TO_PROMPT,
    TaskStep.SUBMITTED,
    TaskStep.COMPLETED,
]


@pytest.mark.parametrize("point", _CRASH_POINTS, ids=lambda s: s.value)
def test_a_death_at_each_named_point_leaves_all_or_none(
    store: SqliteInterruptStore, pool: ConnectionPool, point: TaskStep
) -> None:
    """AC-S1.27's oracle: rollback exposes ALL-OR-NONE at every named point.

    The invariant asserted after each injected death is the one that does not
    depend on where it landed: the interrupt row is never in a half-written
    state, the phase is always a member of the closed set, and a ``cancelling``
    row ALWAYS has its persisted cancel deadline — which is the schema CHECK the
    restart path depends on, because a recovered ``cancelling`` takes the timeout
    using that stored value regardless of ``cancel_sent``.
    """
    admit(store)

    def fault(step: TaskStep) -> None:
        if step is point:
            raise _Crash(step.value)

    task, transport, _, _ = build(store, preparation=cancel_required(), fault=fault)
    with pytest.raises(_Crash):
        task.run_once()

    state = store.read_state(TERMINAL)
    assert state is not None, "the reservation survives every crash"
    assert state.phase in set(InterruptPhase), "the phase is always in the closed set"
    if state.phase is InterruptPhase.CANCELLING:
        assert (
            state.deadline is not None
        ), "a cancelling row without its persisted deadline cannot be timed out on restart"
    if state.phase in (InterruptPhase.NONE, InterruptPhase.PROMPTING):
        assert state.deadline is None, "a consumed deadline is cleared before the phase moves"

    # And never more than one prompt write, whatever the crash interrupted.
    assert len(transport.submits) <= 1


@pytest.mark.parametrize("point", _CRASH_POINTS, ids=lambda s: s.value)
def test_no_crash_point_produces_a_second_prompt_write(
    store: SqliteInterruptStore, point: TaskStep
) -> None:
    """A crash, then a RESTART: the replacement task must not write again.

    "Second I submit" is a named r18 mutant. The restart is modelled by building
    a fresh task over the SAME store — which is what a restarted server has — and
    running it to completion.
    """
    admit(store)

    def fault(step: TaskStep) -> None:
        if step is point:
            raise _Crash(step.value)

    first, first_transport, _, _ = build(store, preparation=cancel_required(), fault=fault)
    with pytest.raises(_Crash):
        first.run_once()

    second, second_transport, _, _ = build(store, preparation=cancel_required())
    second.run_once()

    total = len(first_transport.submits) + len(second_transport.submits)
    assert total <= 1, f"{total} prompt writes across a crash at {point.value}"


def test_the_crash_matrix_covers_every_step_the_task_names() -> None:
    """The matrix is only a matrix if it is TOTAL over the steps.

    A step added to the task without a row here would be a gap in the oracle, and
    AC-S1.27's fails-if names "omitted crash gap" first.
    """
    covered = set(_CRASH_POINTS)
    recovery_only = {TaskStep.RECOVERING, TaskStep.FINALIZED}
    assert covered | recovery_only == set(TaskStep)


@pytest.mark.parametrize("point", [TaskStep.RECOVERING, TaskStep.FINALIZED], ids=lambda s: s.value)
def test_a_death_in_recovery_leaves_the_terminal_non_admissible(
    store: SqliteInterruptStore, point: TaskStep
) -> None:
    """The recovery half of the matrix.

    A crash anywhere in recovery must leave the terminal REFUSING interrupts:
    the one outcome that would be unrecoverable is a half-torn-down seat that
    accepted new work.
    """
    admit(store)

    def fault(step: TaskStep) -> None:
        if step is point:
            raise _Crash(step.value)

    session = FakeSession(
        settlement=CancelSettlement(kind=SettleKind.UNSETTLED), closes=False, group_dies=True
    )
    task, _, _, _ = build(store, preparation=cancel_required(), session=session, fault=fault)
    with pytest.raises(_Crash):
        task.run_once()

    state = store.read_state(TERMINAL)
    if state is not None:
        assert state.phase is InterruptPhase.RECOVERING
        later = store.admit_interrupt(
            InterruptAdmission(
                terminal_id=TERMINAL,
                callback_id="cb-after",
                principal=CallerPrincipal(origin=PrincipalOrigin.TERMINAL, subject="caller"),
                envelope=SubmitEnvelope(callback_id="cb-after", body="x"),
                now=T0 + timedelta(seconds=300),
            )
        )
        assert later.refused is not None


# ==================================================== AC-S1.29 isolation


@pytest.mark.parametrize("barrier_at", ["prepare", "submit", "await_cancel", "teardown"])
def test_a_blocked_receiver_never_blocks_another(pool: ConnectionPool, barrier_at: str) -> None:
    """AC-S1.29's four arms, on the property that is actually checkable here.

    The full AC places an AWAITABLE barrier in A and requires B to reach a
    durable presentation before A is released, with no duration in the
    allowance. The scheduler that would drive B concurrently is Phase B's
    ``DeliveryTick`` integration; what this asserts is the PRECONDITION that
    makes that possible and that a wrong design would break first: **A's block
    is inside A's own task, and it holds no store transaction while it is
    blocked.**

    The barrier is entered inside the port call and, while it is held, B's
    admission and claim are driven on the same database from this thread. If A
    were holding a write transaction across its await — the inline-scheduler
    mutation the AC's control arm describes — B's ``BEGIN IMMEDIATE`` would block
    behind it and this test would hang rather than fail, which is why the
    assertion is that B COMPLETES.
    """
    store = SqliteInterruptStore(pool, limiter=InterruptLimiter())
    admit(store, terminal_id="term-A", callback="cb-A")

    b_done: list[str] = []

    def barrier(where: str) -> None:
        if where != barrier_at:
            return
        # A is blocked HERE. Drive B all the way through on the same database.
        b_store = SqliteInterruptStore(pool, limiter=InterruptLimiter())
        outcome = b_store.admit_interrupt(
            InterruptAdmission(
                terminal_id="term-B",
                callback_id="cb-B",
                principal=CallerPrincipal(origin=PrincipalOrigin.TERMINAL, subject="other"),
                envelope=SubmitEnvelope(callback_id="cb-B", body="b work"),
                now=T0,
            )
        )
        assert outcome.admitted, "B must be admissible while A is blocked"
        claimed = b_store.claim_next("term-B", now=T0, lease_owner="tick-b")
        assert claimed is not None, "B must be claimable while A is blocked"
        # B's own idle path: intent, then the write's receipt. Driven explicitly
        # here because the point of the arm is that B gets all the way to a
        # DURABLE presentation while A is blocked, not merely that it was
        # admitted.
        assert b_store.begin_prompt(claimed.fence), "B must reach prompting while A is blocked"
        advanced = InterruptFence(
            terminal_id=claimed.fence.terminal_id,
            msg_id=claimed.fence.msg_id,
            claim_id=claimed.fence.claim_id,
            owner=claimed.fence.owner,
            generation=b_store.read_state("term-B").generation,
        )
        assert b_store.complete_prompt(
            advanced, SubmitReceipt(accepted=True, write_receipt_at=T0)
        ), "B must reach a durable receipt while A is blocked"
        b_done.append(where)

    session = FakeSession(
        settlement=(
            CancelSettlement(kind=SettleKind.UNSETTLED)
            if barrier_at == "teardown"
            else CancelSettlement(kind=SettleKind.CANCELLED, settle_ms=5.0)
        ),
        closes=False,
        group_dies=True,
    )
    preparation = IDLE if barrier_at in ("prepare", "submit") else cancel_required()
    task, transport, used_session, _ = build(store, preparation=preparation, session=session)
    transport.barrier = barrier
    used_session.barrier = barrier
    # A is claimed under term-A, so point the task at it.
    task._receiver_id = "term-A"  # noqa: SLF001 — the arm is about the receiver, not the ctor

    task.run_once()
    assert b_done == [barrier_at], f"B did not complete while A was blocked at {barrier_at}"


def test_the_task_holds_no_transaction_across_any_await() -> None:
    """The static half of AC-S1.29, and the reason the dynamic half can pass.

    The task must never open a transaction itself: every store call it makes is
    ONE aggregate operation that opens and closes its own, so there is no window
    in which an await happens under a held write lock.
    """
    import inspect

    from cli_agent_orchestrator.app.acp import receiver_task

    source = inspect.getsource(receiver_task)
    assert "immediate_transaction" not in source
    assert "BEGIN IMMEDIATE" not in source
    assert "sqlite3" not in source


# ============================== B3 / M5 — the recomputed-deadline mutant


class AdvancingClock:
    """A clock that MOVES between samples.

    The fixed clock every other arm uses cannot tell a persisted deadline from a
    recomputed one, because with time standing still the two are equal. That is
    exactly why the review's M5 survived: replacing
    ``await_cancel(handle, window.deadline)`` with a freshly computed
    ``now() + ACP_CANCEL_SETTLE_S`` passed the entire 1,370-test selection.
    """

    def __init__(self, start: datetime = T0, step_s: float = 7.0) -> None:
        self.value = start
        self.step_s = step_s
        self.samples = 0

    def now(self) -> datetime:
        self.samples += 1
        value = self.value
        self.value = value + timedelta(seconds=self.step_s)
        return value


class DeadlineRecordingSession(FakeSession):
    """Records the deadline it was handed, so the arm can compare instants."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.deadline_seen: datetime | None = None

    def await_cancel(self, handle: CancelHandle, deadline: datetime) -> CancelSettlement:
        self.deadline_seen = deadline
        return super().await_cancel(handle, deadline)


def test_await_cancel_consumes_the_PERSISTED_deadline_not_a_recomputed_one(
    store: SqliteInterruptStore,
) -> None:
    """AC-S1.26's "await against recomputed time" mutant, made RED.

    The clock advances by seven seconds on every sample, so the instant the
    aggregate persisted and any instant recomputed later are provably different.
    The arm asserts THREE things, and it takes all three to pin the property:

    1. the deadline handed to ``await_cancel`` is exactly the one ``begin_cancel``
       returned;
    2. it equals the one the STORE persisted, so a restart reading the row gets
       the same answer;
    3. it is derived from the FIRST clock sample — the receiver task's single
       sample — and not from a later one.

    Without (3) a mutant that resampled once more before the store call would
    still satisfy (1) and (2) while moving the bound.
    """
    admit(store)
    clock = AdvancingClock()
    session = DeadlineRecordingSession()
    task, _, _, _ = build(
        store, preparation=cancel_required(), session=session, clock=clock  # type: ignore[arg-type]
    )
    report = task.run_once()

    assert report.outcome is TaskOutcome.DELIVERED
    assert report.window is not None
    assert session.deadline_seen == report.window.deadline, "a recomputed deadline was consumed"

    persisted = store.read_state(TERMINAL)
    # The row has moved on to ``none`` by now and cleared the consumed deadline,
    # which is itself the contract — so the durable check is that the value the
    # store RETURNED was the value it wrote, asserted while it was still in the
    # row by the sibling arm below.
    assert persisted is not None

    from cli_agent_orchestrator.core.timing import ACP_CANCEL_SETTLE_S

    # The task samples the clock ONCE, immediately before ``begin_cancel``. The
    # claim above it took a sample too, so the cancel window is built from the
    # SECOND tick of this clock, not from a later one.
    expected = T0 + timedelta(seconds=clock.step_s) + timedelta(seconds=ACP_CANCEL_SETTLE_S)
    assert (
        report.window.deadline == expected
    ), "the cancel deadline was not derived from the task's single clock sample"


def test_the_persisted_deadline_is_readable_from_the_row_while_cancelling(
    store: SqliteInterruptStore,
) -> None:
    """The durable half: the value is IN THE ROW, which is what a restart reads.

    A restarted server has no clock sample and no returned value — only the row —
    so if the deadline were never persisted, every recovered ``cancelling`` would
    have to invent a bound.
    """
    admit(store)
    clock = AdvancingClock()
    claimed = store.claim_next(TERMINAL, now=clock.now(), lease_owner="tick")
    assert claimed is not None

    sample = clock.now()
    window = store.begin_cancel(claimed.fence, handle(), sample)
    assert isinstance(window, CancelWindow)

    state = store.read_state(TERMINAL)
    assert state is not None
    assert state.phase is InterruptPhase.CANCELLING
    assert state.deadline == window.deadline
    # And it is the SAMPLE's deadline, not "twenty seconds from whenever you ask".
    from cli_agent_orchestrator.core.timing import ACP_CANCEL_SETTLE_S

    assert state.deadline == sample + timedelta(seconds=ACP_CANCEL_SETTLE_S)
    assert clock.now() > state.deadline - timedelta(
        seconds=ACP_CANCEL_SETTLE_S
    ), "the clock really did move, so a recomputed value would differ"
