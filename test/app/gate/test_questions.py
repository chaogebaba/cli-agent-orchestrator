"""``GateQuestionService`` over a real store (WP-ARCH A, slice B1).

The service's own judgements, as distinct from the store's transactions: that an
ask is validated BEFORE a row is minted, that a dispatch is provisioned only when
the caller offered a position, that a sweep emits one notice per question it
actually settled, and that the notifier port is reached AFTER the commit and its
failure becomes a row rather than an exception.

The notifier is a fake, which is the point of the port existing in slice B1 at
all: the live wiring is B2's closure over the delivery queue, and ``app`` may not
import the module that closure reaches.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cli_agent_orchestrator.adapters.store.gate import SqliteGateStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.app.gate.questions import GateQuestionService
from cli_agent_orchestrator.app.gate.render import ANOMALY_QUESTION_EXPIRED
from cli_agent_orchestrator.core import gate as g

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _intent(pool: Any, question_id: str) -> Any:
    """The notice intent row for one question."""
    return (
        pool.connection()
        .execute(
            "SELECT state, msg_id, attempts, last_error FROM question_notice_intent "
            "WHERE question_id = ?",
            (question_id,),
        )
        .fetchone()
    )


class FakeClock:
    def __init__(self, value: datetime = _NOW) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class FakeNotifier:
    """Records every notice; optionally fails, to exercise the FAILED path."""

    def __init__(self, *, outcome: str = "ok") -> None:
        self.calls: list[tuple[str, str, tuple[str, ...], str, str]] = []
        self.outcome = outcome

    def notify(
        self,
        *,
        question: g.RoundQuestion,
        kind: str,
        classification: str,
        code: str,
        lines: Any,
    ) -> str | None:
        self.calls.append((question.question_id, kind, tuple(lines), code, classification))
        if self.outcome == "raise":
            raise RuntimeError("transport down")
        if self.outcome == "none":
            return None
        return f"msg-{len(self.calls)}"


@pytest.fixture
def wiring(tmp_path: Path) -> tuple[GateQuestionService, SqliteGateStore, FakeClock]:
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    return GateQuestionService(store, clock=clock), store, clock


def _service(wiring: tuple[GateQuestionService, SqliteGateStore, FakeClock]) -> GateQuestionService:
    return wiring[0]


def test_ask_provisions_the_dispatch_when_a_position_is_offered(
    wiring: tuple[GateQuestionService, SqliteGateStore, FakeClock],
) -> None:
    """A non-gate lane has no round and may have no dispatch row yet.

    Refusing it would make the primitive useless to exactly the callers A2 is
    for.  The provisioning is explicit — the caller names the position — so a
    caller that BELIEVES it already has a dispatch is never quietly given a new
    one.
    """
    service, store, _clock = wiring
    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    assert question.round_id is None
    projection_free = store.open_question_for_dispatch("worker-1")
    assert projection_free is not None and projection_free.question_id == question.question_id


def test_ask_without_a_position_refuses_an_unknown_dispatch(
    wiring: tuple[GateQuestionService, SqliteGateStore, FakeClock],
) -> None:
    with pytest.raises(g.GateQuestionError) as exc:
        _service(wiring).ask(
            dispatch_id="ghost",
            question="accept?",
            client_request_id="cr1",
            owner_conversation="seat",
        )
    assert exc.value.code is g.QuestionRefusal.DISPATCH_UNKNOWN


def test_a_non_blocking_ask_is_refused_before_a_row_is_minted(
    wiring: tuple[GateQuestionService, SqliteGateStore, FakeClock],
) -> None:
    """``validate_ask`` runs first, so a bad ask leaves no trace to clean up."""
    service, store, _clock = wiring
    with pytest.raises(g.GateQuestionError) as exc:
        _asked, _ignored = service.ask(
            dispatch_id="worker-1",
            question="accept?",
            client_request_id="cr1",
            owner_conversation="seat",
            blocking=False,
            position="dev",
        )
    assert exc.value.code is g.QuestionRefusal.DEFAULT_REQUIRED
    assert store.questions_for_owner() == []


def test_is_suspended_is_true_exactly_while_a_question_is_open(
    wiring: tuple[GateQuestionService, SqliteGateStore, FakeClock],
) -> None:
    """The predicate B2's reap consults; here it is the read half only."""
    service, _store, clock = wiring
    assert service.is_suspended("worker-1") is False
    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    assert service.is_suspended("worker-1") is True
    service.escalate(question.question_id)
    assert service.is_suspended("worker-1") is True  # ESCALATED still holds the slot
    clock.value = _NOW + timedelta(minutes=1)
    service.answer(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="seat",
    )
    assert service.is_suspended("worker-1") is False


