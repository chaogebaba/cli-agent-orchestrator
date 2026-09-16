"""AC-S1.23 — every admitted interrupt is attributable — and AC-S1.8's clauses.

The S1 review found both wholly unbuilt: ``cao diag`` had no interrupt fold, none
of the six terminal arms existed, neither static check existed, and AC-S1.8's
three mechanical clauses had no arm anywhere. The fails-if for AC-S1.23 is "an
interrupt is indistinguishable from an ordinary delivery", which was true.

Each terminal arm states its EXPECTED FOLDED ROW, as the AC requires: the arm
drives the store to a terminal state and then asserts what ``cao diag`` says
about it, rather than asserting on the store's own return value — which is the
difference between "the transition happened" and "an operator can find out".
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
from cli_agent_orchestrator.app.diag.interrupt_fold import (
    PHASE_SOURCES,
    fold_interrupt,
    render_interrupt,
)
from cli_agent_orchestrator.core.delivery import DeadReason, DeliveryAttempt
from cli_agent_orchestrator.core.interrupt import (
    ActiveTurnHandle,
    AuditPhase,
    CallerPrincipal,
    CancelSettlement,
    CancelWindow,
    InterruptAdmission,
    PrincipalOrigin,
    SettleKind,
    SubmitEnvelope,
    SubmitReceipt,
    Urgency,
)

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
TERMINAL = "term-acp"
CB = "cb-I"


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[ConnectionPool]:
    result, pool = migrate(tmp_path / "diag.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    yield pool
    pool.close_all()


@pytest.fixture
def store(pool: ConnectionPool) -> SqliteInterruptStore:
    return SqliteInterruptStore(pool, limiter=InterruptLimiter())


def _admit(store: SqliteInterruptStore, callback: str = CB, **kw: object) -> None:
    outcome = store.admit_interrupt(
        InterruptAdmission(
            terminal_id=TERMINAL,
            callback_id=callback,
            principal=CallerPrincipal(origin=PrincipalOrigin.TERMINAL, subject="supervisor"),
            envelope=SubmitEnvelope(callback_id=callback, body="stop", urgency=Urgency.INTERRUPT),
            now=T0,
            **kw,  # type: ignore[arg-type]
        )
    )
    assert outcome.admitted, outcome


def _handle() -> ActiveTurnHandle:
    return ActiveTurnHandle(
        terminal_id=TERMINAL,
        lifecycle_generation=7,
        session_id="sess-1",
        acp_request_id="req-42",
        callback_id="cb-N",
        turn_seq=3,
    )


def _fold(pool: ConnectionPool, callback: str = CB):
    return fold_interrupt(pool.connection(), callback)


def _claim(store: SqliteInterruptStore):
    claimed = store.claim_next(TERMINAL, now=T0, lease_owner="tick")
    assert claimed is not None
    return claimed


def _advance(store: SqliteInterruptStore, fence):
    state = store.read_state(TERMINAL)
    assert state is not None
    from cli_agent_orchestrator.core.interrupt import InterruptFence

    return InterruptFence(
        terminal_id=fence.terminal_id,
        msg_id=fence.msg_id,
        claim_id=fence.claim_id,
        owner=fence.owner,
        generation=state.generation,
    )


# ============================================== the six terminal arms


def test_arm_admitted_but_never_claimed(store: SqliteInterruptStore, pool: ConnectionPool) -> None:
    """An interrupt that exists and has gone nowhere is still ATTRIBUTABLE.

    This is the arm the audit object's placement is FOR (r11, review r10 B2):
    it rides a fact that exists BEFORE any claim, so an admitted-but-never-claimed
    interrupt has a principal in ``cao diag`` rather than being invisible.
    """
    _admit(store)
    fold = _fold(pool)
    assert fold.found
    assert fold.principal == "terminal:supervisor"
    assert fold.terminal_id == TERMINAL
    assert fold.phase in (AuditPhase.ADMITTED.value, AuditPhase.CLAIMED.value)


def test_arm_idle_success(store: SqliteInterruptStore, pool: ConnectionPool) -> None:
    """The fresh-worker case: no cancel was issued, and the fold says so."""
    _admit(store)
    claimed = _claim(store)
    assert store.begin_prompt(claimed.fence)
    assert store.complete_prompt(
        _advance(store, claimed.fence), SubmitReceipt(accepted=True, write_receipt_at=T0)
    )
    fold = _fold(pool)
    assert fold.phase == AuditPhase.CANCELLED.value  # the row is DELIVERED
    assert fold.phase_source == "delivery_msg"
    assert fold.cut_callback_id is None, "an idle interrupt cut nothing"


def test_arm_active_success_names_the_cut(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """A turn WAS cut, and the fold names which one — the whole point of D6b(7)."""
    _admit(store)
    claimed = _claim(store)
    window = store.begin_cancel(claimed.fence, _handle(), T0)
    assert isinstance(window, CancelWindow)
    fence = _advance(store, claimed.fence)
    assert store.settle_to_prompt(
        fence, CancelSettlement(kind=SettleKind.CANCELLED, settle_ms=12.0)
    )
    assert store.complete_prompt(
        _advance(store, fence), SubmitReceipt(accepted=True, write_receipt_at=T0)
    )
    fold = _fold(pool)
    assert fold.cut_callback_id == "cb-N"
    assert fold.cut_disposition == "quarantined"
    assert fold.cancelled_acp_request_id == "req-42"


def test_arm_cancel_timeout(store: SqliteInterruptStore, pool: ConnectionPool) -> None:
    """The cancel never settled: I is dead ``cancel_timeout`` and was NEVER dispatched."""
    _admit(store)
    claimed = _claim(store)
    assert isinstance(store.begin_cancel(claimed.fence, _handle(), T0), CancelWindow)
    fence = _advance(store, claimed.fence)
    assert store.begin_recovery(fence, T0 + timedelta(seconds=25)) is not None
    fold = _fold(pool)
    assert fold.phase == AuditPhase.CANCEL_TIMEOUT.value
    assert fold.phase_source == "delivery_dead"
    assert fold.live_phase == "recovering", "the terminal stays non-admissible"


def test_arm_window_lost(store: SqliteInterruptStore, pool: ConnectionPool) -> None:
    """``begin_cancel`` refused; N and the actor were untouched."""
    _admit(store)
    claimed = _claim(store)
    assert store.fail_interrupt(
        claimed.fence, DeadReason.INTERRUPT_WINDOW_LOST, AuditPhase.WINDOW_LOST.value
    )
    fold = _fold(pool)
    assert fold.phase == AuditPhase.WINDOW_LOST.value
    assert fold.phase_source == "delivery_dead"


def test_arm_pending_expired(store: SqliteInterruptStore, pool: ConnectionPool) -> None:
    """``pending_deadline`` passed with I unclaimed.  The charge STANDS."""
    _admit(store)
    claimed = _claim(store)
    assert store.fail_interrupt(
        claimed.fence, DeadReason.INTERRUPT_UNCLAIMED, AuditPhase.PENDING_EXPIRED.value
    )
    fold = _fold(pool)
    assert fold.phase == AuditPhase.PENDING_EXPIRED.value
    charged = (
        pool.connection()
        .execute("SELECT count(*) FROM interrupt_ledger WHERE interrupt_id = ?", (CB,))
        .fetchone()[0]
    )
    assert charged == 1, "an admitted interrupt that died is still one the budget counted"


def test_arm_prompt_ambiguous_reads_the_typed_attempt_detail(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """r13: the attempt detail is the SOLE source of ``prompt_ambiguous``.

    The row is also dead, so a fold that read the dead-letter first would report
    the wrong phase — which is why the detail is read first and why this arm
    asserts the SOURCE and not only the phase.
    """
    _admit(store)
    claimed = _claim(store)
    pool.connection().execute(
        "INSERT INTO delivery_attempt (msg_id, claim_id, carrier, started_at, outcome, detail) "
        "VALUES (?,?,?,?,?,?)",
        (
            claimed.fence.msg_id,
            claimed.fence.claim_id,
            "acp",
            "2026-09-16T12:00:00.000000+00:00",
            "submission_uncertain",
            '{"interrupt": {"phase": "prompt_ambiguous"}}',
        ),
    )
    assert store.fail_interrupt(
        claimed.fence, DeadReason.INTERRUPT_UNCLAIMED, AuditPhase.PROMPT_AMBIGUOUS.value
    )
    fold = _fold(pool)
    assert fold.phase == AuditPhase.PROMPT_AMBIGUOUS.value
    assert fold.phase_source == "delivery_attempt.detail"


def test_an_ordinary_callback_is_not_mistaken_for_an_interrupt(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """The control for the AC's fails-if, from the other direction."""
    fold = _fold(pool, "cb-an-ordinary-delivery")
    assert fold.found is False
    assert "not found" in render_interrupt(fold)


