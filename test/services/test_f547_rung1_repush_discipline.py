"""F547 #403: rung1 re-ring discipline (delivery_service surfaces).

Contract points covered here:
  * Point 2 — escalating backoff after a DELIVERED rung1 (60s → 5m → 30m, cap),
    re-ring text carries attempt count + unacked age (never byte-identical to
    the legacy first-ring text).
  * Point 3 — HOLD before a rung1 ring while the seat is blocked
    (WAITING_USER_ANSWER / compacting / API-retry); one consolidated ring on
    unblock listing all pending row ids.
  * Point 4 — rung1 success records f459.socket_delivered for the row.

Each test carries a mutation note: which reverted line makes it fail.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    DeliveryObligationModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    _utcnow,
)
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import delivery_service
from cli_agent_orchestrator.services.delivery_service import (
    LadderResult,
    _drive_one_obligation,
    _rung1_backoff_seconds,
    _rung1_repush_body,
    attempt_rung1,
)


@pytest.fixture
def ds_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", TestSession)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession
    )
    return TestSession


def _seed(db, *, tid="sup1", mb="mb1"):
    db.add(
        TerminalModel(
            id=tid,
            tmux_session="cao-test",
            tmux_window=tid,
            provider="claude_code",
            agent_profile="supervisor",
        )
    )
    db.add(
        MailboxModel(
            id=mb,
            session_name="cao-test",
            role="supervisor",
            current_terminal_id=tid,
            generation=1,
            consumed_through_id=0,
        )
    )
    db.add(MailboxIncarnationModel(mailbox_id=mb, generation=1, terminal_id=tid))


def _add_inbox(db, *, row_id, mailbox_id="mb1", status="pending", receiver_id="sup1"):
    db.add(
        InboxModel(
            id=row_id,
            sender_id="worker01",
            receiver_id=receiver_id,
            logical_receiver_id=mailbox_id,
            message="hello",
            status=status,
            orchestration_type=OrchestrationType.SEND_MESSAGE.value,
        )
    )


def _add_obl(db, *, row_id, mailbox_id="mb1", next_attempt_at=None):
    now = _utcnow()
    db.add(
        DeliveryObligationModel(
            inbox_row_id=row_id,
            mailbox_id=mailbox_id,
            state="OPEN",
            accepted_at=now,
            next_attempt_at=next_attempt_at if next_attempt_at is not None else now,
            attempts=0,
        )
    )


def _delivered_traces(db, row_id):
    return (
        db.query(InboxMessageTraceEventModel.id)
        .filter(
            InboxMessageTraceEventModel.message_id == row_id,
            InboxMessageTraceEventModel.kind == "f459.socket_delivered",
        )
        .count()
    )


def _live_target(**overrides):
    from cli_agent_orchestrator.services.delivery_service import DeliveryTarget

    base = dict(
        terminal_id="sup1",
        tmux_session="cao-test",
        tmux_window="supervisor",
        cc_inbox_path="/tmp/f547-inbox/team-lead.json",
        has_registry=True,
        liveness="presumed_live",
    )
    base.update(overrides)
    return DeliveryTarget(**base)


# ---------------------------------------------------------------------------
# Point 2: escalating backoff
# ---------------------------------------------------------------------------


def test_backoff_ladder_steps_and_caps():
    """1st delivered → 60s, 2nd → 300s, 3rd → 1800s, 4th+ → 1800s (cap).

    Mutation: replace `_rung1_backoff_seconds` body with `return tick_s` (the
    old re-arm) or make idx clamp to 0 → the ladder collapses → fail.
    """
    assert _rung1_backoff_seconds(1) == 60.0
    assert _rung1_backoff_seconds(2) == 300.0
    assert _rung1_backoff_seconds(3) == 1800.0
    assert _rung1_backoff_seconds(4) == 1800.0  # cap
    assert _rung1_backoff_seconds(99) == 1800.0


def test_repush_body_carries_attempt_and_age_and_ids():
    """Re-ring text carries the re-push count, unacked minutes, and pending ids,
    and is NOT the legacy first-ring line.

    Mutation: revert _rung1_repush_body to return the legacy
    "[cao] Callback from ... Run any command to surface and ack it." text →
    the 're-push'/'unacked' assertions fail.
    """
    body = _rung1_repush_body(3, 41 * 60, [7, 9, 11])
    assert "re-push 3" in body
    assert "unacked 41m" in body
    assert "7,9,11" in body
    assert "Callback from" not in body  # not the legacy first-ring text


def test_delivered_rung1_reschedules_on_backoff_not_tick(ds_db):
    """After a delivered rung1, next_attempt_at is placed on the backoff (>=60s),
    NOT re-armed at now+tick_s (5s).

    Mutation: restore `obl.next_attempt_at = now + timedelta(seconds=tick_s)`
    in the r1.delivered branch → next_attempt_at is ~5s out → fail.
    """
    now = _utcnow()
    with ds_db() as db:
        _seed(db)
        _add_inbox(db, row_id=1)
        _add_obl(db, row_id=1, next_attempt_at=now)
        db.commit()

    with (
        patch.object(
            delivery_service,
            "attempt_rung1",
            return_value=LadderResult(True, "transport_attempt", "proceed", None),
        ),
        patch.object(delivery_service, "resolve_supervisor_target", return_value=_live_target()),
        patch.object(delivery_service, "_receiver_hold_reason", return_value=None),
        # attempt_rung1 is mocked, so record the socket_delivered marker ourselves
        # to drive the backoff ladder to step 1 (60s).
        patch.object(delivery_service, "_socket_delivered_count", side_effect=[0, 1]),
    ):
        with ds_db() as db:
            obl = db.query(DeliveryObligationModel).one()
            _drive_one_obligation(db, obl, now, 3600.0, "shadow")
            db.commit()
            refreshed = db.query(DeliveryObligationModel).one()
            nxt = refreshed.next_attempt_at
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            assert nxt >= now + timedelta(seconds=60)


def test_first_ring_uses_legacy_text_repush_uses_count(ds_db):
    """First ring (delivered_count 0) passes message_body=None (legacy text);
    a subsequent ring (delivered_count>0) passes a 're-push' body.

    Mutation: drop the `if delivered_count > 0:` guard so repush_body is always
    None → the second call's message_body is None → the 're-push' assertion
    fails.
    """
    now = _utcnow()
    captured = {}

    def _fake_rung1(target, row_id, *, oldest_age_s=0.0, message_body=None):
        captured["body"] = message_body
        return LadderResult(True, "transport_attempt", "proceed", None)

    # First ring: no prior deliveries.
    with ds_db() as db:
        _seed(db)
        _add_inbox(db, row_id=1)
        _add_obl(db, row_id=1, next_attempt_at=now)
        db.commit()
    with (
        patch.object(delivery_service, "attempt_rung1", side_effect=_fake_rung1),
        patch.object(delivery_service, "resolve_supervisor_target", return_value=_live_target()),
        patch.object(delivery_service, "_receiver_hold_reason", return_value=None),
        patch.object(delivery_service, "_socket_delivered_count", side_effect=[0, 1]),
    ):
        with ds_db() as db:
            obl = db.query(DeliveryObligationModel).one()
            _drive_one_obligation(db, obl, now, 3600.0, "shadow")
    assert captured["body"] is None  # legacy first-ring text path

    # Re-ring: one prior delivery recorded.
    with (
        patch.object(delivery_service, "attempt_rung1", side_effect=_fake_rung1),
        patch.object(delivery_service, "resolve_supervisor_target", return_value=_live_target()),
        patch.object(delivery_service, "_receiver_hold_reason", return_value=None),
        patch.object(delivery_service, "_socket_delivered_count", side_effect=[1, 2]),
    ):
        with ds_db() as db:
            obl = db.query(DeliveryObligationModel).one()
            _drive_one_obligation(db, obl, now, 3600.0, "shadow")
    assert captured["body"] is not None
    assert "re-push" in captured["body"]


# ---------------------------------------------------------------------------
# Point 3: HOLD while blocked
# ---------------------------------------------------------------------------


def test_hold_waiting_user_answer_skips_ring_keeps_obligation(ds_db):
    """When the seat is WAITING_USER_ANSWER, rung1 is NOT attempted and the
    obligation stays OPEN (re-checked next tick).

    Mutation: delete the `if hold_reason is not None:` early-return block in
    _drive_one_obligation → attempt_rung1 is called despite the hold → the
    rung1.assert_not_called() fails.
    """
    now = _utcnow()
    with ds_db() as db:
        _seed(db)
        _add_inbox(db, row_id=1)
        _add_obl(db, row_id=1, next_attempt_at=now)
        db.commit()

    with (
        patch.object(delivery_service, "attempt_rung1") as rung1,
        patch.object(delivery_service, "resolve_supervisor_target", return_value=_live_target()),
        patch.object(delivery_service, "_receiver_hold_reason", return_value="waiting_user_answer"),
    ):
        with ds_db() as db:
            obl = db.query(DeliveryObligationModel).one()
            _drive_one_obligation(db, obl, now, 3600.0, "shadow")
            db.commit()
            refreshed = db.query(DeliveryObligationModel).one()
            assert refreshed.state == "OPEN"
    rung1.assert_not_called()


def test_receiver_hold_reason_waiting():
    """_receiver_hold_reason returns 'waiting_user_answer' for a WAITING seat.

    Mutation: change the compared status in _receiver_hold_reason from
    TerminalStatus.WAITING_USER_ANSWER to IDLE → returns None → fail.
    """
    with patch(
        "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
        return_value=TerminalStatus.WAITING_USER_ANSWER,
    ):
        assert delivery_service._receiver_hold_reason("sup1") == "waiting_user_answer"


def test_receiver_hold_reason_pane_marker(monkeypatch):
    """When status is not WAITING but the pane tail shows 'Compacting', hold.

    Mutation: remove the pane-tail marker loop in _receiver_hold_reason (return
    None after the WAITING check) → returns None for the compacting seat → fail.
    """
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            return_value=TerminalStatus.PROCESSING,
        ),
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_buffer",
            return_value="... some output ...\nCompacting conversation ...\n",
        ),
        patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            side_effect=lambda path, default=None, override=None: default,
        ),
    ):
        assert delivery_service._receiver_hold_reason("sup1") == "pane_marker:compacting"


def test_receiver_hold_reason_none_when_idle(monkeypatch):
    """A healthy idle seat with a clean pane is NOT held.

    Mutation: invert the WAITING comparison (`!=`) → an idle seat is treated as
    held → returns a reason → fail.
    """
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status",
            return_value=TerminalStatus.IDLE,
        ),
        patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_buffer",
            return_value="all good, ready\n",
        ),
        patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            side_effect=lambda path, default=None, override=None: default,
        ),
    ):
        assert delivery_service._receiver_hold_reason("sup1") is None


def test_consolidated_pending_ids_on_repush(ds_db):
    """A post-hold re-ring lists ALL pending row ids for the mailbox, not just
    the obligation's own row.

    Mutation: make _pending_row_ids_for_mailbox return only the obligation row
    (e.g. `[obl.inbox_row_id]`) → the body would miss rows 2 and 3 → fail.
    """
    now = _utcnow()
    captured = {}

    def _fake_rung1(target, row_id, *, oldest_age_s=0.0, message_body=None):
        captured["body"] = message_body
        return LadderResult(True, "transport_attempt", "proceed", None)

    with ds_db() as db:
        _seed(db)
        _add_inbox(db, row_id=1)
        _add_inbox(db, row_id=2)
        _add_inbox(db, row_id=3)
        _add_obl(db, row_id=1, next_attempt_at=now)
        db.commit()

    with (
        patch.object(delivery_service, "attempt_rung1", side_effect=_fake_rung1),
        patch.object(delivery_service, "resolve_supervisor_target", return_value=_live_target()),
        patch.object(delivery_service, "_receiver_hold_reason", return_value=None),
        patch.object(delivery_service, "_socket_delivered_count", side_effect=[1, 2]),
    ):
        with ds_db() as db:
            obl = db.query(DeliveryObligationModel).one()
            _drive_one_obligation(db, obl, now, 3600.0, "shadow")

    assert captured["body"] is not None
    assert "1,2,3" in captured["body"]


# ---------------------------------------------------------------------------
# Point 4: socket_delivered on rung1 success
# ---------------------------------------------------------------------------


def test_rung1_rang_records_socket_delivered(ds_db, tmp_path, monkeypatch):
    """A 'rang' rung1 writes an f459.socket_delivered trace for the row.

    Mutation: delete the `_mark_socket_delivered(inbox_row_id)` call in the
    attempt_rung1 'rang' branch → no trace row is written → count 0 → fail.
    """
    inbox = tmp_path / "inbox" / "team-lead.json"
    inbox.parent.mkdir(parents=True)
    target = _live_target(cc_inbox_path=str(inbox))
    with (
        patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            return_value=True,
        ),
        patch(
            "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring",
            return_value="rang",
        ),
        patch(
            "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending",
            return_value=True,
        ),
    ):
        with ds_db() as db:
            # _mark_socket_delivered uses SessionLocal — patched to ds_db via fixture.
            result = attempt_rung1(target, 55)
            assert result.delivered is True
        with ds_db() as db:
            assert _delivered_traces(db, 55) == 1


def test_rung1_non_rang_records_no_socket_delivered(ds_db, tmp_path):
    """A deferred rung1 does NOT write a socket_delivered trace.

    Mutation: move the _mark_socket_delivered call outside the `if result ==
    "rang"` branch → a deferred ring would still record delivery → count 1 →
    fail.
    """
    inbox = tmp_path / "inbox" / "team-lead.json"
    inbox.parent.mkdir(parents=True)
    target = _live_target(cc_inbox_path=str(inbox))
    with (
        patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            return_value=True,
        ),
        patch(
            "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring",
            return_value="wake_unverified",
        ),
        patch(
            "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending",
            return_value=True,
        ),
    ):
        with ds_db() as db:
            result = attempt_rung1(target, 56)
            assert result.delivered is False
        with ds_db() as db:
            assert _delivered_traces(db, 56) == 0
