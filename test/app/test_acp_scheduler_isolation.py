"""AC-S1.29 in full — four causal barriers against the INTEGRATED scheduler.

The earlier arms in ``test_acp_receiver_task.py`` proved the precondition: A's
block sits inside A's own task and holds no store transaction. These prove the
claim the AC actually makes, which is about the SCHEDULER: with A blocked at each
of the four named points, B is driven to a durable presentation BY THE REAL
``DeliveryTick``, and only then is A released.

Two things make these causal rather than temporal, which is the AC's own
distinction:

* the barrier is released **only after test-observed ``B_presented``**. No
  duration and no timeout releases A — if B never got through, the arm would
  deadlock rather than pass slowly, and a deadlock is a failure that cannot be
  mistaken for a slow success;
* the CONTROL arm inverts it. A scheduler that awaited A's port call inline can
  never reach B, so B can never satisfy the prerequisite that releases A, and the
  control asserts exactly that — the mutation makes the arm unsatisfiable rather
  than merely slower.

``DELIVERY_TICK_S`` and ``ACP_WRITE_SETTLE_S`` bound B's progress in the AC's
wording. Here B's progress is bounded by ONE scheduler pass, which is the
strictly stronger statement: B does not merely finish within a tick, it finishes
within the same tick that found A blocked.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS
from test.app.test_acp_receiver_task import (  # reuse the doubles, not a second set
    FakeSession,
    FakeTransport,
    FixedClock,
    handle,
)

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.interrupt import SqliteInterruptStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.app.acp.interrupt_limiter import InterruptLimiter
from cli_agent_orchestrator.app.acp.receiver_task import (
    ReceiverDeliveryTask,
    ReceiverTaskRegistry,
    TaskOutcome,
)
from cli_agent_orchestrator.core.interrupt import (
    CallerPrincipal,
    CancelSettlement,
    InterruptAdmission,
    InterruptPreparation,
    PreparationKind,
    PrincipalOrigin,
    SettleKind,
    SubmitEnvelope,
    SubmitReceipt,
    Urgency,
)

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
BARRIER_POINTS = ("prepare", "submit", "await_cancel", "teardown")


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[ConnectionPool]:
    result, pool = migrate(tmp_path / "isolation.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    yield pool
    pool.close_all()


def _admit(store: SqliteInterruptStore, terminal: str, callback: str) -> None:
    outcome = store.admit_interrupt(
        InterruptAdmission(
            terminal_id=terminal,
            callback_id=callback,
            principal=CallerPrincipal(origin=PrincipalOrigin.TERMINAL, subject=terminal),
            envelope=SubmitEnvelope(callback_id=callback, body="work", urgency=Urgency.INTERRUPT),
            now=T0,
        )
    )
    assert outcome.admitted, outcome


def _task(
    store: SqliteInterruptStore,
    terminal: str,
    *,
    preparation: InterruptPreparation,
    session: FakeSession,
) -> tuple[ReceiverDeliveryTask, FakeTransport]:
    transport = FakeTransport(
        preparation, SubmitReceipt(accepted=True, write_flushed_at=T0, write_receipt_at=T0)
    )
    task = ReceiverDeliveryTask(
        terminal,
        store=store,
        transport=transport,
        session=session,
        clock=FixedClock(),
        lease_owner=f"tick-{terminal}",
    )
    return task, transport


def _b_side(store: SqliteInterruptStore) -> tuple[ReceiverDeliveryTask, FakeTransport]:
    return _task(
        store,
        "term-B",
        preparation=InterruptPreparation(kind=PreparationKind.IDLE),
        session=FakeSession(),
    )


def _a_side(
    store: SqliteInterruptStore, point: str
) -> tuple[ReceiverDeliveryTask, FakeTransport, FakeSession]:
    """A, configured so its run actually reaches ``point``."""
    if point in ("prepare", "submit"):
        preparation = InterruptPreparation(kind=PreparationKind.IDLE)
        session = FakeSession()
    elif point == "await_cancel":
        preparation = InterruptPreparation(
            kind=PreparationKind.CANCEL_REQUIRED, active_turn=handle()
        )
        session = FakeSession(settlement=CancelSettlement(kind=SettleKind.CANCELLED, settle_ms=4.0))
    else:  # teardown
        preparation = InterruptPreparation(
            kind=PreparationKind.CANCEL_REQUIRED, active_turn=handle()
        )
        session = FakeSession(
            settlement=CancelSettlement(kind=SettleKind.UNSETTLED), closes=False, group_dies=True
        )
    task, transport = _task(store, "term-A", preparation=preparation, session=session)
    return task, transport, session


# ==================================================== the four barrier arms


@pytest.mark.parametrize("point", BARRIER_POINTS)
def test_b_reaches_a_durable_presentation_while_a_is_blocked(
    pool: ConnectionPool, point: str
) -> None:
    """A blocks at ``point``; the REAL tick drives B to a durable receipt.

    A runs on its own thread and parks on an ``Event`` that only ``B_presented``
    sets. The scheduler then runs on the main thread and must get B all the way
    through — admission, claim, dispatch intent, write receipt — in ONE pass.
    """
    store = SqliteInterruptStore(pool, limiter=InterruptLimiter())
    _admit(store, "term-A", "cb-A")
    _admit(store, "term-B", "cb-B")

    a_blocked = threading.Event()
    b_presented = threading.Event()

    def barrier(where: str) -> None:
        if where != point:
            return
        a_blocked.set()
        # No timeout: the ONLY thing that releases A is B getting through. An
        # arm that released on elapsed time would pass against the very
        # head-of-line blocking it exists to detect.
        b_presented.wait()

    a_task, a_transport, a_session = _a_side(store, point)
    a_transport.barrier = barrier
    a_session.barrier = barrier
    b_task, b_transport = _b_side(store)

    registry = ReceiverTaskRegistry({"term-B": b_task})
    tick = _tick_with(registry)

    a_thread = threading.Thread(target=a_task.run_once, daemon=True)
    a_thread.start()
    assert a_blocked.wait(timeout=30), f"A never reached the {point} barrier"

    # ---- the scheduler runs while A is parked -----------------------------
    tick.signal_acp_receivers(_report())
    assert tick.acp_runs == 1, "the scheduler kept scheduling while A was blocked"
    assert len(b_transport.submits) == 1, "B wrote its one prompt while A was blocked"
    assert registry.reports[-1].outcome is TaskOutcome.DELIVERED

    state = store.read_state("term-B")
    assert state is not None and state.phase.value == "none", "B's receipt is DURABLE"

    b_presented.set()
    a_thread.join(timeout=30)
    assert not a_thread.is_alive(), "A resumed once B was through"


@pytest.mark.parametrize("point", BARRIER_POINTS)
def test_the_control_mutation_makes_b_unreachable(pool: ConnectionPool, point: str) -> None:
    """The CONTROL: a scheduler that awaits A's call inline can never reach B.

    The mutation is the smallest faithful one — the scheduler runs A's blocking
    task itself instead of signalling B — and the assertion is that B can never
    satisfy the prerequisite that would release A. Expressed as a deadlock the
    test detects with a bounded join rather than as a slow pass, because "B was
    late" and "B was never reached" are different failures and only the second is
    head-of-line blocking.
    """
    store = SqliteInterruptStore(pool, limiter=InterruptLimiter())
    _admit(store, "term-A", "cb-A")
    _admit(store, "term-B", "cb-B")

    b_presented = threading.Event()
    reached_b = threading.Event()

    def barrier(where: str) -> None:
        if where != point:
            return
        b_presented.wait(timeout=2.0)  # bounded, because this arm expects to TIME OUT

    a_task, a_transport, a_session = _a_side(store, point)
    a_transport.barrier = barrier
    a_session.barrier = barrier
    b_task, b_transport = _b_side(store)

    def inline_scheduler() -> None:
        # THE MUTATION: A's task is awaited inline, so B is only reached after A
        # returns — which, with A waiting on B, it cannot do.
        a_task.run_once()
        b_task.run_once()
        reached_b.set()

    thread = threading.Thread(target=inline_scheduler, daemon=True)
    thread.start()
    assert not reached_b.wait(
        timeout=1.0
    ), "the inline mutation reached B, so this arm proves nothing about isolation"
    assert b_transport.submits == [], "B must not have been served by the inline scheduler"
    b_presented.set()
    thread.join(timeout=30)


# ==================================================== restart and duplicates


def test_a_scheduler_restart_leaves_exactly_one_task_for_a_blocked_receiver(
    pool: ConnectionPool,
) -> None:
    """AC-S1.29's restart clause: one A task, however often the scheduler runs.

    The registry's re-entrancy guard is the in-process half of D6b(3)'s
    duplicate rule, and the persisted phase is the durable half. A scheduler that
    ticked again while A was blocked must NOT start a second A.
    """
    store = SqliteInterruptStore(pool, limiter=InterruptLimiter())
    _admit(store, "term-A", "cb-A")

    a_blocked = threading.Event()
    release = threading.Event()
    starts: list[int] = []

    def barrier(where: str) -> None:
        if where != "submit":
            return
        starts.append(1)
        a_blocked.set()
        release.wait()

    a_task, a_transport, a_session = _a_side(store, "submit")
    a_transport.barrier = barrier
    a_session.barrier = barrier
    registry = ReceiverTaskRegistry({"term-A": a_task})

    thread = threading.Thread(target=lambda: registry.signal("term-A"), daemon=True)
    thread.start()
    assert a_blocked.wait(timeout=30)
    assert registry.is_running("term-A")

    # Two more scheduler passes — a restart, and then an ordinary tick.
    first = _tick_with(registry)
    first.signal_acp_receivers(_report())
    second = _tick_with(registry)
    second.signal_acp_receivers(_report())

    # The count of barrier ENTRIES is the duplicate oracle: a second task would
    # have entered the barrier again. ``submits`` is appended after the barrier
    # returns, so while A is parked it is still empty by construction — asserting
    # over it here would test the double's ordering rather than the registry.
    assert starts == [1], "a second A task was started while the first was blocked"
    assert a_transport.submits == [], "A is still parked, so nothing has been written yet"

    release.set()
    thread.join(timeout=30)
    assert len(a_transport.submits) == 1, "exactly one A write survived the two extra passes"


def test_the_registry_binds_one_task_per_receiver(pool: ConnectionPool) -> None:
    """A second ``register`` REPLACES; it never adds a second runner."""
    store = SqliteInterruptStore(pool, limiter=InterruptLimiter())
    first, _ = _b_side(store)
    second, _ = _b_side(store)
    registry = ReceiverTaskRegistry()
    registry.register("term-B", first)
    registry.register("term-B", second)
    assert registry.receivers() == ("term-B",)


def test_the_scheduler_is_inert_with_no_acp_receivers() -> None:
    """AC-S1.1's shape for the scheduler half: native means no branch runs."""
    tick = _tick_with(None)
    tick.signal_acp_receivers(_report())
    assert tick.acp_runs == 0