def test_the_notifier_is_called_once_per_question_after_the_commit(
    tmp_path: Path,
) -> None:
    """One notice per ask, carrying the rendered four-line envelope."""
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    notifier = FakeNotifier()
    service = GateQuestionService(store, clock=clock, notifier=notifier)

    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept, re-round or override?",
        client_request_id="cr1",
        owner_conversation="seat",
        options=("accept", "re-round"),
        position="dev",
    )
    assert len(notifier.calls) == 1
    qid, kind, lines, code, classification = notifier.calls[0]
    assert qid == question.question_id and kind == "question"
    assert code == "" and classification == "expected"
    assert len(lines) == 4
    intent = (
        pool.connection()
        .execute(
            "SELECT state, msg_id FROM question_notice_intent WHERE question_id = ?",
            (question.question_id,),
        )
        .fetchone()
    )
    assert intent["state"] == "SENT" and intent["msg_id"] == "msg-1"


@pytest.mark.parametrize("outcome", ["raise", "none"])
def test_a_notifier_that_does_not_land_settles_the_intent_failed(
    tmp_path: Path, outcome: str
) -> None:
    """A failed notice is a retryable row, never a raise into the asker's face.

    The ask has already committed — the lane IS suspended — so a transport fault
    must not be reported as "your question was not recorded".  Both spellings of
    failure (an exception and a ``None`` id) settle the same way.
    """
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    service = GateQuestionService(store, clock=clock, notifier=FakeNotifier(outcome=outcome))
    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    row = (
        pool.connection()
        .execute(
            "SELECT state, attempts FROM question_notice_intent WHERE question_id = ?",
            (question.question_id,),
        )
        .fetchone()
    )
    assert row["state"] == "FAILED" and row["attempts"] == 1
    assert store.get_question(question.question_id) is not None  # the ask stands


def test_with_no_notifier_wired_the_intent_stays_pending(
    wiring: tuple[GateQuestionService, SqliteGateStore, FakeClock],
) -> None:
    """Slice B1 wires none; PENDING is what B2's sweep finds, not a lost notice."""
    service, store, _clock = wiring
    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    row = (
        store._pool.connection()
        .execute(  # noqa: SLF001
            "SELECT state, attempts FROM question_notice_intent WHERE question_id = ?",
            (question.question_id,),
        )
        .fetchone()
    )
    assert row["state"] == "PENDING" and row["attempts"] == 0


def test_sweep_emits_exactly_one_anomaly_per_question_it_settled(tmp_path: Path) -> None:
    """AC-A7.  A second sweep over the same rows notifies nobody."""
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    notifier = FakeNotifier()
    service = GateQuestionService(store, clock=clock, notifier=notifier)
    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        expires_in_s=60,
        position="dev",
    )
    notifier.calls.clear()

    clock.value = _NOW + timedelta(seconds=61)
    first = service.sweep_expired()
    second = service.sweep_expired()
    assert [q.question_id for q in first] == [question.question_id]
    assert second == []
    assert [call[1] for call in notifier.calls] == ["condition"]
    assert "UNANSWERED" in "\n".join(notifier.calls[0][2])


def test_consume_answer_records_the_receipt(
    wiring: tuple[GateQuestionService, SqliteGateStore, FakeClock],
) -> None:
    service, store, clock = wiring
    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    clock.value = _NOW + timedelta(minutes=1)
    _settled, event = service.answer(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="seat",
    )
    service.consume_answer(event.answer_event_id)
    reloaded = store.get_question(question.question_id)
    assert reloaded is not None and reloaded.consumed_at is not None


