"""Tests for F77 — lifecycle-pointer family fixes (FAM-1/2/3 + migration)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as database_client
from cli_agent_orchestrator.clients.database import (
    Base,
    CallbackBarrierMemberModel,
    CallbackBarrierModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    ProviderSessionModel,
    TerminalModel,
    _close_barrier_owner_gone_in_db,
    _fire_open_barrier_in_db,
    _migrate_f77_lifecycle_pointers,
    _resolve_barrier_owner_or_none,
    cancel_callback_barrier,
    delete_terminal_and_warm_intent,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType


@pytest.fixture
def test_db(monkeypatch):
    """In-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", TestSession)
    return TestSession


def _make_terminal(db, terminal_id, session_name="s1"):
    t = TerminalModel(
        id=terminal_id,
        tmux_session=session_name,
        tmux_window="w0",
        provider="kiro_cli",
        agent_profile="developer",
        lifecycle_generation=1,
    )
    db.add(t)
    db.flush()
    return t


def _make_mailbox(db, mailbox_id, terminal_id, session_name="s1"):
    m = MailboxModel(
        id=mailbox_id,
        session_name=session_name,
        role="supervisor",
        current_terminal_id=terminal_id,
        generation=1,
    )
    db.add(m)
    db.flush()
    return m


def _make_barrier(
    db,
    *,
    owner_terminal_id=None,
    owner_mailbox_id=None,
    state="OPEN",
    label="test-barrier",
    timeout_seconds=300,
):
    b = CallbackBarrierModel(
        owner_terminal_id=owner_terminal_id,
        owner_mailbox_id=owner_mailbox_id,
        owner_generation=1,
        label=label,
        state=state,
        timeout_at=datetime.now() + timedelta(seconds=timeout_seconds),
        created_at=datetime.now(),
    )
    db.add(b)
    db.flush()
    return b


def _make_member(db, barrier_id, terminal_id, position=0, state="AWAITING", member_key="k0"):
    m = CallbackBarrierMemberModel(
        barrier_id=barrier_id,
        member_key=member_key,
        position=position,
        terminal_id=terminal_id,
        lifecycle_generation=1,
        state=state,
    )
    db.add(m)
    db.flush()
    return m


class TestFAM1MailboxNullification:
    """AC1: deleting a terminal nullifies its mailbox authority."""

    def test_delete_terminal_nullifies_mailbox_current_terminal_id(self, test_db):
        """Delete a terminal that is mailboxes.current_terminal_id → NULL."""
        with test_db.begin() as db:
            _make_terminal(db, "owner1")
            _make_mailbox(db, "mbox1", "owner1")

        delete_terminal_and_warm_intent("owner1")

        with test_db() as db:
            mailbox = db.query(MailboxModel).filter_by(id="mbox1").one()
            assert mailbox.current_terminal_id is None

    def test_delete_terminal_does_not_affect_other_mailboxes(self, test_db):
        """Deleting terminal X doesn't nullify unrelated mailboxes."""
        with test_db.begin() as db:
            _make_terminal(db, "t1")
            _make_terminal(db, "t2")
            _make_mailbox(db, "mbox1", "t1", session_name="s1")
            _make_mailbox(db, "mbox2", "t2", session_name="s2")

        delete_terminal_and_warm_intent("t1")

        with test_db() as db:
            mbox2 = db.query(MailboxModel).filter_by(id="mbox2").one()
            assert mbox2.current_terminal_id == "t2"


class TestFAM2ResolverLivenessCheck:
    """AC3: mailbox-owned barrier with dead terminal → None (owner_gone)."""

    def test_resolve_returns_none_when_mailbox_terminal_is_dead(self, test_db):
        """Mailbox points at dead terminal → resolver returns None."""
        with test_db() as db:
            # Mailbox points at a terminal that doesn't exist
            m = MailboxModel(
                id="mbox-dead",
                session_name="s1",
                role="supervisor",
                current_terminal_id="dead_terminal",
                generation=1,
            )
            db.add(m)
            b = CallbackBarrierModel(
                owner_mailbox_id="mbox-dead",
                owner_terminal_id=None,
                owner_generation=1,
                label="b1",
                state="OPEN",
                timeout_at=datetime.now() + timedelta(seconds=300),
                created_at=datetime.now(),
            )
            db.add(b)
            db.flush()

            result = _resolve_barrier_owner_or_none(db, b)
            assert result is None

    def test_resolve_returns_none_when_mailbox_has_no_incarnation(self, test_db):
        """Mailbox with current_terminal_id=NULL → resolver returns None."""
        with test_db() as db:
            m = MailboxModel(
                id="mbox-null",
                session_name="s1",
                role="supervisor",
                current_terminal_id=None,
                generation=1,
            )
            db.add(m)
            b = CallbackBarrierModel(
                owner_mailbox_id="mbox-null",
                owner_terminal_id=None,
                owner_generation=1,
                label="b2",
                state="OPEN",
                timeout_at=datetime.now() + timedelta(seconds=300),
                created_at=datetime.now(),
            )
            db.add(b)
            db.flush()

            result = _resolve_barrier_owner_or_none(db, b)
            assert result is None

    def test_resolve_succeeds_with_live_terminal(self, test_db):
        """Mailbox with live terminal → returns (terminal_id, mailbox_id)."""
        with test_db() as db:
            _make_terminal(db, "live_t")
            m = MailboxModel(
                id="mbox-live",
                session_name="s1",
                role="supervisor",
                current_terminal_id="live_t",
                generation=1,
            )
            db.add(m)
            b = CallbackBarrierModel(
                owner_mailbox_id="mbox-live",
                owner_terminal_id=None,
                owner_generation=1,
                label="b3",
                state="OPEN",
                timeout_at=datetime.now() + timedelta(seconds=300),
                created_at=datetime.now(),
            )
            db.add(b)
            db.flush()

            result = _resolve_barrier_owner_or_none(db, b)
            assert result == ("live_t", "mbox-live")