def test_one_receivers_fault_does_not_stop_the_others(pool: ConnectionPool) -> None:
    """A fleet in which one agent's fault stops the others is a fleet with one agent."""
    store = SqliteInterruptStore(pool, limiter=InterruptLimiter())
    _admit(store, "term-B", "cb-B")

    class _Exploding:
        def run_once(self) -> object:
            raise RuntimeError("this receiver is broken")

    b_task, b_transport = _b_side(store)
    registry = ReceiverTaskRegistry({"term-A": _Exploding(), "term-B": b_task})  # type: ignore[dict-item]
    tick = _tick_with(registry)
    tick.signal_acp_receivers(_report())
    assert len(b_transport.submits) == 1, "B was served despite A raising"


# ---------------------------------------------------------------- helpers


def _report():
    from cli_agent_orchestrator.app.delivery.tick import TickReport

    return TickReport()


def _tick_with(registry: ReceiverTaskRegistry | None):
    """A ``DeliveryTick`` whose only live part is the ACP scheduler half.

    The queue-side collaborators are doubles that answer "nothing to do": these
    arms are about ``signal_acp_receivers``, and a real queue would add rows
    whose delivery has nothing to do with the property under test.
    """
    from cli_agent_orchestrator.app.delivery.tick import DeliveryTick

    class _Idle:
        def ready_receivers(self) -> tuple[str, ...]:
            return ()

        def reclaim(self, *, now):  # noqa: ANN001
            raise AssertionError("not reached")

        def open_digests(self) -> tuple[()]:
            return ()

    return DeliveryTick(
        store=_Idle(),  # type: ignore[arg-type]
        wake=None,  # type: ignore[arg-type]
        directory=None,  # type: ignore[arg-type]
        findings=None,
        clock=FixedClock(),
        receiver_tasks=registry,
    )


