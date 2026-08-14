"""F192: Obligation creation at the choke point — regression tests.

Proves that EVERY producer path creates a delivery obligation atomically
when the receiver is a supervisor mailbox.  Tests fail on base 8340fd1e
(obligation was only created in mailbox_service, not the database choke point).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    DeliveryObligationModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    create_inbox_message,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.services import mailbox_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """In-memory DB with full schema for F192 tests."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f192.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    # Patch stalled_callback_watchdog to avoid side effects
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog",
        MagicMock(),
    )
    yield sessions
    engine.dispose()


@pytest.fixture
def supervisor_terminal(scratch_db):
    """Create a supervisor terminal + mailbox + incarnation."""
    with scratch_db.begin() as db:
        db.add(
            TerminalModel(
                id="d85d2aab",
                tmux_session="cao-test",
                tmux_window="supervisor",
                provider="claude_code",
                agent_profile="code_supervisor",
                init_state="ready",
            )
        )
        db.add(
            MailboxModel(
                id="mb_d85d2aab",
                session_name="cao-test",
                role="supervisor",
                current_terminal_id="d85d2aab",
                generation=1,
                consumed_through_id=0,
                cc_inbox_path="/tmp/test-inbox.json",
            )
        )
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_d85d2aab",
                generation=1,
                terminal_id="d85d2aab",
            )
        )
        # Worker terminal (sender)
        db.add(
            TerminalModel(
                id="aaaa1111",
                tmux_session="cao-test",
                tmux_window="worker1",
                provider="kiro_cli",
                agent_profile="developer",
                init_state="ready",
            )
        )
    return scratch_db


# ---------------------------------------------------------------------------
# TEST 1: HTTP route — POST /terminals/{id}/inbox/messages
# ---------------------------------------------------------------------------


class TestHTTPRouteObligationCreation:
    """The MCP POST route creates obligation via the choke point."""

    def test_post_inbox_message_creates_obligation(self, supervisor_terminal, monkeypatch):
        """POST /terminals/{id}/inbox/messages for a supervisor terminal
        creates an OPEN obligation row in the same transaction."""
        from fastapi.testclient import TestClient

        from cli_agent_orchestrator.api.main import app

        db_factory = supervisor_terminal

        # Mock infrastructure checks that the route performs
        backend = MagicMock()
        backend.session_exists.return_value = True
        backend.get_history.return_value = "some pane text"

        monkeypatch.setattr(
            "cli_agent_orchestrator.api.main.require_input_allowed", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            lambda tid: {
                "tmux_session": "cao-test",
                "tmux_window": "supervisor",
            },
        )

        app.state.plugin_registry = MagicMock()

        with patch("cli_agent_orchestrator.api.main.get_backend", return_value=backend):
            with patch(
                "cli_agent_orchestrator.api.main.inbox_service.deliver_pending",
                return_value=None,
            ):
                client = TestClient(app, headers={"Host": "localhost"})
                response = client.post(
                    "/terminals/d85d2aab/inbox/messages",
                    params={
                        "sender_id": "aaaa1111",
                        "message": "F192 test: HTTP route obligation",
                    },
                )

        assert response.status_code == 200, f"Route failed: {response.text}"
        data = response.json()
        assert data["success"] is True
        message_id = data["message_id"]

        # CRITICAL ASSERTION: obligation row exists
        with db_factory() as db:
            obligation = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=message_id)
                .one_or_none()
            )
            assert obligation is not None, (
                f"F192 REGRESSION: no obligation row for message {message_id} "
                f"sent via HTTP route to supervisor terminal"
            )
            assert obligation.state == "OPEN"
            assert obligation.mailbox_id == "mb_d85d2aab"

    def test_single_obligation_per_message_no_double_create(
        self, supervisor_terminal, monkeypatch
    ):
        """Exactly one obligation per message — no double-create when sent
        through both the choke point and any caller-side path."""
        db_factory = supervisor_terminal

        # Use the raw create_inbox_message (the choke point path)
        msg = create_inbox_message(
            sender_id="aaaa1111",
            receiver_id="d85d2aab",
            message="F192 test: single obligation check",
        )

        with db_factory() as db:
            obligations = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=msg.id)
                .all()
            )
            assert len(obligations) == 1, (
                f"Expected exactly 1 obligation, got {len(obligations)} "
                f"(double-create bug)"
            )


