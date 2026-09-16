"""AC-S1.22 (a)(b)(c), AC-S1.25 and AC-S1.26 — the interrupt aggregate.

Against a real SQLite file, for the reason ``test_queue_store.py`` gives: what is
being tested is the STATEMENTS.  A fake store would pass every assertion here
while the shipped claim quietly dropped ``urgency_rank`` from its ORDER BY, or
the shipped ``begin_cancel`` quietly took a second clock sample — and those two
are named r18/r9 mutants with no second line of defence.

Every mutant below is expressed as the SMALLEST faithful change: a patched SQL
string, a patched bound, a second concurrent call.  A mutant that rewrote the
method wholesale would prove only that a different method behaves differently.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS

import pytest

from cli_agent_orchestrator.adapters.store import interrupt as interrupt_module
from cli_agent_orchestrator.adapters.store.connection import ConnectionPool, render_timestamp
from cli_agent_orchestrator.adapters.store.interrupt import SqliteInterruptStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.app.acp.interrupt_limiter import InterruptLimiter
from cli_agent_orchestrator.core.interrupt import (
    WINDOW_LOST,
    ActiveTurnHandle,
    CallerPrincipal,
    CancelWindow,
    InterruptAdmission,
    InterruptFence,
    InterruptPhase,
    InterruptRefusal,
    PrincipalOrigin,
    SessionState,
    SubmitEnvelope,
    Urgency,
    urgency_rank,
)
from cli_agent_orchestrator.core.timing import (
    ACP_CANCEL_SETTLE_S,
    CANCEL_HOLD_MARGIN_S,
    INTERRUPT_BUDGET_N,
    INTERRUPT_MIN_GAP_S,
)

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
TERMINAL = "term-a"


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[ConnectionPool]:
    result, pool = migrate(tmp_path / "acp.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok, result
    assert pool is not None
    yield pool
    pool.close_all()


@pytest.fixture
def store(pool: ConnectionPool) -> SqliteInterruptStore:
    return SqliteInterruptStore(pool, limiter=InterruptLimiter())


def mcp(subject: str = "caller-1") -> CallerPrincipal:
    return CallerPrincipal(origin=PrincipalOrigin.TERMINAL, subject=subject, lifecycle_generation=3)


def admission(
    *,
    callback_id: str,
    now: datetime = T0,
    terminal_id: str = TERMINAL,
    principal: CallerPrincipal | None = None,
    force: bool = False,
    expire_after_s: int | None = None,
) -> InterruptAdmission:
    return InterruptAdmission(
        terminal_id=terminal_id,
        callback_id=callback_id,
        principal=principal if principal is not None else mcp(),
        envelope=SubmitEnvelope(
            callback_id=callback_id, body="stop and read this", urgency=Urgency.INTERRUPT
        ),
        now=now,
        observed_state=SessionState.ACTIVE,
        cut_candidate="cb-N",
        force=force,
        envelope_expire_after_s=expire_after_s,
    )


def _msg_count(pool: ConnectionPool) -> int:
    return int(pool.connection().execute("SELECT count(*) FROM delivery_msg").fetchone()[0])


def _ledger_count(pool: ConnectionPool) -> int:
    return int(pool.connection().execute("SELECT count(*) FROM interrupt_ledger").fetchone()[0])


# ============================================================ AC-S1.22 (a)


def test_a_second_interrupt_inside_the_gap_is_refused_with_no_row(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """(a) refused ``INTERRUPT_RATE_LIMITED`` at the edge, and NO ``delivery_msg`` row.

    The journal count is asserted, not the return value alone.  "A refused
    interrupt leaves a row" is AC-S1.22's first fails-if, and a refusal that
    cleaned up after itself would be indistinguishable from one that never wrote
    — until a crash landed between the write and the cleanup.
    """
    first = store.admit_interrupt(admission(callback_id="cb-1"))
    assert first.admitted
    # Free the reservation so the GAP is what refuses, not the mutex.
    _force_phase_none(pool)
    before = _msg_count(pool)

    inside_gap = T0 + timedelta(seconds=INTERRUPT_MIN_GAP_S - 1)
    second = store.admit_interrupt(admission(callback_id="cb-2", now=inside_gap))
    assert second.refused is InterruptRefusal.RATE_LIMITED
    assert _msg_count(pool) == before, "a refused interrupt may leave no queue row"
    assert _ledger_count(pool) == 1, "a refused interrupt may not be charged"


def test_past_the_gap_the_same_terminal_admits_again(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """The control arm: the gap must be a GAP, not a permanent refusal."""
    assert store.admit_interrupt(admission(callback_id="cb-1")).admitted
    _force_phase_none(pool)
    later = T0 + timedelta(seconds=INTERRUPT_MIN_GAP_S + 1)
    assert store.admit_interrupt(admission(callback_id="cb-2", now=later)).admitted


def test_the_gap_is_per_terminal_not_per_principal(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """A DIFFERENT principal inside the gap is still refused on the same terminal.

    AC-S1.22's fails-if names the inverse — "a per-terminal-only gap lets a
    rotating loop through" — and this is the other half of the same pair: the
    gap belongs to the terminal, so rotating the CALLER does not refresh it.
    """
    assert store.admit_interrupt(admission(callback_id="cb-1")).admitted
    _force_phase_none(pool)
    inside_gap = T0 + timedelta(seconds=1)
    other = store.admit_interrupt(
        admission(callback_id="cb-2", now=inside_gap, principal=mcp("caller-2"))
    )
    assert other.refused is InterruptRefusal.RATE_LIMITED


# ============================================================ AC-S1.22 (b)


def test_the_budget_refuses_the_n_plus_first_across_distinct_terminals(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """(b) ``INTERRUPT_BUDGET_N + 1`` from ONE principal across DISTINCT terminals.

    Distinct terminals on purpose: the per-terminal gap must not be what refuses,
    or the arm would prove nothing about the budget.  Each admission is on a
    fresh terminal and the reservation is released between them, so the only
    bound left standing is the per-principal one.
    """
    now = T0
    for index in range(INTERRUPT_BUDGET_N):
        outcome = store.admit_interrupt(
            admission(callback_id=f"cb-{index}", now=now, terminal_id=f"term-{index}")
        )
        assert outcome.admitted, f"admission {index} should pass the budget"
        assert outcome.quota is not None
        assert outcome.quota.remaining_budget == INTERRUPT_BUDGET_N - index
        now += timedelta(seconds=1)

    before = _msg_count(pool)
    refused = store.admit_interrupt(
        admission(callback_id="cb-over", now=now, terminal_id="term-over")
    )
    assert refused.refused is InterruptRefusal.BUDGET_EXHAUSTED
    assert _msg_count(pool) == before
    assert _ledger_count(pool) == INTERRUPT_BUDGET_N


def test_another_principals_budget_is_untouched(store: SqliteInterruptStore) -> None:
    """(d)'s cross-surface property, asserted here because it is the same ledger.

    An MCP principal's budget is consumed by its own calls only.  If the budget
    key leaked the terminal or the surface, exhausting one caller would refuse
    every other caller on the box.
    """
    now = T0
    for index in range(INTERRUPT_BUDGET_N):
        assert store.admit_interrupt(
            admission(callback_id=f"cb-{index}", now=now, terminal_id=f"term-{index}")
        ).admitted
        now += timedelta(seconds=1)
    fresh = store.admit_interrupt(
        admission(
            callback_id="cb-other",
            now=now,
            terminal_id="term-other",
            principal=mcp("caller-2"),
        )
    )
    assert fresh.admitted


def test_a_viewer_and_an_mcp_caller_are_different_principals(store: SqliteInterruptStore) -> None:
    """The budget key is ``(origin, subject)``; a shared subject string must not merge them."""
    terminal_principal = CallerPrincipal(origin=PrincipalOrigin.TERMINAL, subject="same")
    viewer_principal = CallerPrincipal(origin=PrincipalOrigin.VIEWER, subject="same")
    assert terminal_principal.budget_key != viewer_principal.budget_key


def test_reattaching_never_resets_quota() -> None:
    """(h): ``attach_session_id`` is AUDIT metadata and may not enter the budget key."""
    keys = {
        CallerPrincipal(
            origin=PrincipalOrigin.VIEWER, subject="v1", attach_session_id=f"attach-{index}"
        ).budget_key
        for index in range(5)
    }
    assert len(keys) == 1


def test_a_lifecycle_generation_bump_never_resets_quota() -> None:
    """A respawned terminal is the same principal spending the same budget."""
    keys = {
        CallerPrincipal(
            origin=PrincipalOrigin.TERMINAL, subject="t1", lifecycle_generation=generation
        ).budget_key
        for generation in range(4)
    }
    assert len(keys) == 1


def test_the_window_survives_a_restart(tmp_path: Path) -> None:
    """(e): the ledger is durable, so a restart neither resets nor extends a window.

    Two independent pools over one file, which is what a ``cao-server`` bounce
    actually is: the process goes, the file stays.  An in-process counter would
    pass every other budget test in this file and fail only this one.
    """
    db = tmp_path / "restart.db"
    result, first_pool = migrate(db, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and first_pool is not None
    first = SqliteInterruptStore(first_pool, limiter=InterruptLimiter())
    now = T0
    for index in range(INTERRUPT_BUDGET_N):
        assert first.admit_interrupt(
            admission(callback_id=f"cb-{index}", now=now, terminal_id=f"term-{index}")
        ).admitted
        now += timedelta(seconds=1)
    first_pool.close_all()

    _, second_pool = migrate(db, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert second_pool is not None
    second = SqliteInterruptStore(second_pool, limiter=InterruptLimiter())
    refused = second.admit_interrupt(
        admission(callback_id="cb-after", now=now, terminal_id="term-after")
    )
    second_pool.close_all()
    assert refused.refused is InterruptRefusal.BUDGET_EXHAUSTED


# ============================================================ AC-S1.22 (c)


def test_two_simultaneous_admissions_at_the_budget_edge_leave_exactly_one(
    tmp_path: Path,
) -> None:
    """(c) the ``BEGIN IMMEDIATE`` authority: exactly one of two concurrent calls wins.

    Real threads on real connections, because the property under test is SQLite's
    write lock.  Two calls are issued at the last remaining budget slot against
    two different terminals (so the per-terminal gap cannot be what refuses one
    of them), and the oracle is the LEDGER: exactly one row is charged.
    """
    db = tmp_path / "race.db"
    result, pool = migrate(db, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    store = SqliteInterruptStore(pool, limiter=InterruptLimiter())
    now = T0
    for index in range(INTERRUPT_BUDGET_N - 1):
        assert store.admit_interrupt(
            admission(callback_id=f"cb-{index}", now=now, terminal_id=f"term-{index}")
        ).admitted
        now += timedelta(seconds=1)

    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def attempt(tag: str) -> None:
        local_pool = ConnectionPool(db, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
        local = SqliteInterruptStore(local_pool, limiter=InterruptLimiter())
        barrier.wait(timeout=10)
        outcome = local.admit_interrupt(
            admission(callback_id=f"cb-{tag}", now=now, terminal_id=f"term-{tag}")
        )
        with lock:
            outcomes.append(outcome)
        local_pool.close_all()

    threads = [threading.Thread(target=attempt, args=(tag,)) for tag in ("x", "y")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    admitted = [o for o in outcomes if getattr(o, "admitted", False)]
    charged = _ledger_count(pool)
    pool.close_all()
    assert len(outcomes) == 2
    assert len(admitted) == 1, f"exactly one may win at the edge, got {outcomes}"
    assert charged == INTERRUPT_BUDGET_N


def test_a_second_interrupt_while_one_is_in_progress_is_refused_free(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """The reservation CAS refuses BEFORE quota is consulted (r14, review r13 N2).

    Asserted by the ledger, which is the only place a charge is visible: the
    second call finds ``pending`` and leaves the ledger at one row.
    """
    assert store.admit_interrupt(admission(callback_id="cb-1")).admitted
    second = store.admit_interrupt(
        admission(callback_id="cb-2", now=T0 + timedelta(seconds=INTERRUPT_MIN_GAP_S + 5))
    )
    assert second.refused is InterruptRefusal.IN_PROGRESS
    assert second.quota is None, "a reservation refusal reports no quota because none was read"
    assert _ledger_count(pool) == 1


def test_force_waives_the_quota_bounds_but_never_the_reservation(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """(f): ``force`` proceeds through (a)/(b) and is still refused IN_PROGRESS."""
    assert store.admit_interrupt(admission(callback_id="cb-1")).admitted
    _force_phase_none(pool)
    inside_gap = T0 + timedelta(seconds=1)
    forced = store.admit_interrupt(admission(callback_id="cb-2", now=inside_gap, force=True))
    assert forced.admitted, "force waives the per-terminal gap"

    blocked = store.admit_interrupt(admission(callback_id="cb-3", now=inside_gap, force=True))
    assert blocked.refused is InterruptRefusal.IN_PROGRESS, "force never waives the mutex"


def test_a_window_too_short_for_cancel_plus_margin_is_refused_pre_admission(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """(i): no row, no quota, when I could not survive its own cancel window."""
    before_ledger = _ledger_count(pool)
    outcome = store.admit_interrupt(
        admission(
            callback_id="cb-short",
            expire_after_s=ACP_CANCEL_SETTLE_S + CANCEL_HOLD_MARGIN_S - 5,
        )
    )
    assert outcome.refused is InterruptRefusal.WINDOW_TOO_SHORT
    assert _msg_count(pool) == 0
    assert _ledger_count(pool) == before_ledger


# ============================================================ AC-S1.25


def _enqueue_normal(pool: ConnectionPool, msg_id: str, *, available_at: datetime) -> None:
    """A plain ``normal`` row, written the way the ordinary enqueue writes one."""
    pool.connection().execute(
        "INSERT INTO delivery_msg (msg_id, idempotency_key, receiver_id, kind, payload, state, "
        "mode, available_at, dead_by, urgency, urgency_rank, created_at) "
        "VALUES (?,?,?,?,?,'ready','live',?,?,'normal',1,?)",
        (
            msg_id,
            f"key-{msg_id}",
            TERMINAL,
            "callback",
            f"body-{msg_id}",
            render_timestamp(available_at),
            render_timestamp(available_at + timedelta(hours=1)),
            render_timestamp(available_at),
        ),
    )


def test_the_claim_returns_the_interrupt_before_an_older_normal_row(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """Two-row mutation: N is OLDER than I, and I is still claimed first."""
    _enqueue_normal(pool, "msg-N", available_at=T0 - timedelta(minutes=5))
    assert store.admit_interrupt(admission(callback_id="cb-I")).admitted
    _force_phase_none(pool)  # claim from `none`, so the ORDER BY is what decides

    claimed = store.claim_next(TERMINAL, now=T0, lease_owner="tick")
    assert claimed is not None
    assert claimed.envelope.urgency is Urgency.INTERRUPT
    assert claimed.fence.msg_id != "msg-N"


def test_mutant_dropping_urgency_rank_from_the_order_by_selects_the_normal_row(
    store: SqliteInterruptStore, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTANT: remove ``urgency_rank`` from the claim's ORDER BY -> RED with N selected.

    Patched as a STRING substitution on the executed SQL so nothing else about
    the statement changes: AC-S1.25's fails-if is "ordering lives in prose or in
    the driver instead of the claim SQL", and this is the mutant that proves it
    lives in the SQL.
    """
    _enqueue_normal(pool, "msg-N", available_at=T0 - timedelta(minutes=5))
    assert store.admit_interrupt(admission(callback_id="cb-I")).admitted
    _force_phase_none(pool)

    conn = pool.connection()

    class _OrderByMutant:
        """The same connection with one clause struck from every statement.

        ``sqlite3.Connection.execute`` is read-only, so the mutation is applied
        by wrapping the connection the store asks the pool for.  Everything else
        about the statement — the WHERE, the LIMIT, the parameters — is
        untouched, which is what makes this a mutant rather than a rewrite.
        """

        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            return self._inner.execute(
                sql.replace("ORDER BY urgency_rank, available_at", "ORDER BY available_at"),
                *args,
            )

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    monkeypatch.setattr(pool, "connection", lambda: _OrderByMutant(conn))
    claimed = store.claim_next(TERMINAL, now=T0, lease_owner="tick")
    assert claimed is not None
    assert claimed.fence.msg_id == "msg-N", "the mutant must be RED"


