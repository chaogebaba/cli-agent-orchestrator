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
from cli_agent_orchestrator.core import gate as g

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, value: datetime = _NOW) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class FakeNotifier:
    """Records every notice; optionally fails, to exercise the FAILED path."""

    def __init__(self, *, outcome: str = "ok") -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.outcome = outcome

    def notify(self, *, question: g.RoundQuestion, kind: str, lines: Any) -> str | None:
        self.calls.append((question.question_id, kind, tuple(lines)))
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
    question = service.ask(
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
        service.ask(
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
    question = service.ask(
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

    question = service.ask(
        dispatch_id="worker-1",
        question="accept, re-round or override?",
        client_request_id="cr1",
        owner_conversation="seat",
        options=("accept", "re-round"),
        position="dev",
    )
    assert len(notifier.calls) == 1
    qid, kind, lines = notifier.calls[0]
    assert qid == question.question_id and kind == "question"
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
    question = service.ask(
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
    question = service.ask(
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
    question = service.ask(
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
    question = service.ask(
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
    open_q = service.ask(
        dispatch_id="w1",
        question="a?",
        client_request_id="cr1",
        owner_conversation="seat",
        position="dev",
    )
    settled_q = service.ask(
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