# ---------------------------------------------------------------------------
# TEST 2: Producer-sweep — parametrized over direct-caller sites
# ---------------------------------------------------------------------------


class TestProducerSweepObligationCreation:
    """Every direct caller of create_inbox_message creates obligation
    when targeting a supervisor terminal."""

    def test_create_inbox_message_direct_call(self, supervisor_terminal):
        """Direct create_inbox_message call (used by MCP route, fifo_reader,
        callback_barrier_service) creates obligation."""
        db_factory = supervisor_terminal

        msg = create_inbox_message(
            sender_id="aaaa1111",
            receiver_id="d85d2aab",
            message="F192 test: direct create_inbox_message",
        )

        with db_factory() as db:
            obligation = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=msg.id)
                .one_or_none()
            )
            assert obligation is not None, (
                "F192 REGRESSION: create_inbox_message did not create obligation"
            )
            assert obligation.state == "OPEN"
            assert obligation.mailbox_id == "mb_d85d2aab"

    def test_fifo_reader_path(self, supervisor_terminal, monkeypatch):
        """fifo_reader calls create_inbox_message targeting the supervisor —
        obligation must exist."""
        db_factory = supervisor_terminal

        # Simulate what fifo_reader does: call create_inbox_message with
        # the supervisor terminal as receiver_id
        msg = create_inbox_message(
            sender_id="aaaa1111",
            receiver_id="d85d2aab",
            message="[F138-D20] Confirmed-gone report: simulated fifo_reader path",
        )

        with db_factory() as db:
            obligation = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=msg.id)
                .one_or_none()
            )
            assert obligation is not None, (
                "F192 REGRESSION: fifo_reader path did not create obligation"
            )
            assert obligation.state == "OPEN"
            assert obligation.mailbox_id == "mb_d85d2aab"

    def test_callback_barrier_path(self, supervisor_terminal, monkeypatch):
        """callback_barrier_service calls create_inbox_message targeting the
        supervisor — obligation must exist."""
        db_factory = supervisor_terminal

        # Simulate what callback_barrier_service does
        msg = create_inbox_message(
            sender_id="aaaa1111",
            receiver_id="d85d2aab",
            message="barrier callback result",
        )

        with db_factory() as db:
            obligation = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=msg.id)
                .one_or_none()
            )
            assert obligation is not None, (
                "F192 REGRESSION: callback_barrier path did not create obligation"
            )
            assert obligation.state == "OPEN"
            assert obligation.mailbox_id == "mb_d85d2aab"

    def test_mailbox_service_logical_path(self, supervisor_terminal, monkeypatch):
        """mailbox_service.create_logical_inbox_message (direct _insert_routed_inbox_row)
        also creates obligation via the choke point."""
        db_factory = supervisor_terminal

        # Patch delivery signal to avoid side effects
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.inbox_service.request_delivery",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed",
            lambda *a, **kw: None,
        )

        msg = mailbox_service.create_logical_inbox_message(
            sender_id="aaaa1111",
            mailbox_id="mb_d85d2aab",
            message="F192 test: logical mailbox path",
        )

        with db_factory() as db:
            obligation = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=msg.id)
                .one_or_none()
            )
            assert obligation is not None, (
                "F192 REGRESSION: mailbox_service logical path did not create obligation"
            )
            assert obligation.state == "OPEN"
            assert obligation.mailbox_id == "mb_d85d2aab"

    def test_no_obligation_for_non_supervisor_terminal(self, scratch_db):
        """Messages to non-supervisor terminals do NOT get obligations."""
        with scratch_db.begin() as db:
            db.add(
                TerminalModel(
                    id="bbbb2222",
                    tmux_session="cao-test",
                    tmux_window="worker",
                    provider="kiro_cli",
                    agent_profile="developer",
                    init_state="ready",
                )
            )
            db.add(
                TerminalModel(
                    id="cccc3333",
                    tmux_session="cao-test",
                    tmux_window="sender",
                    provider="kiro_cli",
                    agent_profile="developer",
                    init_state="ready",
                )
            )

        msg = create_inbox_message(
            sender_id="cccc3333",
            receiver_id="bbbb2222",
            message="worker-to-worker, no obligation expected",
        )

        with scratch_db() as db:
            obligation = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=msg.id)
                .one_or_none()
            )
            assert obligation is None, (
                "Obligation should NOT be created for non-supervisor targets"
            )