def test_the_claim_leases_exactly_one_row_and_leaves_the_rest_ready(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """Isolation arm: I plus three normal rows; only I is leased, no attempt spent.

    "The 64-row batch claim never runs for an ACP receiver" (r14, review r13 B3)
    is what this asserts from the outside: three rows stay ``ready`` with
    ``attempts`` at zero, so they are all still there — and still unspent — when
    the interrupt finishes.
    """
    for index in range(3):
        _enqueue_normal(pool, f"msg-N{index}", available_at=T0 - timedelta(minutes=index + 1))
    assert store.admit_interrupt(admission(callback_id="cb-I")).admitted
    _force_phase_none(pool)

    claimed = store.claim_next(TERMINAL, now=T0, lease_owner="tick")
    assert claimed is not None
    rows = (
        pool.connection()
        .execute("SELECT msg_id, state, attempts FROM delivery_msg ORDER BY msg_id")
        .fetchall()
    )
    leased = [r["msg_id"] for r in rows if r["state"] == "leased"]
    ready = [r["msg_id"] for r in rows if r["state"] == "ready"]
    assert len(leased) == 1
    assert len(ready) == 3
    assert all(int(r["attempts"]) == 0 for r in rows), "no attempt is spent by an isolated claim"


@pytest.mark.parametrize(
    "phase", [InterruptPhase.CANCELLING, InterruptPhase.PROMPTING, InterruptPhase.RECOVERING]
)
def test_a_receiver_mid_interrupt_is_served_no_row(
    store: SqliteInterruptStore, pool: ConnectionPool, phase: InterruptPhase
) -> None:
    """Mid-interrupt phase arm (r14, review r13 B4): ``claim_next`` leases NOTHING.

    All three mid-interrupt phases, not just ``cancelling``: the selection rule
    is total over the phase set, and "any phase other than pending/recovering
    selects the next row" is the mutant the r13 blocker named.
    """
    _enqueue_normal(pool, "msg-N", available_at=T0 - timedelta(minutes=5))
    _set_phase(pool, phase)
    assert store.claim_next(TERMINAL, now=T0, lease_owner="tick") is None
    row = (
        pool.connection()
        .execute("SELECT state FROM delivery_msg WHERE msg_id = 'msg-N'")
        .fetchone()
    )
    assert row["state"] == "ready", "no session/prompt may be written mid-interrupt"


def test_urgency_rank_puts_interrupt_first_by_rank_not_by_spelling() -> None:
    """The rank is a NUMBER.  ``"interrupt" < "normal"`` is a coincidence of
    spelling and would invert silently if either value were renamed."""
    assert urgency_rank(Urgency.INTERRUPT) < urgency_rank(Urgency.NORMAL)
    assert urgency_rank(None) == urgency_rank(Urgency.NORMAL)
    assert urgency_rank("something-else") == urgency_rank(Urgency.NORMAL)


# ============================================================ AC-S1.26


def _claimed_fence(store: SqliteInterruptStore, pool: ConnectionPool) -> InterruptFence:
    assert store.admit_interrupt(admission(callback_id="cb-I")).admitted
    claimed = store.claim_next(TERMINAL, now=T0, lease_owner="tick")
    assert claimed is not None
    return claimed.fence


def _handle() -> ActiveTurnHandle:
    return ActiveTurnHandle(
        terminal_id=TERMINAL,
        lifecycle_generation=7,
        session_id="sess-1",
        acp_request_id="req-42",
        callback_id="cb-N",
        turn_seq=3,
    )


def test_begin_cancel_persists_exactly_the_two_computed_instants(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """(a): one transaction persists ``deadline`` and I's ``lease_until``, and
    returns identical values.

    The arithmetic is asserted against the frozen constants, not against whatever
    the store happened to compute: ``deadline = now + ACP_CANCEL_SETTLE_S`` and
    ``lease_until = deadline + CANCEL_HOLD_MARGIN_S``.  Everything downstream
    consumes the RETURNED values, which is why identity between what was returned
    and what was stored is the property under test.
    """
    fence = _claimed_fence(store, pool)
    window = store.begin_cancel(fence, _handle(), T0)
    assert isinstance(window, CancelWindow)
    assert window.deadline == T0 + timedelta(seconds=ACP_CANCEL_SETTLE_S)
    assert window.lease_until == window.deadline + timedelta(seconds=CANCEL_HOLD_MARGIN_S)

    state = store.read_state(TERMINAL)
    assert state is not None
    assert state.phase is InterruptPhase.CANCELLING
    assert state.deadline == window.deadline, "the stored deadline IS the returned one"
    row = (
        pool.connection()
        .execute("SELECT lease_expires_at FROM delivery_msg WHERE msg_id = ?", (fence.msg_id,))
        .fetchone()
    )
    assert row["lease_expires_at"] == render_timestamp(window.lease_until)


def test_the_two_instants_are_distinct(store: SqliteInterruptStore, pool: ConnectionPool) -> None:
    """MUTANT-adjacent: ``deadline == lease_until`` is a named r18 mutant.

    ``CancelWindow`` refuses to be constructed that way at all, so the mutant
    cannot even be expressed as a value — which is the strongest form of the
    check available.
    """
    fence = _claimed_fence(store, pool)
    window = store.begin_cancel(fence, _handle(), T0)
    assert isinstance(window, CancelWindow)
    assert window.lease_until > window.deadline
    with pytest.raises(ValueError, match="strictly later"):
        CancelWindow(deadline=window.deadline, lease_until=window.deadline)


def test_begin_cancel_takes_the_caller_s_clock_sample_and_never_its_own(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """MUTANT: "store resamples clock".  The instant is the one passed IN.

    A store that resampled would produce a deadline near the real wall clock;
    the injected sample is a fixed 2026-09-16 instant, so the two are far apart
    and the assertion cannot pass by coincidence.
    """
    fence = _claimed_fence(store, pool)
    # Inside I's lifetime (so the window is genuinely grantable) and hours away
    # from the real wall clock, so a resampling store could not produce this
    # answer by coincidence.
    injected = T0 + timedelta(seconds=5)
    window = store.begin_cancel(fence, _handle(), injected)
    assert isinstance(window, CancelWindow)
    assert window.deadline == injected + timedelta(seconds=ACP_CANCEL_SETTLE_S)
    assert abs((window.deadline - datetime.now(UTC)).total_seconds()) > 600


def test_begin_cancel_refuses_a_stale_fence(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """(d): lost fence -> ``WINDOW_LOST``, atomically, with nothing written."""
    fence = _claimed_fence(store, pool)
    stale = InterruptFence(
        terminal_id=fence.terminal_id,
        msg_id=fence.msg_id,
        claim_id=fence.claim_id + 1,
        owner=fence.owner,
        generation=fence.generation,
    )
    assert store.begin_cancel(stale, _handle(), T0) is WINDOW_LOST
    state = store.read_state(TERMINAL)
    assert state is not None
    assert state.phase is InterruptPhase.PENDING, "a lost window changes nothing"
    assert state.deadline is None


def test_begin_cancel_refuses_a_stale_generation(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    fence = _claimed_fence(store, pool)
    stale = InterruptFence(
        terminal_id=fence.terminal_id,
        msg_id=fence.msg_id,
        claim_id=fence.claim_id,
        owner=fence.owner,
        generation=fence.generation + 5,
    )
    assert store.begin_cancel(stale, _handle(), T0) is WINDOW_LOST


def test_a_caller_set_expiry_before_lease_until_never_reaches_begin_cancel(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """(c): the caller's expiry is refused PRE-admission, never extended.

    r3's B2 bug was a caller deadline swallowed by busy credit.  The rule here is
    the same rule stated earlier in the lifecycle: if the caller's own box cannot
    hold cancel + margin, the interrupt is refused before a row exists.
    """
    outcome = store.admit_interrupt(
        admission(callback_id="cb-boxed", expire_after_s=ACP_CANCEL_SETTLE_S)
    )
    assert outcome.refused is InterruptRefusal.WINDOW_TOO_SHORT


def test_the_active_turn_audit_fields_are_copied_and_carry_no_queue_ids(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """A2.9(iii): H names a TURN.  Its audit fields land; no queue id travels with it."""
    fence = _claimed_fence(store, pool)
    handle = _handle()
    assert isinstance(store.begin_cancel(fence, handle, T0), CancelWindow)
    state = store.read_state(TERMINAL)
    assert state is not None
    assert state.active_turn_session_id == handle.session_id
    assert state.active_turn_request_id == handle.acp_request_id
    assert state.active_turn_seq == handle.turn_seq
    assert state.cut_callback_id == handle.callback_id
    for forbidden in ("msg_id", "claim_id", "lease_owner", "lease_expires_at"):
        assert not hasattr(handle, forbidden)


def test_race_lost_clears_the_consumed_deadline(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """(e): the handle moved before bytes; re-prepare from ``pending`` with no residue.

    The cleared deadline matters more than the phase: a retained one would let a
    later restart read a cancel window belonging to a cancel nobody issued.
    """
    fence = _claimed_fence(store, pool)
    window = store.begin_cancel(fence, _handle(), T0)
    assert isinstance(window, CancelWindow)
    state = store.read_state(TERMINAL)
    assert state is not None
    assert store.race_lost(
        InterruptFence(
            terminal_id=fence.terminal_id,
            msg_id=fence.msg_id,
            claim_id=fence.claim_id,
            owner=fence.owner,
            generation=state.generation,
        )
    )
    after = store.read_state(TERMINAL)
    assert after is not None
    assert after.phase is InterruptPhase.PENDING
    assert after.deadline is None
    assert after.active_turn_session_id is None


# ============================================================ helpers


def _force_phase_none(pool: ConnectionPool) -> None:
    """Release the reservation WITHOUT going through a transition.

    A test aid, and deliberately blunt: these arms are about the LIMITER and the
    CLAIM, and driving a full settle just to free the mutex would couple every
    budget assertion to the cancel path.
    """
    pool.connection().execute(
        "UPDATE interrupt_state SET phase = 'none', deadline = NULL",
    )


def _set_phase(pool: ConnectionPool, phase: InterruptPhase) -> None:
    deadline = render_timestamp(T0 + timedelta(seconds=ACP_CANCEL_SETTLE_S))
    pool.connection().execute(
        "INSERT INTO interrupt_state (terminal_id, phase, generation, deadline) VALUES (?,?,1,?) "
        "ON CONFLICT(terminal_id) DO UPDATE SET phase = excluded.phase, deadline = excluded.deadline",
        (
            TERMINAL,
            phase.value,
            deadline if phase is InterruptPhase.CANCELLING else None,
        ),
    )
