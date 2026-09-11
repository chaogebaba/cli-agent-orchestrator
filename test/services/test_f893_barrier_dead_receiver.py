"""F893 (#745) H3: a barrier aimed at a DEAD receiver is not an ownership refusal.

Live on grok-box-009 (F880 r1) two of three barrier members dispatched fine from
the supervisor while the third came back "callback barriers require supervisor
ownership of the receiver". The third receiver had 404'd on the send one step
earlier — it had died from the #745 tmux leak — and its ``caller_id`` in the DB
was the dispatching supervisor all along. The boolean permission check collapsed
"receiver is gone" into "you do not own it", and the misdiagnosis cost the round.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import (
    Base,
    TerminalModel,
    callback_barrier_dispatch_allowed,
    callback_barrier_dispatch_permission,
)
from cli_agent_orchestrator.mcp_server import server as mcp_server
from cli_agent_orchestrator.services import callback_barrier_service
from cli_agent_orchestrator.services import inbox_service as inbox_module


@pytest.fixture
def barrier_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f893.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    monkeypatch.setattr("cli_agent_orchestrator.services.mailbox_service.SessionLocal", sessions)
    with sessions.begin() as db:
        for tid, caller, profile in (
            ("sup", None, "supervisor"),
            ("alive", "sup", "reviewer"),
            ("other", "someone-else", "reviewer"),
        ):
            db.add(
                TerminalModel(
                    id=tid,
                    tmux_session="cao-f893",
                    tmux_window=tid,
                    provider="codex",
                    agent_profile=profile,
                    caller_id=caller,
                    lifecycle_generation=1,
                )
            )
    return sessions


class TestPermissionClassification:
    def test_owned_receiver_is_allowed(self, barrier_db):
        assert callback_barrier_dispatch_permission("sup", "alive") == "allowed"

    def test_dead_receiver_is_named_unresolvable_not_unowned(self, barrier_db):
        assert callback_barrier_dispatch_permission("sup", "vanished") == "receiver_unresolvable"

    def test_live_but_foreign_receiver_is_still_not_owned(self, barrier_db):
        assert callback_barrier_dispatch_permission("sup", "other") == "not_owned"

    def test_boolean_wrapper_keeps_its_old_meaning(self, barrier_db):
        assert callback_barrier_dispatch_allowed("sup", "alive") is True
        assert callback_barrier_dispatch_allowed("sup", "other") is False
        assert callback_barrier_dispatch_allowed("sup", "vanished") is False


class TestServiceRefusalText:
    def test_dead_receiver_refusal_says_not_addressable(self, barrier_db, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "sup")
        with pytest.raises(ValueError, match="not addressable") as exc:
            callback_barrier_service.dispatch(
                receiver_id="vanished",
                message="m",
                refresh_ingest=False,
                barrier="b",
                barrier_timeout_seconds=None,
                barrier_member_key=None,
            )
        assert "supervisor ownership" not in str(exc.value)

    def test_foreign_receiver_refusal_still_says_ownership(self, barrier_db, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "sup")
        with pytest.raises(ValueError, match="supervisor ownership"):
            callback_barrier_service.dispatch(
                receiver_id="other",
                message="m",
                refresh_ingest=False,
                barrier="b",
                barrier_timeout_seconds=None,
                barrier_member_key=None,
            )

    def test_owned_receiver_still_dispatches(self, barrier_db, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "sup")
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.callback_barrier_service.require_input_allowed",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(inbox_module, "request_delivery", MagicMock())
        result = callback_barrier_service.dispatch(
            receiver_id="alive",
            message="m",
            refresh_ingest=False,
            barrier="b",
            barrier_timeout_seconds=None,
            barrier_member_key=None,
        )
        assert result["success"] is True
        assert result["receiver_id"] == "alive"


class TestMcpRefusalText:
    def test_mcp_send_message_names_the_dead_receiver(self, barrier_db, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "sup")
        monkeypatch.setattr(mcp_server, "_current_terminal_id", lambda: "sup")
        result = mcp_server._send_message_impl(
            "vanished",
            "task",
            barrier="b",
            barrier_timeout_seconds=90,
            barrier_member_key=None,
        )
        assert result["success"] is False
        assert "not addressable" in result["error"]
        assert "supervisor ownership" not in result["error"]

    def test_mcp_send_message_still_refuses_a_foreign_receiver_as_ownership(
        self, barrier_db, monkeypatch
    ):
        monkeypatch.setenv("CAO_TERMINAL_ID", "sup")
        monkeypatch.setattr(mcp_server, "_current_terminal_id", lambda: "sup")
        result = mcp_server._send_message_impl(
            "other",
            "task",
            barrier="b",
            barrier_timeout_seconds=90,
            barrier_member_key=None,
        )
        assert result["success"] is False
        assert "supervisor ownership" in result["error"]