def test_require_raises_a_typed_not_found(
    wiring: tuple[GateQuestionService, SqliteGateStore, FakeClock],
) -> None:
    with pytest.raises(g.GateQuestionError) as exc:
        _service(wiring).require("nope")
    assert exc.value.code is g.QuestionRefusal.QUESTION_NOT_FOUND


def test_list_open_defaults_to_the_two_slot_holding_states(
    wiring: tuple[GateQuestionService, SqliteGateStore, FakeClock],
) -> None:
    service, _store, clock = wiring
    open_q, _r1 = service.ask(
        dispatch_id="w1",
        question="a?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    settled_q, _r2 = service.ask(
        dispatch_id="w2",
        question="b?",
        client_request_id="cr2",
        owner_conversation="seat",
        position="dev",
    )
    clock.value = _NOW + timedelta(minutes=1)
    service.answer(
        question_id=settled_q.question_id,
        answer="x",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="seat",
    )
    assert [q.question_id for q in service.list_open()] == [open_q.question_id]
    assert {q.question_id for q in service.list_open(states=tuple(g.QuestionState))} == {
        open_q.question_id,
        settled_q.question_id,
    }


# -- B1 r2: escalation is heard, and a replay is not re-announced ------------


def test_escalating_notifies_the_seat(tmp_path: Path) -> None:
    """N10: a raise the seat never hears about has changed nothing.

    Escalation exists to get a question in front of somebody who will decide it.
    Without a notice the state moves in the database and no human learns
    anything, which is the whole of what escalating is for.
    """
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    notifier = FakeNotifier()
    service = GateQuestionService(store, clock=clock, notifier=notifier)
    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    notifier.calls.clear()

    service.escalate(question.question_id)
    assert [call[0] for call in notifier.calls] == [question.question_id]
    assert notifier.calls[0][1] == "question"


def test_a_replayed_ask_is_not_announced_a_second_time(tmp_path: Path) -> None:
    """One question, one notice. A retry must not put a second copy in front of the seat."""
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    notifier = FakeNotifier()
    service = GateQuestionService(store, clock=clock, notifier=notifier)
    kwargs = dict(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    first, replayed_first = service.ask(**kwargs)  # type: ignore[arg-type]
    second, replayed_second = service.ask(**kwargs)  # type: ignore[arg-type]

    assert replayed_first is False and replayed_second is True
    assert second.question_id == first.question_id
    assert len(notifier.calls) == 1, "a replay must not be announced again"


def test_provisioning_leaves_a_live_builder_alone(tmp_path: Path) -> None:
    """N1: the guard §3.4 self-caught, now protected by a test rather than a scratch run."""
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    service = GateQuestionService(store, clock=clock)
    store.record_dispatch(
        g.Dispatch(
            dispatch_id="b1",
            role=g.DispatchRole.BUILDER,
            position="dev",
            request_id="b1",
            state=g.DispatchState.DISPATCHED,
        )
    )

    _asked, _ig = service.ask(
        dispatch_id="b1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="grunt",
    )

    survivor = store.get_dispatch("b1")
    assert survivor is not None
    assert survivor.role is g.DispatchRole.BUILDER, "an ask must not re-role a dispatch"
    assert survivor.position == "dev", "an ask must not re-position a dispatch"
    assert survivor.state is g.DispatchState.AWAITING_ANSWER  # only the state moves


# -- slice B2: the retry sweep and the settlement read ----------------------


def test_a_failed_notice_is_retried_by_the_sweep_and_never_double_sent(
    tmp_path: Path,
) -> None:
    """The other half of the ask transaction's promise (B2).

    The ask commits an intent saying somebody must be told.  If the transport was
    down at that instant the intent is FAILED and the lane would otherwise wait
    out its whole hour for a question nobody ever saw.  The sweep discharges it —
    and once the retry lands, a LATER sweep must not send it again, because two
    copies of one question is the context-noise the seat contract forbids.
    """
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    notifier = FakeNotifier(outcome="raise")
    service = GateQuestionService(store, clock=clock, notifier=notifier)

    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    assert len(notifier.calls) == 1  # the original attempt
    assert _intent(pool, question.question_id)["state"] == "FAILED"

    # The transport comes back; the sweep re-sends exactly once.
    notifier.outcome = "ok"
    _expired, retried = service.sweep()
    assert [q.question_id for q in retried] == [question.question_id]
    assert len(notifier.calls) == 2
    row = _intent(pool, question.question_id)
    assert row["state"] == "SENT" and row["attempts"] == 2

    # And a later sweep has nothing to do: SENT is not a retry candidate.
    _expired2, retried2 = service.sweep()
    assert retried2 == []
    assert len(notifier.calls) == 2


def test_the_sweep_does_not_re_announce_a_question_that_was_answered(
    tmp_path: Path,
) -> None:
    """A settled question leaves the retry set, whatever its notice intent says."""
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    notifier = FakeNotifier(outcome="raise")
    service = GateQuestionService(store, clock=clock, notifier=notifier)
    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    clock.value = _NOW + timedelta(minutes=1)
    service.answer(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="seat",
    )
    notifier.calls.clear()
    notifier.outcome = "ok"
    _expired, retried = service.sweep()
    assert retried == [] and notifier.calls == []


def test_the_sweep_expires_before_it_retries(tmp_path: Path) -> None:
    """Order matters: a question that has just timed out is not also re-asked.

    Expiry first removes it from the retry candidates, so the seat gets ONE
    anomaly rather than an anomaly and a fresh copy of a dead question.
    """
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    notifier = FakeNotifier(outcome="raise")
    service = GateQuestionService(store, clock=clock, notifier=notifier)
    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        expires_in_s=60,
        position="dev",
    )
    notifier.calls.clear()
    notifier.outcome = "ok"

    clock.value = _NOW + timedelta(seconds=61)
    expired, retried = service.sweep()
    assert [q.question_id for q in expired] == [question.question_id]
    assert retried == []
    assert [call[1] for call in notifier.calls] == ["condition"]


