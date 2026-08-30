"""F578 D23 — create_inbox_message_endpoint forwards expire_after_s /
supersede_key to the producer (AC10 endpoint hop).

Empirical gate r1 B2: the endpoint had no expire_after_s / supersede_key query
params, so the fields could not reach create_inbox_message. These tests pin the
forwarding on BOTH endpoint branches (direct terminal + mb_ logical) by mocking
the producer and the terminal-existence guards; the DB seam itself is covered by
test/clients/test_inbox_expire_supersede.py.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def client():
    from test.api.conftest import TestClientWithHost

    with patch("cli_agent_orchestrator.api.main.status_monitor"):
        from cli_agent_orchestrator.api.main import app

        return TestClientWithHost(app)


def _fake_inbox_msg():
    from datetime import datetime, timezone

    msg = MagicMock()
    msg.id = 7
    msg.sender_id = "abcd1111"
    msg.receiver_id = "abcd0000"
    msg.created_at = datetime(2026, 8, 30, tzinfo=timezone.utc)
    return msg


def test_endpoint_forwards_controls_direct_terminal_path(client):
    import cli_agent_orchestrator.api.main as main

    with (
        patch.dict(os.environ, {"CAO_SENDER_TOKEN_DISABLED": "1"}, clear=False),
        patch.object(main, "require_input_allowed"),
        patch.object(
            main, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
        ),
        patch.object(main, "get_backend") as backend,
        patch.object(main, "create_inbox_message", return_value=_fake_inbox_msg()) as producer,
        patch.object(main.inbox_service, "deliver_pending"),
        patch("cli_agent_orchestrator.services.inbox_service.request_delivery"),
    ):
        backend.return_value.session_exists.return_value = True
        backend.return_value.get_history.return_value = ""
        resp = client.post(
            "/terminals/abcd0000/inbox/messages",
            params={
                "sender_id": "abcd1111",
                "message": "ephemeral",
                "expire_after_s": 5,
                "supersede_key": "status",
            },
        )

    assert resp.status_code == 200, resp.text
    assert producer.call_args.kwargs["expire_after_s"] == 5
    assert producer.call_args.kwargs["supersede_key"] == "status"


def test_endpoint_omits_controls_when_unset_direct_path(client):
    import cli_agent_orchestrator.api.main as main

    with (
        patch.dict(os.environ, {"CAO_SENDER_TOKEN_DISABLED": "1"}, clear=False),
        patch.object(main, "require_input_allowed"),
        patch.object(
            main, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
        ),
        patch.object(main, "get_backend") as backend,
        patch.object(main, "create_inbox_message", return_value=_fake_inbox_msg()) as producer,
        patch.object(main.inbox_service, "deliver_pending"),
        patch("cli_agent_orchestrator.services.inbox_service.request_delivery"),
    ):
        backend.return_value.session_exists.return_value = True
        backend.return_value.get_history.return_value = ""
        resp = client.post(
            "/terminals/abcd0000/inbox/messages",
            params={"sender_id": "abcd1111", "message": "ordinary"},
        )

    assert resp.status_code == 200, resp.text
    assert "expire_after_s" not in producer.call_args.kwargs
    assert "supersede_key" not in producer.call_args.kwargs


def test_endpoint_forwards_controls_logical_mailbox_path(client):
    import cli_agent_orchestrator.api.main as main

    with (
        patch.dict(os.environ, {"CAO_SENDER_TOKEN_DISABLED": "1"}, clear=False),
        patch(
            "cli_agent_orchestrator.services.mailbox_service.create_logical_inbox_message",
            return_value=_fake_inbox_msg(),
        ) as producer,
        patch.object(main.inbox_service, "deliver_pending"),
    ):
        resp = client.post(
            "/terminals/mb_abcd0000/inbox/messages",
            params={
                "sender_id": "abcd1111",
                "message": "status v2",
                "expire_after_s": 9,
                "supersede_key": "k",
            },
        )

    assert resp.status_code == 200, resp.text
    assert producer.call_args.kwargs["expire_after_s"] == 9
    assert producer.call_args.kwargs["supersede_key"] == "k"