class TestFAM3TerminalizeAwaitingMembers:
    """AC4/5/6: non-complete barrier close → AWAITING members become FAILED."""

    def test_fire_timeout_terminalizes_awaiting_members(self, test_db):
        """AC4: timeout fires → AWAITING member becomes FAILED(barrier_closed_timeout)."""
        with test_db.begin() as db:
            _make_terminal(db, "owner_t")
            _make_terminal(db, "worker_t")
            barrier = _make_barrier(db, owner_terminal_id="owner_t")
            _make_member(db, barrier.id, "owner_t", position=0, state="ARRIVED", member_key="k0")
            _make_member(db, barrier.id, "worker_t", position=1, state="AWAITING", member_key="k1")

            _fire_open_barrier_in_db(db, barrier, state="FIRED_TIMEOUT", close_reason="timeout")

        with test_db() as db:
            members = (
                db.query(CallbackBarrierMemberModel)
                .order_by(CallbackBarrierMemberModel.position)
                .all()
            )
            arrived = [m for m in members if m.state == "ARRIVED"]
            failed = [m for m in members if m.state == "FAILED"]
            assert len(arrived) == 1
            assert len(failed) == 1
            assert failed[0].failure_class == "barrier_closed_timeout"

    def test_cancel_terminalizes_awaiting_members(self, test_db):
        """AC5: cancel → AWAITING member becomes FAILED(barrier_closed_cancel)."""
        with test_db.begin() as db:
            _make_terminal(db, "owner_c")
            _make_terminal(db, "worker_c1")
            _make_terminal(db, "worker_c2")
            barrier = _make_barrier(db, owner_terminal_id="owner_c", label="cancel-test")
            _make_member(db, barrier.id, "worker_c1", position=0, state="AWAITING", member_key="w1")
            _make_member(db, barrier.id, "worker_c2", position=1, state="AWAITING", member_key="w2")

        result = cancel_callback_barrier(barrier_id=1)
        assert result["state"] == "CANCELLED"

        with test_db() as db:
            members = db.query(CallbackBarrierMemberModel).filter_by(barrier_id=1).all()
            assert all(m.state == "FAILED" for m in members)
            assert all(m.failure_class == "barrier_closed_cancel" for m in members)

    def test_owner_gone_terminalizes_awaiting_members(self, test_db):
        """AC6: owner deleted → AWAITING members FAILED(barrier_owner_gone)."""
        with test_db.begin() as db:
            _make_terminal(db, "gone_owner")
            _make_terminal(db, "gone_w1")
            _make_terminal(db, "gone_w2")
            barrier = _make_barrier(db, owner_terminal_id="gone_owner")
            _make_member(db, barrier.id, "gone_w1", position=0, state="AWAITING", member_key="m0")
            _make_member(db, barrier.id, "gone_w2", position=1, state="AWAITING", member_key="m1")

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            _close_barrier_owner_gone_in_db(db, barrier, now)

        with test_db() as db:
            members = db.query(CallbackBarrierMemberModel).all()
            assert all(m.state == "FAILED" for m in members)
            assert all(m.failure_class == "barrier_owner_gone" for m in members)

    def test_fire_does_not_overwrite_already_failed_members(self, test_db):
        """Members already FAILED (e.g. quota_or_auth) keep their failure_class."""
        with test_db.begin() as db:
            _make_terminal(db, "owner_f")
            _make_terminal(db, "worker_f")
            barrier = _make_barrier(db, owner_terminal_id="owner_f")
            _make_member(db, barrier.id, "owner_f", position=0, state="ARRIVED", member_key="k0")
            m = _make_member(
                db, barrier.id, "worker_f", position=1, state="FAILED", member_key="k1"
            )
            m.failure_class = "quota_or_auth"

            _fire_open_barrier_in_db(db, barrier, state="FIRED_TIMEOUT", close_reason="timeout")

        with test_db() as db:
            member = db.query(CallbackBarrierMemberModel).filter_by(member_key="k1").one()
            assert member.failure_class == "quota_or_auth"  # NOT overwritten


