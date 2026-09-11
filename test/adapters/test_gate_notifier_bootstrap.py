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
from cli_agent_orchestrator.core import gate as g
from cli_agent_orchestrator.core.ports import QuestionNotifier

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class _Message:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


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
        return_value=_Message(4242),
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
        return_value=_Message(1),
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
        return_value=_Message(1),
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
        return_value=_Message(1),
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
        return_value=_Message(None),  # type: ignore[arg-type]
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
        return_value=_Message(77),
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
