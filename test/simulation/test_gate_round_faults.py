"""Gate-round fault arms that are 2a-scoped (WP-ARCH Amendment A, slice 2a).

Blueprint name ``test/sim/test_gate_round_faults.py``; the repo's simulation
substrate lives under ``test/simulation/``, so the file lands here.  The reap
sweep and the question wait adapters (A4/A6/A7) are 2b/2c; the arms that are
2a-scoped are the ones about DURABLE ROWS surviving a crash, which are
deterministic without the async substrate:

* **Crash between effect-intent and spawn** — the intent is recorded BEFORE the
  external operation (P1), so a crash before the result is written leaves the
  intent readable with NO result.  Re-projection shows exactly that: the repair
  is possible from the intent row, which a DISPATCHED row alone could not do.
* **Row-version conflict** — a stale writer is a typed refusal, not a silent
  overwrite (the optimistic-concurrency arm named in the brief).

A "crash" here is modelled by closing the pool between the two writes and
reopening it: SQLite has committed the intent, the result write never happened,
and the reopened store re-projects from what is on disk.  That is exactly the
observable a real crash leaves, and it is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.gate import SqliteGateStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.core import gate as g

TEST_BUSY_TIMEOUT_MS = 5000

#: The fixed "now" the slice-B2 question arms are written against.
_SIM_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _manifest() -> g.ArtifactManifest:
    return g.ArtifactManifest(
        manifest_version=1,
        repo_bindings=(g.RepoBinding(name="fork", commit="c" * 12),),
        base_sha="b" * 12,
        head_sha="h" * 12,
        branch="cao/x",
        worktree_path="/w",
        entries=(g.DiffEntry(post_path="a.py", post_object_id="o1"),),
        blueprint_sha="bp" * 4,
        ac_list_sha="ac" * 4,
    )


def _fresh_store(path: Path) -> SqliteGateStore:
    _res, pool = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert pool is not None
    return SqliteGateStore(pool, clock=FakeClock())


def test_crash_between_effect_intent_and_spawn_reprojects_intent_without_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "gate.db"
    store = _fresh_store(path)
    run = store.open_run(
        wp="F791", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2
    )
    rnd = store.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
    )
    dispatch = g.Dispatch(
        dispatch_id="D1",
        round_id=rnd.round_id,
        role=g.DispatchRole.BUILDER,
        position="dev",
        request_id="req1",
        effect_id="E1",
    )
    store.record_dispatch(dispatch)
    # Intent recorded BEFORE the spawn (P1).
    store.record_effect_intent(
        g.EffectIntent(
            effect_id="E1",
            kind=g.EffectKind.SPAWN,
            round_id=rnd.round_id,
            dispatch_id="D1",
            requested_at=datetime(2026, 9, 9, tzinfo=UTC),
        )
    )
    # --- CRASH: the process dies before the spawn result is recorded. ---
    store._pool.close_all()  # noqa: SLF001 — modelling the crash window

    # Reopen from disk and re-project the round.
    reopened = _fresh_store(path)
    projection = reopened.project_round(rnd.round_id)
    assert projection is not None
    assert len(projection.effect_intents) == 1
    assert projection.effect_intents[0].effect_id == "E1"
    # The intent survived; NO result was written, so the repair is possible.
    assert projection.effect_results == ()


def test_effect_intent_is_idempotent_across_retry(tmp_path: Path) -> None:
    # A retried intent (same effect_id) is a no-op, not a second row — the adapter
    # dedups on effect_id, which is what makes a blind retry after an uncertain
    # crash safe.
    store = _fresh_store(tmp_path / "gate.db")
    run = store.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2)
    rnd = store.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
    )
    intent = g.EffectIntent(
        effect_id="E1",
        kind=g.EffectKind.MERGE,
        round_id=rnd.round_id,
        requested_at=datetime(2026, 9, 9, tzinfo=UTC),
    )
    store.record_effect_intent(intent)
    store.record_effect_intent(intent)  # retry
    projection = store.project_round(rnd.round_id)
    assert projection is not None
    assert len(projection.effect_intents) == 1


def test_effect_result_without_intent_is_refused(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path / "gate.db")
    with pytest.raises(g.GateError):
        store.record_effect_result(
            g.EffectResult(effect_id="UNKNOWN", outcome=g.EffectOutcome.APPLIED)
        )


def test_uncertain_result_has_no_settled_at(tmp_path: Path) -> None:
    # An UNCERTAIN result is reconciled, not retried blindly (R30); it carries no
    # settled_at until reconciliation confirms it.
    store = _fresh_store(tmp_path / "gate.db")
    run = store.open_run(wp="F", lane="dev", owner_conversation="c1", owner_epoch=1, max_rounds=2)
    rnd = store.open_round(
        run_id=run.run_id,
        build_inputs=_manifest(),
        execution_target=g.ExecutionTarget(host="laptop"),
    )
    store.record_effect_intent(
        g.EffectIntent(
            effect_id="E1",
            kind=g.EffectKind.PUSH,
            round_id=rnd.round_id,
            requested_at=datetime(2026, 9, 9, tzinfo=UTC),
        )
    )
    store.record_effect_result(
        g.EffectResult(effect_id="E1", outcome=g.EffectOutcome.UNCERTAIN, evidence_ref="ref")
    )
    projection = store.project_round(rnd.round_id)
    assert projection is not None
    assert projection.effect_results[0].outcome is g.EffectOutcome.UNCERTAIN
    assert projection.effect_results[0].settled_at is None


# -- slice B2: the question's fault arms -------------------------------------
#
# The three ways a durable question can be half-done, each modelled the way the
# 2a arms above model a crash — by closing the pool between two writes and
# reopening it, so what the reopened store sees is exactly what a real crash
# would have left on disk.


def _open_question(store: SqliteGateStore, *, dispatch_id: str = "d1", ttl_s: int = 3600) -> Any:
    store.record_dispatch(
        g.Dispatch(
            dispatch_id=dispatch_id,
            role=g.DispatchRole.OTHER,
            position="dev",
            request_id=dispatch_id,
            # DISPATCHED, not the model default: a release now RESTORES the state
            # the ask suspended, so these arms say which state they mean.
            state=g.DispatchState.DISPATCHED,
        )
    )
    record, _replayed = store.ask_question(
        dispatch_id=dispatch_id,
        round_id=None,
        client_request_id=f"cr-{dispatch_id}",
        owner_conversation="seat",
        owner_epoch=1,
        continuation_kind=g.ContinuationKind.ASSIGNMENT,
        continuation_ref=dispatch_id,
        question="accept, re-round or override?",
        options=("accept", "re-round"),
        blocking=True,
        asked_at=_SIM_NOW,
        expires_at=_SIM_NOW + timedelta(seconds=ttl_s),
    )
    return record


def test_crash_with_the_answer_committed_but_undelivered_is_recoverable(
    tmp_path: Path,
) -> None:
    """R28's window, made observable rather than indistinguishable from success.

    The answer commits and the server dies before the asker ever receives it.  On
    restart the row says ANSWERED, the delivery intent still says PENDING, and
    the dispatch is still AWAITING_ANSWER — which together say precisely "a
    decision exists and the lane does not have it yet".  Had ANSWERED alone been
    the record, that state would look identical to a lane that had already
    resumed, and the answer would be lost in the only way that matters.
    """
    path = tmp_path / "gate.db"
    store = _fresh_store(path)
    question = _open_question(store)
    _settled, event = store.answer_question(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="seat",
        caller_epoch=1,
        now=_SIM_NOW + timedelta(minutes=1),
    )
    store._pool.close_all()  # noqa: SLF001 — the crash

    _res, pool = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert pool is not None
    reopened = SqliteGateStore(pool, clock=FakeClock())
    reloaded = reopened.get_question(question.question_id)
    assert reloaded is not None
    assert reloaded.state is g.QuestionState.ANSWERED
    assert reloaded.consumed_at is None
    intent = (
        pool.connection()
        .execute(
            "SELECT state FROM answer_delivery_intent WHERE answer_event_id = ?",
            (event.answer_event_id,),
        )
        .fetchone()
    )
    assert intent["state"] == "PENDING"
    assert (
        pool.connection()
        .execute("SELECT state FROM gate_dispatch WHERE dispatch_id='d1'")
        .fetchone()[0]
        == g.DispatchState.AWAITING_ANSWER.value
    )
    # And the recovery is available from the rows alone: the answer is readable.
    recorded = reopened.get_answer(event.answer_event_id)
    assert recorded is not None and recorded.answer == "accept"


def test_delivered_but_not_consumed_is_distinct_from_consumed(tmp_path: Path) -> None:
    """ANSWERED is not proof of receipt; CONSUMED is the receipt (R28).

    Two separate records, and the dispatch leaves AWAITING_ANSWER on the second
    one and not the first.  Collapsing them into a single flag is the mutant this
    arm kills: a run would then read as resumed the instant somebody typed an
    answer, whether or not the lane ever woke.
    """
    path = tmp_path / "gate.db"
    store = _fresh_store(path)
    question = _open_question(store)
    _settled, event = store.answer_question(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="seat",
        caller_epoch=1,
        now=_SIM_NOW + timedelta(minutes=1),
    )
    conn = store._pool.connection()  # noqa: SLF001
    before = conn.execute("SELECT state FROM gate_dispatch WHERE dispatch_id='d1'").fetchone()[0]
    store.mark_answer_consumed(event.answer_event_id, now=_SIM_NOW + timedelta(minutes=2))
    after = conn.execute("SELECT state FROM gate_dispatch WHERE dispatch_id='d1'").fetchone()[0]

    assert before == g.DispatchState.AWAITING_ANSWER.value
    assert after == g.DispatchState.DISPATCHED.value
    assert (
        conn.execute(
            "SELECT state FROM answer_delivery_intent WHERE answer_event_id = ?",
            (event.answer_event_id,),
        ).fetchone()[0]
        == "CONSUMED"
    )
    reloaded = store.get_question(question.question_id)
    assert reloaded is not None and reloaded.consumed_at is not None


def test_a_simultaneous_answer_and_expiry_settle_exactly_once(tmp_path: Path) -> None:
    """Both settlers arrive at the same instant; one wins and the other is refused.

    ``now`` is exactly ``expires_at`` for both, which is the worst case the
    conditional transaction exists for.  Whichever commits first, the second is a
    typed refusal — never a second settlement, and never an expiry overwriting a
    consumed answer.
    """
    path = tmp_path / "gate.db"
    store = _fresh_store(path)
    question = _open_question(store, ttl_s=60)
    deadline = _SIM_NOW + timedelta(seconds=60)

    # The sweep gets there first.
    expired = store.expire_due_questions(deadline)
    assert [q.question_id for q in expired] == [question.question_id]
    with pytest.raises(g.GateQuestionError) as exc:
        store.answer_question(
            question_id=question.question_id,
            answer="accept",
            answered_by="seat",
            client_request_id="ar1",
            caller_conversation="seat",
            caller_epoch=1,
            now=deadline,
        )
    assert exc.value.code is g.QuestionRefusal.QUESTION_SETTLED

    # The mirror image, on a fresh question: the answer lands at the deadline
    # minus an instant and the sweep then finds nothing to do.
    other = _open_question(store, dispatch_id="d2", ttl_s=60)
    store.answer_question(
        question_id=other.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar2",
        caller_conversation="seat",
        caller_epoch=1,
        now=deadline - timedelta(microseconds=1),
    )
    assert store.expire_due_questions(deadline) == []
    reloaded = store.get_question(other.question_id)
    assert reloaded is not None and reloaded.state is g.QuestionState.ANSWERED


def test_expiry_emits_exactly_one_anomaly_per_question(tmp_path: Path) -> None:
    """AC-A7, through the service and its notifier.

    Two sweeps over the same overdue question must produce ONE envelope, because
    the store returns only the rows IT settled.  A sweep that re-read "what is
    expired" instead would announce a question a concurrent sweep had already
    announced, and the seat would see the same dead question twice.
    """
    from cli_agent_orchestrator.app.gate.questions import GateQuestionService
    from cli_agent_orchestrator.app.gate.render import ANOMALY_QUESTION_EXPIRED

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def notify(
            self, *, question: Any, kind: str, classification: str, code: str, lines: Any
        ) -> str | None:
            self.calls.append((question.question_id, kind, code))
            assert classification == "anomaly", "an expiry must be typed ANOMALY (A5)"
            return "1"

    class _MovableClock:
        def __init__(self) -> None:
            self.value = _SIM_NOW

        def now(self) -> datetime:
            return self.value

    path = tmp_path / "gate.db"
    _res, pool = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert pool is not None
    clock = _MovableClock()
    store = SqliteGateStore(pool, clock=clock)
    notifier = _Recorder()
    service = GateQuestionService(store, clock=clock, notifier=notifier)
    question, _replayed = service.ask(
        dispatch_id="w1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        expires_in_s=60,
        position="dev",
    )
    notifier.calls.clear()

    clock.value = _SIM_NOW + timedelta(seconds=61)
    first = service.sweep_expired()
    second = service.sweep_expired()
    third = service.sweep_expired()

    assert [q.question_id for q in first] == [question.question_id]
    assert second == [] and third == []
    assert notifier.calls == [(question.question_id, "condition", ANOMALY_QUESTION_EXPIRED)]


def test_a_suspended_dispatch_is_refused_by_the_reap_predicate(tmp_path: Path) -> None:
    """AC-A4's read half: a lane waiting on an answer is not idle (slice B2).

    ``is_suspended`` is the predicate a reap must consult, and this arm proves it
    answers correctly across the whole lifecycle — true while PENDING, still true
    while ESCALATED (the slot is held), false once the answer is consumed and
    false once the question expires.

    The LIVE reap consult is deliberately NOT wired here. A2 property (1) is
    blueprint-bound to A6a's durable lease, which has only half landed
    (``session_lifecycle_lease.py`` is still process-local dicts with no TTL and
    no fencing token), and there is no single ``reap_idle`` function to consult
    from. Wiring it on top of a lease that cannot fence would be a guard that
    looks present and is not. Recorded as a named follow-on, with this arm as the
    standing proof that the predicate itself is ready for it.
    """
    from cli_agent_orchestrator.app.gate.questions import GateQuestionService

    class _MovableClock:
        def __init__(self) -> None:
            self.value = _SIM_NOW

        def now(self) -> datetime:
            return self.value

    path = tmp_path / "gate.db"
    _res, pool = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert pool is not None
    clock = _MovableClock()
    store = SqliteGateStore(pool, clock=clock)
    service = GateQuestionService(store, clock=clock)

    assert service.is_suspended("w1") is False
    question, _replayed = service.ask(
        dispatch_id="w1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        expires_in_s=60,
        position="dev",
    )
    assert service.is_suspended("w1") is True
    service.escalate(question.question_id)
    assert service.is_suspended("w1") is True

    clock.value = _SIM_NOW + timedelta(seconds=10)
    _settled, event = service.answer(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="seat",
    )
    assert service.is_suspended("w1") is False
    service.consume_answer(event.answer_event_id)
    assert service.is_suspended("w1") is False

    # And the expiry path clears it too, so a timed-out lane is reapable again.
    other, _r = service.ask(
        dispatch_id="w2",
        question="and this?",
        client_request_id="cr2",
        owner_conversation="seat",
        expires_in_s=60,
        position="dev",
    )
    assert service.is_suspended("w2") is True
    clock.value = _SIM_NOW + timedelta(seconds=200)
    assert [q.question_id for q in service.sweep_expired()] == [other.question_id]
    assert service.is_suspended("w2") is False