# ============================== AC-S1.27 against the INTEGRATED scheduler


_SCHEDULER_CRASH_POINTS = [
    "claimed",
    "prepared",
    "cancel_begun",
    "cancel_sent",
    "cancel_settled",
    "settled_to_prompt",
    "submitted",
    "completed",
]


@pytest.mark.parametrize("point", _SCHEDULER_CRASH_POINTS)
def test_a_crash_inside_a_receiver_never_stops_the_scheduler(
    pool: ConnectionPool, point: str
) -> None:
    """AC-S1.27, driven through the tick rather than through the task alone.

    The unit-level matrix proved the STORE survives a death at each point. This
    proves the SCHEDULER does: A dies mid-run, and B is still served in the same
    pass — which is the property that decides whether one broken agent takes the
    fleet with it, and it is invisible to a test that runs the task directly.
    """
    from cli_agent_orchestrator.app.acp.receiver_task import TaskStep

    store = SqliteInterruptStore(pool, limiter=InterruptLimiter())
    _admit(store, "term-A", "cb-A")
    _admit(store, "term-B", "cb-B")

    def fault(step: TaskStep) -> None:
        if step.value == point:
            raise RuntimeError(f"receiver A died at {point}")

    a_transport = FakeTransport(
        InterruptPreparation(kind=PreparationKind.CANCEL_REQUIRED, active_turn=handle()),
        SubmitReceipt(accepted=True, write_flushed_at=T0, write_receipt_at=T0),
    )
    a_task = ReceiverDeliveryTask(
        "term-A",
        store=store,
        transport=a_transport,
        session=FakeSession(),
        clock=FixedClock(),
        lease_owner="tick-A",
        fault=fault,
    )
    b_task, b_transport = _b_side(store)

    registry = ReceiverTaskRegistry({"term-A": a_task, "term-B": b_task})
    tick = _tick_with(registry)
    tick.signal_acp_receivers(_report())

    assert len(b_transport.submits) == 1, f"B was not served after A died at {point}"
    # ...and A's own reservation is intact rather than half-written, whatever it
    # was doing when it died.
    state = store.read_state("term-A")
    assert state is not None
    if state.phase.value == "cancelling":
        assert state.deadline is not None
    if state.phase.value in ("none", "prompting"):
        assert state.deadline is None