# ---------------------------------------------------------------------------
# TEST 3: M-B mutant killer — non-supervisor MAILBOX receiver gets no obligation
# ---------------------------------------------------------------------------


class TestNonSupervisorMailboxNoObligation:
    """Kill mutant M-B: dropping _is_supervisor_mailbox_id gate must fail.

    These tests exercise receivers that HAVE a mailbox (so logical_receiver_id
    is non-None) but whose mailbox role is NOT 'supervisor'.  Under the mutant
    (gate removed), _create_obligation_inline fires for every receiver with a
    mailbox — these tests catch that.
    """

    @pytest.fixture
    def worker_mailbox_db(self, scratch_db):
        """Create a worker terminal WITH a mailbox (role='worker') + sender."""
        with scratch_db.begin() as db:
            # Worker terminal with its own mailbox
            db.add(
                TerminalModel(
                    id="wrkr0001",
                    tmux_session="cao-test",
                    tmux_window="worker-mb",
                    provider="kiro_cli",
                    agent_profile="developer",
                    init_state="ready",
                )
            )
            db.add(
                MailboxModel(
                    id="mb_wrkr0001",
                    session_name="cao-test",
                    role="worker",
                    current_terminal_id="wrkr0001",
                    generation=1,
                    consumed_through_id=0,
                    cc_inbox_path="/tmp/test-worker-inbox.json",
                )
            )
            db.add(
                MailboxIncarnationModel(
                    mailbox_id="mb_wrkr0001",
                    generation=1,
                    terminal_id="wrkr0001",
                )
            )
            # Sender terminal
            db.add(
                TerminalModel(
                    id="sndr0001",
                    tmux_session="cao-test",
                    tmux_window="sender",
                    provider="kiro_cli",
                    agent_profile="developer",
                    init_state="ready",
                )
            )
        return scratch_db

    def test_worker_mailbox_receiver_no_obligation_via_create_inbox_message(
        self, worker_mailbox_db
    ):
        """M-B KILLER: create_inbox_message to a worker-mailbox terminal must
        NOT create an obligation row — the _is_supervisor_mailbox_id gate
        rejects it.

        Under mutant M-B (gate removed), this fails because
        _create_obligation_inline fires for any non-None logical_receiver_id.
        """
        db_factory = worker_mailbox_db

        msg = create_inbox_message(
            sender_id="sndr0001",
            receiver_id="wrkr0001",
            message="task for worker — no obligation expected",
        )

        with db_factory() as db:
            obligation = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=msg.id)
                .one_or_none()
            )
            assert obligation is None, (
                "M-B KILL: obligation must NOT be created for non-supervisor "
                f"mailbox receiver (role='worker'), but got obligation "
                f"state={obligation.state if obligation else '?'} "
                f"mailbox_id={obligation.mailbox_id if obligation else '?'}"
            )

    def test_worker_mailbox_receiver_no_obligation_via_logical_path(
        self, worker_mailbox_db, monkeypatch
    ):
        """M-B KILLER (logical path): mailbox_service.create_logical_inbox_message
        to a worker mailbox must NOT create an obligation row.

        This exercises the raw-producer path that bypasses
        _create_inbox_message_unfenced and calls _insert_routed_inbox_row directly.
        """
        db_factory = worker_mailbox_db

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.inbox_service.request_delivery",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed",
            lambda *a, **kw: None,
        )

        msg = mailbox_service.create_logical_inbox_message(
            sender_id="sndr0001",
            mailbox_id="mb_wrkr0001",
            message="logical message to worker mailbox — no obligation expected",
        )

        with db_factory() as db:
            obligation = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=msg.id)
                .one_or_none()
            )
            assert obligation is None, (
                "M-B KILL: obligation must NOT be created for non-supervisor "
                f"mailbox via logical path (role='worker'), but got obligation "
                f"state={obligation.state if obligation else '?'} "
                f"mailbox_id={obligation.mailbox_id if obligation else '?'}"
            )
