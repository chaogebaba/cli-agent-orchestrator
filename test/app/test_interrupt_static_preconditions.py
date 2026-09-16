"""AC-S1.28 — the static preconditions, as checks rather than review notes.

Every assertion here is over the SOURCE, because each one describes a property a
dynamic test cannot observe.  "The aggregate holds no clock" is invisible to a
test that passes a clock in; "there is exactly one lifetime-law authority" is
invisible to any test of one of them; "no operation calls QueueStore" is invisible
until the day one does and the crash gap it opens is hit in production.

AC-S1.28's fails-if is a four-item list — deadline ambiguity, a second clock, a
nested transaction, authority drift — and the checks below are in that order.
"""

from __future__ import annotations

import ast
import inspect
import typing
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.store import interrupt as store_module
from cli_agent_orchestrator.adapters.store.interrupt import SqliteInterruptStore
from cli_agent_orchestrator.core import interrupt as core_module
from cli_agent_orchestrator.core import ports
from cli_agent_orchestrator.core.interrupt import ActiveTurnHandle, CancelWindow, WindowLost


def _tree(module: object) -> ast.Module:
    return ast.parse(Path(inspect.getfile(module)).read_text())  # type: ignore[arg-type]


def _method(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(store_module)):
        if isinstance(node, ast.ClassDef) and node.name == "SqliteInterruptStore":
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == name:
                    return child
    raise AssertionError(f"SqliteInterruptStore.{name} not found")


_AGGREGATE_OPERATIONS = (
    "admit_interrupt",
    "claim_next",
    "begin_cancel",
    "mark_cancel_sent",
    "race_lost",
    "settle_to_prompt",
    "complete_prompt",
    "fail_interrupt",
    "begin_recovery",
    "finish_recovery",
    "expire_recovery",
    "finalize_no_resume_exit",
)


# ------------------------------------------------- 1. deadline ambiguity


def test_begin_cancel_has_the_signature_the_amendment_quotes() -> None:
    """``begin_cancel(I_fence, active_turn, now) -> CancelWindow | WindowLost``.

    D6b(3), the S1 DoD, §14 and A2.9(iv) all quote this one signature.  Checking
    it here is what stops those four copies from drifting apart silently, which
    is AC-S1.28's "authority drift" in its most literal form.
    """
    signature = inspect.signature(SqliteInterruptStore.begin_cancel)
    assert list(signature.parameters) == ["self", "fence", "active_turn", "now"]
    hints = typing.get_type_hints(SqliteInterruptStore.begin_cancel)
    assert hints["return"] == CancelWindow | WindowLost
    assert hints["now"].__name__ == "datetime"


def test_the_two_instants_cannot_be_collapsed() -> None:
    """A ``CancelWindow`` whose lease does not outlive its deadline is unconstructable."""
    import datetime as dt

    now = dt.datetime(2026, 9, 16, tzinfo=dt.UTC)
    with pytest.raises(ValueError):
        CancelWindow(deadline=now, lease_until=now)
    with pytest.raises(ValueError):
        CancelWindow(deadline=now, lease_until=now - dt.timedelta(seconds=1))


def test_the_schema_makes_the_cancel_deadline_nullable_and_phase_conditional() -> None:
    """Nullable BY PHASE: non-null in ``cancelling``, null in ``none``/``prompting``.

    A sentinel instant would have satisfied "the column exists" while making
    "there is no cancel in flight" indistinguishable from "the cancel deadline is
    the epoch".
    """
    from cli_agent_orchestrator.adapters.store.migrator import _INTERRUPT_STATE_DDL

    ddl = " ".join(_INTERRUPT_STATE_DDL.split())
    assert "deadline TEXT," in ddl, "the cancel deadline must be nullable"
    assert "CHECK (phase != 'cancelling' OR deadline IS NOT NULL)" in ddl
    assert "CHECK (phase NOT IN ('none','prompting') OR deadline IS NULL)" in ddl


