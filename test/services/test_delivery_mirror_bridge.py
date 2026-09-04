"""The legacy-side collector (WP-ARCH phase 3a, F728 #584).

``services/delivery_mirror.py`` is the one legacy module that names the new
tree, so it is the file where a scope error would be invisible from either side:
``app`` cannot see which rows it was handed, and ``clients`` cannot see what was
done with them.  Two things are tested here and nowhere else.

* **The scope predicate.**  Sub-phase 3a mirrors ``send_message`` traffic only.
  Every other enqueue path — the watchdog auto-resume, the barrier escalation,
  the several notices — reaches the same insert seam, so a hook that mirrored
  whatever arrived there would silently widen 3a's scope past what AC-3a
  measures, and the report would compare a population the criterion does not
  describe.
* **The attempt collection.**  ``_collect`` is the one SQLAlchemy join in the
  phase, and the ordinal it assigns becomes the shadow attempt's ``claim_id``.
  A join that lost a member row, or an ordering that was not stable, would make
  re-observing one message write a second set of attempt rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cli_agent_orchestrator.app.delivery import wiring
from cli_agent_orchestrator.app.delivery.facts import LegacyEnqueue
from cli_agent_orchestrator.services import delivery_mirror

AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


@dataclass
class FakeInboxRow:
    """Just enough of ``InboxModel`` for the collector, which uses ``getattr``."""

    id: int = 41
    sender_id: str = "t-worker"
    receiver_id: str = "t-supervisor"
    logical_receiver_id: str | None = "mb_supervisor"
    message: str = "a steer"
    status: str = "pending"
    orchestration_type: str = "send_message"
    created_at: Any = AT
    callback_dedup_key: str | None = None
    expire_after_s: int | None = None
    supersede_key: str | None = None
    park_warm: bool = False
    barrier_id: int | None = None
    barrier_member_key: str | None = None
    enqueue_generation: int | None = 3


class RecordingWiring:
    """Captures what crossed the line, instead of a queue store."""

    def __init__(self) -> None:
        self.enqueued: list[LegacyEnqueue] = []

    def queue_enabled(self) -> bool:
        return True

    def record_enqueue(self, fact: LegacyEnqueue) -> None:
        self.enqueued.append(fact)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> RecordingWiring:
    recording = RecordingWiring()
    monkeypatch.setattr(delivery_mirror, "delivery_wiring", recording)
    return recording


# ------------------------------------------------------------------- the scope


def test_a_send_message_row_is_mirrored(recorder: RecordingWiring) -> None:
    delivery_mirror.record_inbox_row(FakeInboxRow())
    assert [fact.legacy_message_id for fact in recorder.enqueued] == [41]


@pytest.mark.parametrize(
    "orchestration",
    ["assign", "handoff", "watchdog_auto_resume", "barrier_escalation", "", "digest"],
)
def test_every_other_enqueue_path_is_out_of_scope(
    recorder: RecordingWiring, orchestration: str
) -> None:
    """§11's scope line, enforced by a predicate rather than by call-site choice.

    ``assign`` and ``handoff`` are out for a reason worth restating: read at the
    fork's own source neither writes an inbox row at all, so for those two
    write-through would not be a migration of an existing row but a NEW path
    through terminal creation — reaching into the deferred-init failure family
    phase 2 owns, and covered by no acceptance criterion in this blueprint.
    """
    delivery_mirror.record_inbox_row(FakeInboxRow(orchestration_type=orchestration))
    assert recorder.enqueued == []


def test_the_switch_is_checked_before_anything_is_read() -> None:
    """With the queue off the collector does no legacy queries at all.

    A collector that read first and discarded afterwards would put two extra
    queries on the single writer for every message on a server that never opted
    in, which §10 already names as a contention risk.
    """
    wiring.reset_delivery()
    delivery_mirror.record_inbox_row(FakeInboxRow())  # no store installed, no raise


# ------------------------------------------------------------------ the fields


def test_the_receiver_is_the_durable_mailbox_id_when_there_is_one(
    recorder: RecordingWiring,
) -> None:
    """§5 item 2, and the reason #33 becomes closeable.

    A queue row addressed to a TERMINAL id would have to be rewritten every time
    a seat was replaced — which is exactly what legacy's
    ``settle_terminal_fallback`` does today.  Addressed to the mailbox, a fresh
    incarnation inherits its predecessor's pending rows with no rewrite at all.
    """
    delivery_mirror.record_inbox_row(FakeInboxRow())
    assert recorder.enqueued[0].receiver_id == "mb_supervisor"


def test_a_row_with_no_mailbox_falls_back_to_the_terminal_id(
    recorder: RecordingWiring,
) -> None:
    """Not every receiver has a mailbox, and those rows still need mirroring."""
    delivery_mirror.record_inbox_row(FakeInboxRow(logical_receiver_id=None))
    assert recorder.enqueued[0].receiver_id == "t-supervisor"


def test_a_row_with_no_receiver_at_all_is_skipped(recorder: RecordingWiring) -> None:
    delivery_mirror.record_inbox_row(FakeInboxRow(logical_receiver_id=None, receiver_id=""))
    assert recorder.enqueued == []


def test_the_dedup_key_is_what_marks_a_callback(recorder: RecordingWiring) -> None:
    """Legacy's own signal, not a second inference about identity.

    ``callback_dedup_key`` is set only on the F475-eligible path, whose guard
    requires the receiver to be the sender's recorded caller.  Its presence is
    therefore the fork stating that this row is a callback, and re-deriving that
    here would be a second implementation of an identity rule that already
    exists — free to disagree with the one that actually gates the dedup.
    """
    delivery_mirror.record_inbox_row(FakeInboxRow(callback_dedup_key="abc123"))
    assert recorder.enqueued[0].is_callback is True
    assert recorder.enqueued[0].content_hash == "abc123"

    delivery_mirror.record_inbox_row(FakeInboxRow(id=42))
    assert recorder.enqueued[1].is_callback is False


def test_a_naive_timestamp_is_read_as_utc_rather_than_dropping_the_row(
    recorder: RecordingWiring,
) -> None:
    """The queue's models reject a naive datetime, and the driver can return one.

    Every stored stamp in this database is UTC by convention, so reading a naive
    one as UTC is the only interpretation that can be right — and rejecting it
    would drop a message from the comparison for a driver detail.
    """
    naive = datetime(2026, 9, 3, 12, 0)
    delivery_mirror.record_inbox_row(FakeInboxRow(created_at=naive))
    assert recorder.enqueued[0].created_at == naive.replace(tzinfo=UTC)


def test_an_unreadable_row_never_raises_into_the_insert(recorder: RecordingWiring) -> None:
    """A shadow write that could raise into an insert would lose the message."""

    class Exploding:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("column access blew up")

    delivery_mirror.record_inbox_row(Exploding())
    assert recorder.enqueued == []


# ------------------------------------------------------- attempt collection


@pytest.mark.usefixtures("isolated_memory_db")
def test_attempts_are_collected_in_a_stable_order_with_their_reasons() -> None:
    """The one SQLAlchemy join in the phase, against a real schema.

    Ordering by ``(started_at, attempt_uuid)`` rather than by time alone is what
    keeps the numbering stable when two attempts share a timestamp — and the
    ordinal becomes the shadow attempt's ``claim_id``, so an unstable order would
    make re-observing one message write a second set of rows under different ids.
    """
    from cli_agent_orchestrator.clients.database import (
        InboxDeliveryAttemptMemberModel,
        InboxDeliveryAttemptModel,
        InboxModel,
        SessionLocal,
    )

    with SessionLocal() as db:
        row = InboxModel(
            sender_id="t-worker",
            receiver_id="t-supervisor",
            message="m",
            orchestration_type="send_message",
            status="delivered",
        )
        db.add(row)
        db.flush()
        message_id = int(row.id)

        # Two attempts sharing one timestamp, inserted in the reverse of their
        # uuid order, so a naive sort would produce a different numbering.
        for uuid, outcome, reason in (
            ("uuid-b", "confirmed", None),
            ("uuid-a", "interrupted", "proven_absent"),
        ):
            db.add(
                InboxDeliveryAttemptModel(
                    attempt_uuid=uuid,
                    receiver_terminal_id="t-supervisor",
                    provider="claude_code",
                    started_at=AT,
                    outcome=outcome,
                    reason=reason,
                    payload_hash="h",
                    payload_length=1,
                    sender_id="t-worker",
                    orchestration_type="send_message",
                )
            )
            db.add(
                InboxDeliveryAttemptMemberModel(
                    attempt_uuid=uuid, message_id=message_id, position=0
                )
            )
        db.commit()

    collected = delivery_mirror._collect([message_id])
    assert len(collected) == 1
    outcome = collected[0]
    assert outcome.status == "delivered"
    assert [(a.ordinal, a.outcome) for a in outcome.attempts] == [
        (1, "interrupted"),
        (2, "confirmed"),
    ]
    assert outcome.attempts[0].reason == "proven_absent"
    assert outcome.attempts[0].carrier == "claude_code"


@pytest.mark.usefixtures("isolated_memory_db")
def test_an_unsettled_attempt_is_collected_rather_than_skipped() -> None:
    """An attempt with no outcome yet is a fact about the row's history.

    Legacy leaves ``outcome`` NULL until settlement, and a message stuck with an
    open attempt is precisely the #604 shape.  Dropping it would make the row
    look as though nothing had ever been tried.
    """
    from cli_agent_orchestrator.clients.database import (
        InboxDeliveryAttemptMemberModel,
        InboxDeliveryAttemptModel,
        InboxModel,
        SessionLocal,
    )

    with SessionLocal() as db:
        row = InboxModel(
            sender_id="s",
            receiver_id="r",
            message="m",
            orchestration_type="send_message",
            status="delivering",
        )
        db.add(row)
        db.flush()
        message_id = int(row.id)
        db.add(
            InboxDeliveryAttemptModel(
                attempt_uuid="u1",
                receiver_terminal_id="r",
                provider="codex",
                started_at=AT + timedelta(seconds=1),
                payload_hash="h",
                payload_length=1,
                sender_id="s",
                orchestration_type="send_message",
            )
        )
        db.add(
            InboxDeliveryAttemptMemberModel(attempt_uuid="u1", message_id=message_id, position=0)
        )
        db.commit()

    outcome = delivery_mirror._collect([message_id])[0]
    assert outcome.attempts[0].outcome == "unsettled"


@pytest.mark.usefixtures("isolated_memory_db")
def test_collecting_ids_that_do_not_exist_returns_nothing() -> None:
    assert delivery_mirror._collect([9999]) == []
