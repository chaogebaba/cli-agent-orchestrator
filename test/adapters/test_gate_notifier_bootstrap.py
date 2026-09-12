"""The live question notifier, at the composition root (WP-ARCH A, slice B2).

The ONE place a rendered question becomes a message the supervisor will see. Two
properties are load-bearing and neither is visible from ``app/gate``, which only
ever sees a Protocol:

* **It writes one inbox row and stops.** With ``CAO_DELIVERY_QUEUE=on`` that row
  is write-through into the durable delivery queue and the tick is what wakes the
  seat, so there must be no second path — no wake call, no doorbell, no pane
  paste. A second path to the same seat is the duplicate-delivery class the
  one-surface contract exists to prevent, and it would also be a second source of
  truth about whether the supervisor was told.
* **A refused write is REPORTED, not raised.** The ask has already committed and
  the lane IS suspended, so ``database is locked`` must leave a retryable intent
  behind rather than surface to the asker as "your question was not recorded".

Both are tested against the real notifier with ``create_inbox_message`` patched,
which is the seam — the notifier reaches it by a late import inside the method
precisely so this is patchable and so ``bootstrap`` carries no import-time
dependency on the legacy client.

The r2 arms at the end go one layer further out, because the defect they exist
for is only reachable that way: a patched ``create_inbox_message`` can assert what
the notifier returns, but not whether the REAL write path refused, and the review
reproduced a refusal being reported as SENT off the legacy surrogate id. Those
arms therefore run ``create_inbox_message`` itself over a real file with both
schemas, the delivery queue armed at ``on``, and a real queue store in it.
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.clients.database import InboxInsertDisposition, InboxInsertResult
from cli_agent_orchestrator.core import gate as g
from cli_agent_orchestrator.core.ports import QuestionNotifier

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class _Message:
    def __init__(self, message_id: int | None) -> None:
        self.id = message_id


def _accepted(message_id: int | None) -> InboxInsertResult:
    return InboxInsertResult(
        _Message(message_id),
        InboxInsertDisposition.QUEUE_ACCEPTED,
        None if message_id is None else str(message_id),
    )


def _question(**overrides: Any) -> g.RoundQuestion:
    base: dict[str, Any] = {
        "question_id": "Q1",
        "dispatch_id": "w1",
        "client_request_id": "cr1",
        "owner_conversation": "seat-abc",
        "owner_epoch": 1,
        "continuation_kind": g.ContinuationKind.ASSIGNMENT,
        "continuation_ref": "w1",
        "asked_at": _NOW,
        "expires_at": _NOW + timedelta(hours=1),
        "question": "accept, re-round or override?",
        "row_version": 1,
    }
    base.update(overrides)
    return g.RoundQuestion(**base)


def test_the_notifier_satisfies_the_port() -> None:
    assert isinstance(bootstrap.build_question_notifier(), QuestionNotifier)


def test_an_accepted_write_returns_the_message_id() -> None:
    notifier = bootstrap.build_question_notifier()
    with patch(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        return_value=_accepted(4242),
    ) as create:
        msg_id = notifier.notify(
            question=_question(),
            kind="question",
            classification="expected",
            code="",
            lines=("a", "b"),
        )
    assert msg_id == "4242"
    kwargs = create.call_args.kwargs
    assert kwargs["sender_id"] == "cao-gate"
    assert kwargs["message"] == "a\nb"


def test_the_receiver_is_read_off_the_question_not_configured() -> None:
    """An ownership transfer redirects the notice for free.

    ``claim_ownership`` rewrites ``owner_conversation`` on every open question, so
    routing to it means the seat's identity lives in exactly one place. A
    configured receiver would be a second record of where the seat is, free to
    disagree with the rows.
    """
    notifier = bootstrap.build_question_notifier()
    with patch(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        return_value=_accepted(1),
    ) as create:
        notifier.notify(
            question=_question(owner_conversation="seat-xyz"),
            kind="question",
            classification="expected",
            code="",
            lines=("a",),
        )
    assert create.call_args.kwargs["receiver_id"] == "seat-xyz"


def test_an_explicit_receiver_overrides_the_question_s_owner() -> None:
    notifier = bootstrap.build_question_notifier("pinned-seat")
    with patch(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        return_value=_accepted(1),
    ) as create:
        notifier.notify(
            question=_question(), kind="question", classification="expected", code="", lines=("a",)
        )
    assert create.call_args.kwargs["receiver_id"] == "pinned-seat"


def test_the_notice_carries_no_supersede_key_so_the_queue_write_through_survives() -> None:
    """Measured, not assumed: a supersede key takes the notice OUT of the queue.

    F578 supersession runs inside the legacy insert's own transaction and takes
    the write lock, so the queue's write-through cannot ``BEGIN IMMEDIATE`` on
    its own connection, returns ``None``, and the caller falls back to a legacy
    inbox row. On a scratch home (2026-09-11) the same call with a supersede key
    landed in ``inbox`` with ``delivery_msg`` empty, and without one landed in
    ``delivery_msg`` in state ``ready``.

    Nothing is lost by omitting it: a retry only happens when the previous
    attempt RAISED, and an attempt that raised wrote no row to supersede.
    """
    notifier = bootstrap.build_question_notifier()
    with patch(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        return_value=_accepted(1),
    ) as create:
        notifier.notify(
            question=_question(), kind="question", classification="expected", code="", lines=("a",)
        )
    assert "supersede_key" not in create.call_args.kwargs


def test_a_locked_database_is_reported_as_a_failure_never_raised() -> None:
    """The case this is built for: transient, retried, and counted.

    Raising here would propagate into ``_notify``'s caller — the ask path — and a
    lane that IS suspended would be told its question was not recorded.
    """
    notifier = bootstrap.build_question_notifier()
    with patch(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        assert (
            notifier.notify(
                question=_question(),
                kind="question",
                classification="expected",
                code="",
                lines=("a",),
            )
            is None
        )


def test_a_write_that_returns_no_id_is_also_a_failure() -> None:
    """Both spellings of "it did not land" settle the intent the same way."""
    notifier = bootstrap.build_question_notifier()
    with patch(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        return_value=_accepted(None),
    ):
        assert (
            notifier.notify(
                question=_question(),
                kind="question",
                classification="expected",
                code="",
                lines=("a",),
            )
            is None
        )


def test_a_question_with_no_resolvable_seat_fails_without_calling_the_client() -> None:
    """No receiver means no write attempt, and a FAILED intent rather than a crash.

    Unreachable through ``RoundQuestion`` (the model requires a non-empty owner)
    and reachable through the Protocol, which takes ``object``. Worth pinning
    anyway: the branch decides whether an unroutable question is a retryable row
    or an exception out of the ask path.
    """

    class _Ownerless:
        question_id = "Q9"
        owner_conversation = ""

    notifier = bootstrap.build_question_notifier()
    with patch("cli_agent_orchestrator.clients.database.create_inbox_message") as create:
        result = notifier.notify(
            question=_Ownerless(), kind="question", classification="expected", code="", lines=("a",)
        )
    assert result is None
    create.assert_not_called()


def test_a_locked_write_leaves_a_retryable_intent_end_to_end(tmp_path: Path) -> None:
    """The whole path: ask under a locked queue, then sweep once it frees.

    This is the arm that says the two halves fit together — a FAILED intent from
    a real refusal is exactly what ``retry_failed_notices`` picks up, and the
    second attempt settles it SENT with its message id.
    """
    from cli_agent_orchestrator.app.gate.questions import GateQuestionService

    _res, pool = migrate(tmp_path / "gate.db", busy_timeout_ms=5000)
    assert pool is not None
    service = bootstrap.build_gate_question_service(tmp_path / "gate.db", notify=True)
    assert isinstance(service, GateQuestionService)

    with patch(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        question, _replayed = service.ask(
            dispatch_id="w1",
            question="accept?",
            client_request_id="cr1",
            owner_conversation="seat-abc",
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
    # The ask itself stands: the lane is suspended and the question is readable.
    assert service.get(question.question_id) is not None

    with patch(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        return_value=_accepted(77),
    ):
        _expired, retried = service.sweep()
    assert [q.question_id for q in retried] == [question.question_id]
    row = (
        pool.connection()
        .execute(
            "SELECT state, msg_id, attempts FROM question_notice_intent WHERE question_id = ?",
            (question.question_id,),
        )
        .fetchone()
    )
    assert row["state"] == "SENT" and row["msg_id"] == "77" and row["attempts"] == 2


def test_the_notifier_opens_no_second_path_to_the_seat() -> None:
    """No wake, no doorbell, no pane paste — one inbox row and stop.

    Read off the source rather than mocked, because the claim is about what the
    notifier CANNOT do, and a mock only proves what one call did. The delivery
    tick owns waking the seat; a direct call here would be a second, racing
    source of truth about whether the supervisor was told.
    """
    source = Path(bootstrap.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    notifier_class = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "_InboxQuestionNotifier"
    )
    called = {
        ast.unparse(node.func) for node in ast.walk(notifier_class) if isinstance(node, ast.Call)
    }
    body = ast.unparse(notifier_class)
    for forbidden in ("wake_seat", "inject_worker", "doorbell", "send_keys", "paste"):
        assert forbidden not in body, f"the notifier must not reach {forbidden}"
    assert any("create_inbox_message" in name for name in called)


def test_a_legacy_fallback_id_is_not_queue_acceptance() -> None:
    """A legacy row can have an id even though the queue refused the write."""
    notifier = bootstrap.build_question_notifier()
    legacy = InboxInsertResult(_Message(99), InboxInsertDisposition.LEGACY_ACCEPTED)
    with patch(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        return_value=legacy,
    ):
        assert (
            notifier.notify(
                question=_question(),
                kind="question",
                classification="expected",
                code="",
                lines=("a",),
            )
            is None
        )


def test_notice_retries_reuse_one_stable_queue_idempotency_key() -> None:
    notifier = bootstrap.build_question_notifier()
    with patch(
        "cli_agent_orchestrator.clients.database.create_inbox_message",
        side_effect=[_accepted(1), _accepted(1)],
    ) as create:
        for _ in range(2):
            assert (
                notifier.notify(
                    question=_question(),
                    kind="question",
                    classification="expected",
                    code="",
                    lines=("a",),
                )
                == "1"
            )
    keys = [call.kwargs["idempotency_key"] for call in create.call_args_list]
    assert keys[0] == keys[1] == "gate-question-notice:Q1:question:expected"


# ---------------------------------------------------------------------------
# B2 r2: the refusal must stay a refusal, and one question must be one row.
#
# The arms above reach the notifier with ``create_inbox_message`` patched, which
# proves the notifier's own branch but not the thing the review reproduced: the
# REAL write path, with the delivery queue armed, refusing.  The review's repro
# was exactly this shape — ``CAO_DELIVERY_QUEUE=on``, the queue refusing, and the
# intent coming back SENT off the legacy surrogate id.  So these arms run the
# real ``create_inbox_message`` over a real file with both schemas, the delivery
# runtime installed at ``on``, and a real queue store in it.
# ---------------------------------------------------------------------------


class _GateClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@pytest.fixture
def live_notice_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real file, both schemas, a seat, and the delivery queue armed at ``on``."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
    from cli_agent_orchestrator.app.delivery import wiring
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.core.delivery import SwitchPosition

    db_file = tmp_path / "notice.sqlite"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)

    result, pool = migrate(db_file, busy_timeout_ms=5000)
    assert result.ok, result
    assert pool is not None
    with sessions.begin() as db:
        db.add(
            database.TerminalModel(
                id="seat-abc",
                tmux_session="cao-n",
                tmux_window="seat-abc",
                provider="claude_code",
                init_state="ready",
            )
        )

    clock = _GateClock()
    store = SqliteQueueStore(pool, clock=clock)
    wiring.install_delivery(
        wiring.DeliveryRuntime(store=store, clock=clock, position=SwitchPosition.ON)
    )
    try:
        yield pool
    finally:
        wiring.reset_delivery()
        pool.close_all()
        engine.dispose()


def _count(pool: Any, table: str) -> int:
    return int(pool.connection().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_a_queue_refusal_is_never_reported_as_sent(live_notice_env: Any) -> None:
    """B2.1, on the real path: a refused queue write is not acceptance.

    The review's repro, reproduced as an arm.  With the queue armed at ``on`` and
    the write-through refusing (``database is locked``), the legacy fallback still
    writes an ``inbox`` row with an integer id — and inferring acceptance from
    that id is exactly the defect.  The notifier must answer ``None``, so the
    intent settles FAILED and the sweep retries, rather than claiming the seat was
    told when the queue never took the row.
    """
    pool = live_notice_env
    notifier = bootstrap.build_question_notifier()
    with patch(
        "cli_agent_orchestrator.services.queue_carrier.write_through_enqueue",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        msg_id = notifier.notify(
            question=_question(), kind="question", classification="expected", code="", lines=("a",)
        )

    assert msg_id is None, "a queue refusal carries no message id"
    assert _count(pool, "delivery_msg") == 0, "the queue did not take the row"
    assert (
        _count(pool, "inbox") == 1
    ), "the legacy fallback wrote one — and must not be read as SENT"


def test_a_typed_queue_refusal_is_also_never_reported_as_sent(live_notice_env: Any) -> None:
    """The other spelling of a refusal: the write-through returns REFUSED itself.

    The arm above covers the exceptional fallback; this one covers the ordinary
    typed refusal the delivery wiring produces when the queue write fails. Both
    must reach the notifier as "the queue did not take it", and neither may be
    inferred from the legacy surrogate id the fallback then writes.

    MUTANT: accept any ``InboxInsertResult`` regardless of disposition, or read
    ``message.message.id`` instead of ``carrier_id`` — both re-admit the defect.
    """
    pool = live_notice_env
    notifier = bootstrap.build_question_notifier()
    from cli_agent_orchestrator.app.delivery.wiring import (
        WriteThroughDisposition,
        WriteThroughResult,
    )

    with patch(
        "cli_agent_orchestrator.app.delivery.wiring.write_through",
        return_value=WriteThroughResult(WriteThroughDisposition.REFUSED),
    ):
        msg_id = notifier.notify(
            question=_question(), kind="question", classification="expected", code="", lines=("a",)
        )

    assert msg_id is None
    assert _count(pool, "inbox") == 1, "the legacy fallback is the carrier it degraded to"
    assert _count(pool, "delivery_msg") == 0


def test_the_accepted_path_writes_one_queue_row_and_no_legacy_row(live_notice_env: Any) -> None:
    """The positive control for the arm above: same call, queue healthy.

    Without this, "the refusal returns None" could be satisfied by a notifier that
    never accepts anything.
    """
    pool = live_notice_env
    notifier = bootstrap.build_question_notifier()
    msg_id = notifier.notify(
        question=_question(), kind="question", classification="expected", code="", lines=("a", "b")
    )
    assert msg_id is not None
    assert _count(pool, "delivery_msg") == 1
    assert _count(pool, "inbox") == 0


def test_a_retry_after_a_lost_claim_writes_no_second_queue_row(live_notice_env: Any) -> None:
    """B2.2's second window, end to end.

    The review's crash window: the queue ACCEPTS, then the bookkeeping that would
    mark the intent SENT loses its lock.  The intent stays retryable and the sweep
    calls the notifier again — so the only thing standing between that and a
    duplicate delivery is the queue seeing the SAME key twice.  Here the same
    question is notified twice through the real notifier and the real queue store;
    one row must exist, and both calls must come away with the same id.
    """
    pool = live_notice_env
    notifier = bootstrap.build_question_notifier()

    first = notifier.notify(
        question=_question(), kind="question", classification="expected", code="", lines=("a",)
    )
    retry = notifier.notify(
        question=_question(), kind="question", classification="expected", code="", lines=("a",)
    )

    assert first is not None and first == retry, "one logical notice, one queue identity"
    assert _count(pool, "delivery_msg") == 1, "the retry must not enqueue a second delivery"


def test_the_write_through_outcome_is_typed_all_the_way_to_the_caller(
    live_notice_env: Any,
) -> None:
    """Every branch of the queue seam is TYPED, on the real path.

    The notifier can only refuse a fallback if the caller is told WHICH fallback
    it got, so the chain is asserted at the seam itself: the accepted path, the
    delivery-wiring refusal, and a failure of the bridge INTO the wiring module
    all have to arrive as three distinguishable ``InboxInsertDisposition``
    values. The reported defect was a bare ``None``/legacy id, which is why the
    notifier's own arms cannot stand alone here — both non-accepted branches make
    the notifier answer ``None``, and a chain that collapsed them would look
    identical from there.

    MUTANT: ``queue_carrier.write_through_enqueue``'s except branch (the bridge
    could not reach the wiring at all) and the choke point's refused conversion
    are each mutated to their "legacy accepted" spelling.
    """
    from cli_agent_orchestrator.clients.database import (
        InboxInsertDisposition,
        InboxInsertResult,
        create_inbox_message,
    )

    pool = live_notice_env

    accepted = create_inbox_message(
        sender_id="cao-gate", receiver_id="seat-abc", message="a", return_outcome=True
    )
    assert isinstance(accepted, InboxInsertResult)
    assert accepted.disposition is InboxInsertDisposition.QUEUE_ACCEPTED
    assert accepted.carrier_id is not None

    # The queue store refuses: the wiring's own typed REFUSED, converted by the
    # choke point into the caller-visible QUEUE_REFUSED.
    with patch(
        "cli_agent_orchestrator.services.queue_carrier.write_through_enqueue",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        refused = create_inbox_message(
            sender_id="cao-gate", receiver_id="seat-abc", message="b", return_outcome=True
        )
    assert isinstance(refused, InboxInsertResult)
    assert refused.disposition is InboxInsertDisposition.QUEUE_REFUSED
    assert refused.carrier_id is None

    # The BRIDGE fails before it can even hand the fact over: still REFUSED, and
    # never "not attempted" — the queue was the active carrier either way.
    with patch(
        "cli_agent_orchestrator.app.delivery.wiring.write_through",
        side_effect=RuntimeError("bridge down"),
    ):
        bridged = create_inbox_message(
            sender_id="cao-gate", receiver_id="seat-abc", message="c", return_outcome=True
        )
    assert isinstance(bridged, InboxInsertResult)
    assert bridged.disposition is InboxInsertDisposition.QUEUE_REFUSED
    assert bridged.disposition is not InboxInsertDisposition.LEGACY_ACCEPTED
    assert _count(pool, "delivery_msg") == 1, "only the accepted arm reached the queue"