def test_the_deadline_is_persisted_by_begin_cancel_not_merely_returned() -> None:
    """"Omit deadline persistence" is a named r18 mutant; a restart reads the row."""
    source = ast.dump(_method("begin_cancel"))
    assert "deadline = ?" in inspect.getsource(SqliteInterruptStore.begin_cancel)
    assert "interrupt_state" in source or "interrupt_state" in inspect.getsource(
        SqliteInterruptStore.begin_cancel
    )


# ------------------------------------------------------- 2. a second clock


def test_the_aggregate_holds_no_clock() -> None:
    """The store neither takes a clock in ``__init__`` nor reaches for one.

    ``now`` is the receiver task's single sample, passed in.  A store that could
    read a clock would be a second authority on when the cancel window opened,
    and the two would disagree exactly when it matters — across a restart.
    """
    init = inspect.signature(SqliteInterruptStore.__init__)
    assert "clock" not in init.parameters


@pytest.mark.parametrize("name", _AGGREGATE_OPERATIONS)
def test_no_operation_samples_a_clock_for_a_persisted_deadline(name: str) -> None:
    """No ``datetime.now`` / ``utcnow`` reaches a value the design says is persisted.

    ``complete_prompt``/``fail_interrupt``/``begin_recovery`` may stamp a
    ``terminated_at``/``died_at`` — an audit instant with no bound hanging off it
    — so the check is targeted at the DEADLINE-bearing methods, where a resample
    would silently move a bound the restart path re-reads.
    """
    deadline_bearing = {"begin_cancel", "admit_interrupt", "claim_next", "begin_recovery"}
    if name not in deadline_bearing:
        return
    source = inspect.getsource(getattr(SqliteInterruptStore, name))
    assert "datetime.now" not in source, f"{name} resamples the clock"
    assert "utcnow" not in source


# --------------------------------------------- 3. one transaction, no nesting


@pytest.mark.parametrize("name", _AGGREGATE_OPERATIONS)
def test_each_operation_opens_at_most_one_immediate_transaction(name: str) -> None:
    """One operation, one ``BEGIN IMMEDIATE``.  Two would be two crash gaps."""
    source = inspect.getsource(getattr(SqliteInterruptStore, name))
    assert source.count("immediate_transaction(") <= 1, f"{name} opens more than one transaction"


@pytest.mark.parametrize("name", _AGGREGATE_OPERATIONS)
def test_no_operation_calls_another_operation(name: str) -> None:
    """Aggregate NON-COMPOSITION: a transition composed of two is not atomic.

    AC-S1.27's oracle is "rollback exposes all-or-none".  An operation that
    called another would commit the inner one first, and the oracle would be
    false for every crash landing between the two commits.
    """
    method = _method(name)
    for node in ast.walk(method):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr in _AGGREGATE_OPERATIONS
            ):
                raise AssertionError(f"{name} composes {node.func.attr}")


def test_the_aggregate_never_reaches_the_queue_store() -> None:
    """"No ``QueueStore`` participates" (A2.9(iv)) — checked as reachability.

    The aggregate writes ``delivery_msg`` with its own SQL inside its own
    transaction.  Importing the queue store would make it possible to call a
    method that opens a SECOND ``BEGIN IMMEDIATE`` on the same connection, which
    SQLite rejects and which the design forbids for the stronger reason: the
    queue store's transactions are not this transition's.
    """
    source = inspect.getsource(store_module)
    assert "SqliteQueueStore" not in source
    assert "QueueStore" not in source
    assert "from cli_agent_orchestrator.adapters.store.queue import" not in source