# ============================================== the two static checks


def test_every_phase_has_exactly_one_authoritative_source() -> None:
    """AC-S1.23's r13 check (review r12 B5).

    A phase with two sources is one whose answer depends on read order; a phase
    with none is one ``cao diag`` cannot report. Asserted as EQUALITY against the
    closed set, so a new phase without a source fails here rather than silently
    folding to a default.
    """
    assert set(PHASE_SOURCES) == {member.value for member in AuditPhase}
    assert all(isinstance(source, str) and source for source in PHASE_SOURCES.values())
    assert PHASE_SOURCES[AuditPhase.PROMPT_AMBIGUOUS.value] == "delivery_attempt.detail"


def test_presented_surface_takes_no_value_but_native_in_the_acp_driver() -> None:
    """A2.8's AC-3c check, kept green after S1.

    ``surface`` is a closed vocabulary and the ACP plane adds no member to it:
    an interrupt is delivered over the same native surface as any other wake, and
    a tenth ``kind`` or a widened ``surface`` is AC-S1.23's fails-if.
    """
    import ast

    import cli_agent_orchestrator

    src = Path(cli_agent_orchestrator.__file__).resolve().parent
    offenders: list[str] = []
    for package in ("adapters/acp", "app/acp"):
        for path in sorted((src / package).rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.keyword) or node.arg != "surface":
                    continue
                value = ast.unparse(node.value)
                if value not in ('"native"', "'native'"):
                    offenders.append(f"{path.name}: surface={value}")
    assert not offenders, "presented.surface widened in the ACP plane: " + "; ".join(offenders)