class TestFAM8Exemptions:
    """AC8: exempt rows (provider_sessions, mailbox_incarnations) untouched."""

    def test_provider_session_with_dead_source_terminal_survives_delete(self, test_db):
        """provider_sessions READY rows with dead source_terminal_id survive."""
        with test_db.begin() as db:
            _make_terminal(db, "src_t")
            ps = ProviderSessionModel(
                name="my-base",
                provider="kiro_cli",
                session_uuid="uuid1",
                cwd="/tmp",
                agent_profile="developer",
                dirty_hashes="{}",
                status="ready",
                kind="base",
                source_terminal_id="src_t",
            )
            db.add(ps)

        # Delete the source terminal
        delete_terminal_and_warm_intent("src_t")

        with test_db() as db:
            ps = db.query(ProviderSessionModel).filter_by(name="my-base").one()
            assert ps.source_terminal_id == "src_t"  # untouched
            assert ps.status == "ready"

    def test_mailbox_incarnation_historical_rows_survive_delete(self, test_db):
        """mailbox_incarnations historical rows survive terminal deletion."""
        with test_db.begin() as db:
            _make_terminal(db, "inc_t")
            _make_mailbox(db, "mbox_inc", "inc_t")
            inc = MailboxIncarnationModel(
                mailbox_id="mbox_inc",
                terminal_id="inc_t",
                generation=1,
                published_at=datetime.now(timezone.utc),
            )
            db.add(inc)

        delete_terminal_and_warm_intent("inc_t")

        with test_db() as db:
            rows = db.query(MailboxIncarnationModel).all()
            assert len(rows) == 1
            assert rows[0].terminal_id == "inc_t"  # untouched


class TestMigrationF77:
    """Test the idempotent startup migration."""

    def test_migration_fixes_stranded_awaiting_members(self, test_db):
        """AWAITING members under closed barriers → FAILED(barrier_closed_historical)."""
        with test_db.begin() as db:
            _make_terminal(db, "t_mig")
            barrier = _make_barrier(db, owner_terminal_id="t_mig", state="FIRED_TIMEOUT")
            _make_member(db, barrier.id, "t_mig", state="AWAITING", member_key="stale1")

        _migrate_f77_lifecycle_pointers()

        with test_db() as db:
            m = db.query(CallbackBarrierMemberModel).filter_by(member_key="stale1").one()
            assert m.state == "FAILED"
            assert m.failure_class == "barrier_closed_historical"

    def test_migration_fixes_stranded_pending_messages(self, test_db):
        """PENDING inbox rows to dead receiver via mailbox → DELIVERY_FAILED."""
        with test_db.begin() as db:
            _make_terminal(db, "sender_t")
            # No terminal for "dead_recv" — it's dead
            _make_mailbox(db, "mbox_recv", None, session_name="s1")
            row = InboxModel(
                sender_id="sender_t",
                receiver_id="dead_recv",
                logical_receiver_id="mbox_recv",
                message="stranded msg",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(row)

        _migrate_f77_lifecycle_pointers()

        with test_db() as db:
            msg = db.query(InboxModel).one()
            assert msg.status == MessageStatus.DELIVERY_FAILED.value
            assert msg.failure_reason == "receiver_gone_historical"

    def test_migration_is_idempotent(self, test_db):
        """Running migration twice doesn't error or double-update."""
        with test_db.begin() as db:
            _make_terminal(db, "t_idem")
            barrier = _make_barrier(db, owner_terminal_id="t_idem", state="CANCELLED")
            _make_member(db, barrier.id, "t_idem", state="AWAITING", member_key="idem1")

        _migrate_f77_lifecycle_pointers()
        _migrate_f77_lifecycle_pointers()  # second run should be no-op

        with test_db() as db:
            m = db.query(CallbackBarrierMemberModel).filter_by(member_key="idem1").one()
            assert m.state == "FAILED"
            assert m.failure_class == "barrier_closed_historical"

    def test_migration_does_not_touch_open_barrier_members(self, test_db):
        """AWAITING members under OPEN barriers are left alone."""
        with test_db.begin() as db:
            _make_terminal(db, "t_open")
            barrier = _make_barrier(db, owner_terminal_id="t_open", state="OPEN")
            _make_member(db, barrier.id, "t_open", state="AWAITING", member_key="still_open")

        _migrate_f77_lifecycle_pointers()

        with test_db() as db:
            m = db.query(CallbackBarrierMemberModel).filter_by(member_key="still_open").one()
            assert m.state == "AWAITING"