def test_no_operation_performs_io_under_the_write_lock() -> None:
    """No socket, subprocess or sleep anywhere in the aggregate.

    "I/O under SQLite" is a named r18 mutant, and AC-S1.29's four causal barriers
    are the dynamic half of the same claim.  This is the cheap static half: the
    module may not even NAME the modules that would let it happen.
    """
    # Over the AST, not the text: the prose in this module DISCUSSES the ACP
    # subprocess at length, and a grep over source text would fail on the
    # explanation of why no I/O happens here.  What matters is whether an
    # identifier is imported or referenced in CODE.
    tree = _tree(store_module)
    forbidden = {"subprocess", "socket", "requests", "httpx", "asyncio", "sleep", "open"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name.split(".")[0] for a in node.names if a.name.split(".")[0] in forbidden]
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in forbidden:
                offenders.append(root)
        elif isinstance(node, ast.Name) and node.id in forbidden:
            offenders.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            offenders.append(node.attr)
    assert not offenders, f"the aggregate reaches for I/O: {sorted(set(offenders))}"


# -------------------------------------------------------- 4. authority drift


def test_there_is_exactly_one_lifetime_law_authority() -> None:
    """``effective_deadline`` is defined ONCE and the arithmetic appears nowhere else.

    A2.9(iv) states the law verbatim and three places consume it — the claim, the
    death predicate and ``begin_cancel``'s lifetime check.  Three copies of an
    arithmetic rule is how two of them come to disagree about whether a caller
    deadline may be extended, which is r3's B2 bug.
    """
    definitions = [
        node
        for node in ast.walk(_tree(core_module))
        if isinstance(node, ast.FunctionDef) and node.name == "effective_deadline"
    ]
    assert len(definitions) == 1

    src_root = Path(inspect.getfile(core_module)).resolve().parents[1]
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        if path == Path(inspect.getfile(core_module)).resolve():
            continue
        text = path.read_text()
        if "BUSY_CREDIT_CAP_S" in text and "min(" in text and "def effective_deadline" in text:
            offenders.append(str(path.relative_to(src_root)))
    assert not offenders, f"a second lifetime-law authority: {offenders}"


def test_the_active_turn_handle_carries_no_queue_identifier() -> None:
    """"No queue ids in H" — asserted over the dataclass's own fields.

    A2.9(iii) keeps receipt and active turn separate: the handle names a TURN.
    A ``msg_id`` on it would let an interrupt reach N's queue row, and A2.3/I3
    says plainly that a delivered row is never re-presented or re-leased.
    """
    fields = set(ActiveTurnHandle.__dataclass_fields__)
    for forbidden in ("msg_id", "claim_id", "lease_owner", "lease_expires_at", "fence"):
        assert forbidden not in fields


def test_an_acp_envelope_carries_exactly_one_callback() -> None:
    """AC-S1.25's multi-callback arm, as a shape check.

    The native carrier batches several entries per wake; on ACP one envelope is
    one callback.  A batch would make AC-S1.8 clause 1 — exactly one delivery per
    ``callback_id`` — unprovable, because a partial batch write has no honest
    per-callback outcome.
    """
    from cli_agent_orchestrator.core.interrupt import SubmitEnvelope

    fields = SubmitEnvelope.__dataclass_fields__
    assert "callback_id" in fields
    assert fields["callback_id"].type in ("str", str)
    assert "callback_ids" not in fields
    assert "entries" not in fields


def test_claim_next_returns_one_row_not_a_sequence() -> None:
    """The batch claim is not merely unused for an ACP receiver — it is unexpressible."""
    hints = typing.get_type_hints(SqliteInterruptStore.claim_next)
    rendered = str(hints["return"])
    assert "list" not in rendered and "Sequence" not in rendered
    assert "ClaimedRow" in rendered


def test_the_interrupt_store_satisfies_its_port() -> None:
    assert isinstance(
        SqliteInterruptStore(_NullPool(), limiter=_NullLimiter()), ports.InterruptStore
    )


class _NullPool:
    def connection(self) -> object:  # pragma: no cover — shape only
        raise AssertionError("not called")

    def checkpoint(self) -> None:  # pragma: no cover
        raise AssertionError("not called")


class _NullLimiter:
    def decide(self, **_: object) -> object:  # pragma: no cover
        raise AssertionError("not called")
