"""WP-MAILBOX-CHANNEL acceptance tests (AC#1-AC#9).

The durable supervisor mailbox: its rows, its incarnations, its ack watermark
and its quarantine. What this file no longer covers is the FLAG the feature
shipped behind.

WP-ARCH 3c K2/K8 deleted ``supervisor.mailbox_pull`` together with the helper
that read it (``mailbox_service.is_supervisor_mailbox_pull_terminal``) and the
legacy pusher it selected between. The mailbox itself is untouched -- the rows
are still durable, ``list_messages``/``ack_messages`` are still the drain, the
watermark still settles exactly once -- so every arm about the STORE survives
here unchanged. The arms about the SWITCH are re-pointed at
``mailbox_service.probe_supervisor_role``, the fail-closed role probe that the
delivery gate actually consults now, or deleted where the switch was the whole
subject; each deletion is recorded in place below.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptMemberModel,
    InboxDeliveryAttemptModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    get_pending_messages,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.mailbox_service import (
    MailboxDomainError,
    ack_messages,
    list_messages,
    probe_supervisor_role,
    quarantine_malformed_mailbox_rows,
)


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mailbox_pull.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _terminal(db, terminal_id: str, session: str = "cao-test") -> None:
    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session=session,
            tmux_window=terminal_id,
            provider="claude_code",
            agent_profile="chao_supervisor",
            init_state="ready",
        )
    )


def _mailbox(db, terminal_id: str = "sup-001", *, generation: int = 1) -> MailboxModel:
    # WP-ARCH 3c K8 deleted ``MailboxModel.schema_version``; the ``schema_version``
    # keyword this helper used to accept went with it. No caller ever passed a
    # value other than the default, because the only arm that varied it was AC#7
    # (see its note below), so nothing about the rows these tests build changes.
    row = MailboxModel(
        id="mb_sup",
        session_name="cao-test",
        role="supervisor",
        current_terminal_id=terminal_id,
        generation=generation,
        consumed_through_id=0,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(row)
    db.add(
        MailboxIncarnationModel(
            mailbox_id=row.id,
            generation=generation,
            terminal_id=terminal_id,
            published_at=datetime.now(),
        )
    )
    return row


def _inbox_row(
    db,
    receiver: str,
    *,
    logical: str | None = None,
    status: str = "pending",
    sender: str = "worker-001",
    message: str = "hello from worker",
    created_at: datetime | None = None,
) -> InboxModel:
    row = InboxModel(
        sender_id=sender,
        receiver_id=receiver,
        logical_receiver_id=logical,
        enqueue_generation=1,
        message=message,
        orchestration_type="send_message",
        status=status,
        created_at=created_at or datetime.now(),
    )
    db.add(row)
    db.flush()
    return row


def _seat_delivery_patches():
    """The patch set every deliver_pending arm below shares."""
    return (
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
            return_value={
                "tmux_session": "cao-test",
                "tmux_window": "sup-001",
                "lifecycle_generation": 1,
                "recovery_state": None,
            },
        ),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", MagicMock()),
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager", MagicMock()),
    )


def _bare_service() -> InboxService:
    svc = InboxService.__new__(InboxService)
    svc._gone_lock = threading.Lock()
    svc._gone_streaks = {}
    svc._tnf_lock = threading.Lock()
    svc._terminal_not_found_streaks = {}
    return svc


# ---------------------------------------------------------------------------
# AC#1 — the gate holds the row by ROLE
# ---------------------------------------------------------------------------


def test_ac1_gate_holds_the_row_by_role(scratch_db):
    """WP-ARCH 3b / A1.5 INVERTED this case, and the inversion is the decision.

    AC#1 used to prove that with ``supervisor.mailbox_pull`` off, a supervisor
    mailbox terminal fell through the gate and took the push path. That
    fall-through is the path that produced the pasted ``[Message from ...]``
    blocks in the seat's composer (#613, emitter 3), and the user ended it.

    The gate asks the fail-closed ROLE probe, so the ban holds under every switch
    position alike -- muting follows the position and the ban does not. A
    config-gated ban is exactly what F210 declined to build when it made the
    rung-2 exemption role-based, and WP-ARCH 3c then deleted the flag outright,
    which is why the flag half of this arm is gone and the probe half is all
    that is left to assert.

    What is asserted: the gate is REACHED and reports SUPERVISOR, and the row is
    held PENDING for the seat to drain rather than settled by the gate.
    """
    with scratch_db.begin() as db:
        _terminal(db, "sup-001")
        _mailbox(db)
        row = _inbox_row(db, "sup-001", logical="mb_sup")
        row_id = row.id

    # Trace the predicate the gate actually consults.
    gate_called = []
    original_fn = probe_supervisor_role

    def traced_fn(tid):
        result = original_fn(tid)
        gate_called.append((tid, result))
        return result

    meta, monitor, pm = _seat_delivery_patches()
    with (
        patch(
            "cli_agent_orchestrator.services.mailbox_service.probe_supervisor_role",
            traced_fn,
        ),
        meta,
        monitor,
        pm,
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service",
            MagicMock(),
        ),
    ):
        _bare_service().deliver_pending("sup-001")

    # The gate was reached and reported SUPERVISOR, so the row was short-circuited
    # by role rather than falling through to the composer.
    assert any(tid == "sup-001" and result is True for tid, result in gate_called)
    # And it is held PENDING for the seat to drain, not settled by the gate.
    with scratch_db() as db:
        msg = db.get(InboxModel, row_id)
        assert msg.status == MessageStatus.PENDING.value


# ---------------------------------------------------------------------------
# AC#2 — the ban leaves the push machinery untouched
# ---------------------------------------------------------------------------


def test_ac2_seat_row_never_opens_an_attempt_or_a_send(scratch_db):
    """deliver_pending on a supervisor mailbox terminal returns WITHOUT opening a
    delivery attempt or calling send_prepared_input; the row stays PENDING.

    AC#2 asked this of the ``supervisor.mailbox_pull=true`` position. WP-ARCH 3c
    deleted the flag, so the question it was asking about ONE position is now the
    unconditional contract, and the arm asserts it with no flag set at all. The
    two negatives are load-bearing rather than vacuous: ``begin_delivery_attempt``
    is the row that would make the push auditable and ``send_prepared_input`` is
    the keystroke itself, so between them they pin that the gate returns before
    the seam and not merely that the row survived.
    """
    with scratch_db.begin() as db:
        _terminal(db, "sup-001")
        _mailbox(db)
        row = _inbox_row(db, "sup-001", logical="mb_sup")
        row_id = row.id

    meta, monitor, pm = _seat_delivery_patches()
    with (
        meta,
        patch(
            "cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt",
        ) as mock_attempt,
        monitor,
        pm,
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service",
            MagicMock(),
        ) as mock_ts,
    ):
        _bare_service().deliver_pending("sup-001")

    # Push path NOT exercised
    mock_attempt.assert_not_called()
    mock_ts.send_prepared_input.assert_not_called()

    # Row stays PENDING
    with scratch_db() as db:
        msg = db.get(InboxModel, row_id)
        assert msg.status == MessageStatus.PENDING.value


# ---------------------------------------------------------------------------
# AC#3 — ack settles drained rows, exactly-once (CAS race with ThreadPoolExecutor)
# ---------------------------------------------------------------------------


def test_ac3_ack_settles_drained_rows_exactly_once(scratch_db):
    """After delivery + ack_messages(up_to_id), drained rows are DELIVERED
    with failure_reason=mailbox_pull_acked. Concurrent ack has exactly one winner."""
    with scratch_db.begin() as db:
        _terminal(db, "sup-001")
        _mailbox(db)
        r1 = _inbox_row(db, "sup-001", logical="mb_sup", message="msg1")
        r2 = _inbox_row(db, "sup-001", logical="mb_sup", message="msg2")
        up_to = r2.id

    # Ack settles both rows
    result = ack_messages("sup-001", up_to)
    assert result["changed"] is True
    assert result["consumed_through_id"] == up_to
    # F413: settled_count now includes both inbox rows (2) and delivery
    # obligations settled to ACKED. The ORM listener creates obligations for
    # qualifying PENDING supervisor-directed rows when the mailbox is visible
    # at flush time. In this test, one obligation is created (r2 gets one
    # because the mailbox is flushed by the time r2 is added).
    assert result["settled_count"] == 3

    # Verify rows are DELIVERED
    with scratch_db() as db:
        for rid in (r1.id, r2.id):
            row = db.get(InboxModel, rid)
            assert row.status == MessageStatus.DELIVERED.value
            assert row.failure_reason == "mailbox_pull_acked"

    # A second deliver_pending does NOT re-push (get_pending_messages excludes DELIVERED)
    pending = get_pending_messages("sup-001", limit=100)
    assert len(pending) == 0

    # Concurrent ack race: exactly one winner
    with scratch_db.begin() as db:
        r3 = _inbox_row(db, "sup-001", logical="mb_sup", message="msg3")
        race_id = r3.id

    results = []
    errors = []

    def try_ack():
        try:
            r = ack_messages("sup-001", race_id)
            results.append(r)
        except MailboxDomainError as e:
            errors.append(e)
        except Exception as e:
            errors.append(e)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(try_ack) for _ in range(4)]
        for f in futures:
            f.result()

    # At least one succeeded, total successes + errors = 4
    assert len(results) + len(errors) == 4
    # At least one succeeded
    assert len(results) >= 1
    # The row is settled
    with scratch_db() as db:
        row = db.get(InboxModel, race_id)
        assert row.status == MessageStatus.DELIVERED.value


# ---------------------------------------------------------------------------
# AC#4 — a superseded incarnation is not the seat
# ---------------------------------------------------------------------------


def test_ac4_superseded_incarnation_is_not_the_seat(scratch_db):
    """The gate answers for the CURRENT incarnation only.

    AC#4 asked this of ``is_supervisor_mailbox_pull_terminal``: a terminal whose
    mailbox has moved on is not in pull mode, so delivery fell back to push. The
    helper is deleted and the question moved intact to ``probe_supervisor_role``,
    which resolves the mailbox by ``current_terminal_id`` -- so the stale
    generation-1 terminal answers False and the live generation-2 terminal
    answers True.

    The property matters as much under the ban as it did under the flag: it is
    what keeps the ban scoped to the live seat instead of to every pane that was
    ever published as one. Both directions are asserted, because a probe that
    answered True for everything would pass the first assertion alone.
    """
    with scratch_db.begin() as db:
        _terminal(db, "sup-001")
        _terminal(db, "sup-002")
        # Mailbox exists but current_terminal_id points to sup-002 (not sup-001)
        mb = MailboxModel(
            id="mb_sup",
            session_name="cao-test",
            role="supervisor",
            current_terminal_id="sup-002",  # superseded — sup-001 is stale
            generation=2,
            consumed_through_id=0,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        db.add(mb)
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_sup",
                generation=1,
                terminal_id="sup-001",
                published_at=datetime.now(),
            )
        )
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_sup",
                generation=2,
                terminal_id="sup-002",
                published_at=datetime.now(),
            )
        )
        _inbox_row(db, "sup-001", logical="mb_sup", message="fallback msg")

    # The stale incarnation is not the seat — the gate does not protect it.
    assert probe_supervisor_role("sup-001") is False

    # Contrast: sup-002 (the current incarnation) IS the seat.
    assert probe_supervisor_role("sup-002") is True


# ---------------------------------------------------------------------------
# AC#5 — malformed row quarantined, settled DELIVERY_FAILED
# ---------------------------------------------------------------------------


def test_ac5_malformed_row_quarantined(scratch_db):
    """A row whose body fails validation is quarantined as DELIVERY_FAILED /
    mailbox_payload_malformed; no attempt row is left unsettled."""
    with scratch_db.begin() as db:
        _terminal(db, "sup-001")
        _mailbox(db)
        # Valid row
        good = _inbox_row(db, "sup-001", logical="mb_sup", message="valid msg")
        # Malformed: empty message
        bad = _inbox_row(db, "sup-001", logical="mb_sup", message="")
        good_id, bad_id = good.id, bad.id

    count = quarantine_malformed_mailbox_rows("mb_sup")
    assert count == 1

    with scratch_db() as db:
        good_row = db.get(InboxModel, good_id)
        bad_row = db.get(InboxModel, bad_id)
        # Good row untouched
        assert good_row.status == MessageStatus.PENDING.value
        # Bad row quarantined
        assert bad_row.status == MessageStatus.DELIVERY_FAILED.value
        assert bad_row.failure_reason == "mailbox_payload_malformed"


# ---------------------------------------------------------------------------
# AC#6 — drain surface is the existing list/ack
# ---------------------------------------------------------------------------


def test_ac6_drain_via_existing_list_ack(scratch_db):
    """list_messages + ack_messages drive D2 end-to-end with no new CLI/tool."""
    with scratch_db.begin() as db:
        _terminal(db, "sup-001")
        _mailbox(db)
        r1 = _inbox_row(db, "sup-001", logical="mb_sup", message="drain me")
        row_id = r1.id

    # list_messages returns the PENDING row (use mailbox id as receiver)
    msgs = list_messages("mb_sup")
    assert any(m["id"] == row_id for m in msgs["items"])

    # ack settles it
    result = ack_messages("sup-001", row_id)
    assert result["changed"] is True

    # After ack, list_messages no longer shows it as PENDING
    msgs_after = list_messages("mb_sup", status=MessageStatus.PENDING)
    assert not any(m["id"] == row_id for m in msgs_after["items"])


# ---------------------------------------------------------------------------
# AC#7 — REMOVED by WP-ARCH 3c K8.
#
# ``test_ac7_schema_version_default_and_compatibility`` pinned a compatibility
# refusal: a mailbox stamped with a schema_version the drain does not understand
# fell out of pull mode, and ``is_supervisor_mailbox_pull_terminal`` was where
# that comparison lived. K8 rewrote the surviving probe as
# ``is_supervisor_role_terminal`` and its docstring states the drop as a ruling,
# not an accident -- "a schema-mismatched supervisor is still a supervisor" --
# because a version check that can silently un-protect the seat's pane is a
# precondition that can re-arm composer injection, which is the class of thing
# K8 exists to remove.
#
# So the arm's subject is gone in both halves, and K8's follow-through removed
# the field from ``MailboxModel`` as well: nothing reads OR writes
# ``schema_version`` now. The physical column and its ``DEFAULT 1`` are
# deliberately left in place -- it is NOT NULL with a server default, so an
# INSERT that omits it still succeeds and old databases stay readable -- but
# there is no consumer left to refuse anything. Re-pointing the arm at the
# column's default alone would assert that a SQLAlchemy default is its own
# default, which pins no behaviour at all; if a future slice gives the field a
# reader, the refusal it implements is what earns a new arm here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC#8 — the reconcile sweep does not re-drive the seat into a push
# ---------------------------------------------------------------------------


def test_ac8_reconciliation_sweep_does_not_fight_the_seat(scratch_db):
    """A PENDING supervisor row older than the reconcile grace is re-gated (no
    push). A row YOUNGER than INBOX_RECONCILE_GRACE_SECONDS is never passed to
    deliver_pending by the reconcile sweep.

    AC#8 named "pull mode" because the flag was what made the seat's rows sit
    PENDING long enough for the sweep to see them. WP-ARCH 3c deleted the flag
    and the role ban makes those rows sit there unconditionally, so the sweep now
    meets them on EVERY deployment rather than on a flagged one -- which makes
    this arm more load-bearing after the deletion, not less.

    Test-comment note (empirical N1): the sweep query also JOINs on terminal
    existence in addition to the age filter — a seat row on a live terminal
    still appears once past the grace, and the re-driven deliver_pending no-ops
    via the gate; the join is a secondary filter, not a protection of its own.
    """
    from cli_agent_orchestrator.clients.database import list_pending_receiver_ids_older_than
    from cli_agent_orchestrator.services.inbox_service import INBOX_RECONCILE_GRACE_SECONDS

    old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        seconds=INBOX_RECONCILE_GRACE_SECONDS + 60
    )
    with scratch_db.begin() as db:
        _terminal(db, "sup-001")
        _mailbox(db)
        # Old row (past grace) — should appear in reconcile sweep
        old_row = _inbox_row(
            db, "sup-001", logical="mb_sup", message="old msg", created_at=old_time
        )
        # Young row (within grace) — should NOT appear
        young_row = _inbox_row(
            db,
            "sup-001",
            logical="mb_sup",
            message="young msg",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        old_id, young_id = old_row.id, young_row.id

    # The older_than filter excludes young rows
    receiver_ids = list_pending_receiver_ids_older_than(INBOX_RECONCILE_GRACE_SECONDS)
    # If sup-001 appears, it's because the OLD row qualifies
    assert "sup-001" in receiver_ids
    # The young row alone should NOT trigger inclusion
    with scratch_db.begin() as db:
        # Remove the old row to test young-only
        db.query(InboxModel).filter_by(id=old_id).delete()

    receiver_ids_young_only = list_pending_receiver_ids_older_than(INBOX_RECONCILE_GRACE_SECONDS)
    assert "sup-001" not in receiver_ids_young_only
    assert young_id is not None

    # Re-add old row and verify deliver_pending no-ops via the role gate (no push)
    with scratch_db.begin() as db:
        _inbox_row(db, "sup-001", logical="mb_sup", message="re-old", created_at=old_time)

    meta, monitor, pm = _seat_delivery_patches()
    with (
        meta,
        patch(
            "cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt",
        ) as mock_attempt,
        monitor,
        pm,
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service",
            MagicMock(),
        ) as mock_ts,
    ):
        _bare_service().deliver_pending("sup-001")

    # The role gate skipped the push — no attempt opened
    mock_attempt.assert_not_called()
    mock_ts.send_prepared_input.assert_not_called()

    # Rows still PENDING (waiting for the seat's own drain)
    with scratch_db() as db:
        pending = (
            db.query(InboxModel)
            .filter_by(receiver_id="sup-001", status=MessageStatus.PENDING.value)
            .all()
        )
        assert len(pending) >= 1


# ---------------------------------------------------------------------------
# AC#9 — prior push-era attempt settled by ack, never dangling
# ---------------------------------------------------------------------------


def test_ac9_prior_push_era_attempt_settled_by_ack(scratch_db):
    """A row with an existing OPEN attempt from a pre-flag-flip push era, now acked
    by the seat, has that attempt settled confirmed via the D2 safety net.
    No attempt row is left with settled_at=NULL."""
    with scratch_db.begin() as db:
        _terminal(db, "sup-001")
        _mailbox(db)
        row = _inbox_row(db, "sup-001", logical="mb_sup", message="push-era msg")
        row_id = row.id

        # Simulate a pre-flag-flip push-era open attempt
        attempt_uuid = str(uuid.uuid4())
        db.add(
            InboxDeliveryAttemptModel(
                attempt_uuid=attempt_uuid,
                receiver_terminal_id="sup-001",
                provider="claude_code",
                outcome=None,
                reason=None,
                settled_at=None,
                payload_hash="fakehash123",
                payload_length=17,
                sender_id="worker-001",
                orchestration_type="send_message",
                started_at=datetime.now(),
                last_at=datetime.now(),
            )
        )
        db.add(
            InboxDeliveryAttemptMemberModel(
                attempt_uuid=attempt_uuid,
                message_id=row_id,
                position=0,
            )
        )

    # Ack the row
    result = ack_messages("sup-001", row_id)
    assert result["changed"] is True

    # Verify the attempt is settled (not dangling)
    with scratch_db() as db:
        attempt = db.query(InboxDeliveryAttemptModel).filter_by(attempt_uuid=attempt_uuid).one()
        assert attempt.settled_at is not None
        assert attempt.outcome == "confirmed"
        assert attempt.reason == "mailbox_pull_acked"

        # The row itself is DELIVERED
        msg = db.get(InboxModel, row_id)
        assert msg.status == MessageStatus.DELIVERED.value
        assert msg.failure_reason == "mailbox_pull_acked"


# ---------------------------------------------------------------------------
# P0 hotfix (2026-08-09) — REMOVED by WP-ARCH 3c K8.
#
# ``test_p0_hotfix_supervisor_row_pushes_when_receiver_idle`` asserted the exact
# behaviour K8 forbids: with the flag off, the gate resolved False and a
# supervisor-addressed row was allowed to reach the push path. It was written as
# a #123 regression guard at a moment when the seat's callbacks arrived only by
# paste, and the fix it guarded is the emitter #613 later named.
#
# Both of its premises are now gone. There is no flag to read off, and there is
# no False answer to observe -- ``probe_supervisor_role`` is fail-closed, so a
# supervisor-role receiver answers True and an unanswerable probe answers True
# as well. Keeping the arm would require asserting that the seat CAN be pasted
# into, which is the one outcome the phase exists to make unreachable; inverting
# it in place would just duplicate AC#1 and AC#2 above, which already pin the
# True answer and the untouched push machinery. The callback the hotfix cared
# about is delivered by the native channel and drained by list/ack, covered by
# AC#6.
# ---------------------------------------------------------------------------


def test_p0_hotfix_list_messages_since_utc_returns_row_created_now(scratch_db):
    """F130 surface: list_messages with an aware-UTC `since` (e.g. the ISO the
    supervisor passes) returns a row created "now" (UTC). The stored created_at
    is written timezone-aware UTC and the since filter is normalized to
    aware-UTC, so the comparison is correct."""
    with scratch_db.begin() as db:
        _terminal(db, "sup-001")
        _mailbox(db)
        row = _inbox_row(
            db,
            "sup-001",
            logical="mb_sup",
            message="F130 UTC row",
            created_at=datetime.now(timezone.utc),
        )
        row_id = row.id

    # since = a moment before the row was created, expressed in aware UTC.
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    result = list_messages("mb_sup", since=since)
    ids = [item["id"] for item in result["items"]]
    assert row_id in ids