def test_the_anomaly_envelope_carries_a_distinguishing_code(tmp_path: Path) -> None:
    """An expiry and an ordinary run condition share a wire KIND (A5's four).

    A consumer that had to tell them apart by reading the summary prose would
    break the first time the wording changed, so the code travels beside the
    kind.
    """
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    notifier = FakeNotifier()
    service = GateQuestionService(store, clock=clock, notifier=notifier)
    _asked, _ig = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        expires_in_s=60,
        position="dev",
    )
    notifier.calls.clear()
    clock.value = _NOW + timedelta(seconds=61)
    service.sweep_expired()

    assert [call[1] for call in notifier.calls] == ["condition"]
    assert [call[3] for call in notifier.calls] == [ANOMALY_QUESTION_EXPIRED]
    # A question notice, by contrast, needs no further discrimination.
    _asked, _ig = service.ask(
        dispatch_id="worker-2",
        question="and this?",
        client_request_id="cr2",
        owner_conversation="seat",
        position="dev",
    )
    assert notifier.calls[-1][1] == "question" and notifier.calls[-1][3] == ""


def test_settlement_returns_the_question_and_its_answer(tmp_path: Path) -> None:
    """What the bounded poll reads each iteration."""
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    store = SqliteGateStore(pool, clock=clock)
    service = GateQuestionService(store, clock=clock)
    question, _replayed = service.ask(
        dispatch_id="worker-1",
        question="accept?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    record, answer = service.settlement(question.question_id)
    assert record.is_open and answer is None

    clock.value = _NOW + timedelta(minutes=1)
    service.answer(
        question_id=question.question_id,
        answer="accept",
        answered_by="seat",
        client_request_id="ar1",
        caller_conversation="seat",
    )
    record, answer = service.settlement(question.question_id)
    assert not record.is_open
    assert answer is not None and answer.answer == "accept"


def test_settlement_of_an_unknown_question_is_a_typed_refusal(tmp_path: Path) -> None:
    _result, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    clock = FakeClock()
    service = GateQuestionService(SqliteGateStore(pool, clock=clock), clock=clock)
    with pytest.raises(g.GateQuestionError) as exc:
        service.settlement("nope")
    assert exc.value.code is g.QuestionRefusal.QUESTION_NOT_FOUND