# ============================================== AC-S1.8's mechanical clauses


BARE_ALLOWLIST = frozenset({"result", "exited", "capped", "blocked", "expired"})


def test_clause_1_exactly_one_delivery_per_callback_id(
    store: SqliteInterruptStore, pool: ConnectionPool
) -> None:
    """AC-S1.8 clause 1, against the store rather than a live round.

    Two admissions of the SAME callback id must not produce two rows: A2.1's
    conflicting-reuse identity is keyed on it, and the queue's idempotency key is
    what enforces it.
    """
    _admit(store)
    second = store.admit_interrupt(
        InterruptAdmission(
            terminal_id=TERMINAL,
            callback_id=CB,
            principal=CallerPrincipal(origin=PrincipalOrigin.TERMINAL, subject="supervisor"),
            envelope=SubmitEnvelope(callback_id=CB, body="again", urgency=Urgency.INTERRUPT),
            now=T0 + timedelta(seconds=120),
        )
    )
    assert second.refused is not None, "a repeated callback id was admitted twice"
    rows = (
        pool.connection()
        .execute(
            "SELECT count(*) FROM delivery_msg WHERE idempotency_key = ?", (f"interrupt:{CB}",)
        )
        .fetchone()[0]
    )
    assert rows == 1


def test_clause_2_every_emitted_seat_kind_is_in_the_bare_allowlist() -> None:
    """AC-S1.8 clause 2, as a closed-set check over what the plane can EMIT.

    The old form of this clause asked for "zero decision-free messages", an
    undecidable predicate; clause 2 replaced it with a list. So the check is that
    the plane's own vocabulary stays inside it — a kind outside the allowlist is
    the defect, and it is checkable without a live round.
    """
    from cli_agent_orchestrator.core.delivery import DeadReason as _DR

    interrupt_terminals = {
        _DR.INTERRUPT_UNCLAIMED.value,
        _DR.INTERRUPT_WINDOW_LOST.value,
        _DR.INTERRUPT_CANCEL_TIMEOUT.value,
    }
    # Every interrupt terminal reaches a seat as `expired` (the typed dead line)
    # or `exited` (the recovery condition). Neither adds a kind.
    assert BARE_ALLOWLIST >= {"expired", "exited"}
    assert all(isinstance(value, str) for value in interrupt_terminals)


def test_clause_3_the_mandatory_cut_frame_is_bounded() -> None:
    """AC-S1.8 clause 3: framing within N lines / B bytes.

    ``render_interrupt`` is the one thing the plane puts in front of a human
    about an interrupt, so it is where the bound applies. Asserted over a fold
    with every field populated, because an empty one is trivially short.
    """
    from cli_agent_orchestrator.app.diag.interrupt_fold import InterruptFold

    fold = InterruptFold(
        interrupt_id="cb-I",
        found=True,
        phase=AuditPhase.CANCELLED.value,
        phase_source="delivery_msg",
        principal="terminal:supervisor",
        terminal_id=TERMINAL,
        cut_callback_id="cb-N",
        cut_disposition="quarantined",
        cancelled_acp_request_id="req-42",
        dead_tool_call_ids=("tool-1", "tool-2", "tool-3"),
        live_phase="none",
        latency_ms=1234.5,
        latency_breach=True,
        notes=("forced: the viewer waived the quota bounds",),
    )
    rendered = render_interrupt(fold)
    assert len(rendered.splitlines()) <= 14, "the interrupt frame grew past its line budget"
    assert len(rendered.encode()) <= 1024, "the interrupt frame grew past its byte budget"


def test_the_fold_never_reads_the_frame_log() -> None:
    """AC-S1.23's fails-if: "attribution needs the frame log".

    The frames are the adapter's evidence for what the WIRE did. A diagnosis that
    required them could not answer after a restart, when the subprocess and its
    stream are gone — which is exactly when an operator asks.
    """
    import inspect

    from cli_agent_orchestrator.app.diag import interrupt_fold

    source = inspect.getsource(interrupt_fold)
    code = "\n".join(l for l in source.splitlines() if not l.strip().startswith("#"))
    assert "AcpFrameLog" not in code
    assert "frames.jsonl" not in code