@pytest.mark.parametrize("point", _SCHEDULER_CRASH_POINTS)
def test_a_crash_then_a_later_tick_writes_no_second_prompt(
    pool: ConnectionPool, point: str
) -> None:
    """The restart oracle, through the scheduler.

    A dies at ``point``; a LATER tick signals the same receiver with a fresh
    task — which is what a restarted server does — and the total number of prompt
    writes across both is at most one. "Second I submit" is a named r18 mutant,
    and the scheduler is where a duplicate would actually be issued.
    """
    from cli_agent_orchestrator.app.acp.receiver_task import TaskStep

    store = SqliteInterruptStore(pool, limiter=InterruptLimiter())
    _admit(store, "term-A", "cb-A")

    def fault(step: TaskStep) -> None:
        if step.value == point:
            raise RuntimeError(f"died at {point}")

    first_transport = FakeTransport(
        InterruptPreparation(kind=PreparationKind.CANCEL_REQUIRED, active_turn=handle()),
        SubmitReceipt(accepted=True, write_flushed_at=T0, write_receipt_at=T0),
    )
    first = ReceiverDeliveryTask(
        "term-A",
        store=store,
        transport=first_transport,
        session=FakeSession(),
        clock=FixedClock(),
        lease_owner="tick-A",
        fault=fault,
    )
    _tick_with(ReceiverTaskRegistry({"term-A": first})).signal_acp_receivers(_report())

    second, second_transport, _ = _a_side(store, "await_cancel")
    _tick_with(ReceiverTaskRegistry({"term-A": second})).signal_acp_receivers(_report())

    total = len(first_transport.submits) + len(second_transport.submits)
    assert total <= 1, f"{total} prompt writes across a crash at {point} and a later tick"


def test_the_scheduler_performs_no_wire_or_process_call_itself() -> None:
    """D6b(3)'s static half: the scheduler claims, schedules and signals.

    ``signal_acp_receivers`` must not name a port call. AC-S1.29's four barriers
    prove it dynamically; this is the cheap check that says so at a glance, and
    it is the one that fails the moment somebody "just inlines" a prepare.
    """
    import ast
    import inspect

    from cli_agent_orchestrator.app.delivery.tick import DeliveryTick

    source = inspect.getsource(DeliveryTick.signal_acp_receivers)
    tree = ast.parse(source.strip())
    forbidden = {
        "prepare_interrupt",
        "submit",
        "cancel_if_current",
        "await_cancel",
        "terminate_process_group",
        "close",
    }
    reached = [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    ]
    assert not reached, f"the scheduler reaches a wire/process call: {reached}"
