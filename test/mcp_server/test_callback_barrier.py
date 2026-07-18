"""MCP callback-barrier tools and send-message tag threading."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server import server


def test_typed_selector_requires_exactly_one(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    assert server._barrier_params(7, None) == {"barrier_id": 7, "owner": "aaaaaaaa"}
    assert server._barrier_params(None, "123") == {
        "barrier_label": "123",
        "owner": "aaaaaaaa",
    }
    with pytest.raises(ValueError):
        server._barrier_params(None, None)
    with pytest.raises(ValueError):
        server._barrier_params(7, "seven")


@pytest.mark.asyncio
async def test_barrier_status_and_cancel_call_same_api_contract(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"id": 4, "state": "OPEN"}
    with patch.object(server.cao_http, "get", return_value=response) as get:
        result = await server.barrier_status(barrier_id=None, barrier_label="gate")
    assert result == {"id": 4, "state": "OPEN"}
    assert get.call_args.kwargs["params"] == {
        "barrier_label": "gate",
        "owner": "aaaaaaaa",
    }
    response.json.return_value = {"id": 4, "state": "CANCELLED"}
    with patch.object(server.cao_http, "post", return_value=response) as post:
        result = await server.cancel_barrier(barrier_id=4, barrier_label=None)
    assert result["state"] == "CANCELLED"
    assert post.call_args.kwargs["params"] == {"barrier_id": 4, "owner": "aaaaaaaa"}


def test_worker_cannot_create_callback_barrier_via_send_message(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "worker-terminal")
    target = MagicMock()
    target.raise_for_status.return_value = None
    target.json.return_value = {
        "id": "peer-worker",
        "caller_id": "supervisor-terminal",
        "caller_mailbox_id": None,
    }
    with (
        patch.object(server.cao_http, "get", return_value=target),
        patch.object(server, "_send_to_inbox") as send,
    ):
        result = server._send_message_impl(
            "peer-worker",
            "task",
            barrier="worker-created",
            barrier_timeout_seconds=90,
            barrier_member_key="lane-a",
        )
    assert result == {
        "success": False,
        "error": "callback barriers require supervisor ownership of the receiver",
    }
    send.assert_not_called()


def test_supervisor_can_create_callback_barrier_for_owned_worker(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "supervisor-terminal")
    target = MagicMock()
    target.raise_for_status.return_value = None
    target.json.return_value = {
        "id": "owned-worker",
        "caller_id": "supervisor-terminal",
        "caller_mailbox_id": None,
    }
    with (
        patch.object(server.cao_http, "get", return_value=target),
        patch.object(server, "_send_to_inbox", return_value={"success": True}) as send,
    ):
        result = server._send_message_impl("owned-worker", "task", barrier="gate")
    assert result == {"success": True}
    assert send.call_args.kwargs["barrier"] == "gate"


def test_send_message_threads_barrier_params_without_none(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"success": True}
    with patch.object(server.cao_http, "post", return_value=response) as post:
        server._send_to_inbox(
            "bbbbbbbb",
            "task",
            barrier="gate",
            barrier_timeout_seconds=90,
            barrier_member_key="reviewer-a",
        )
    assert post.call_args.kwargs["params"] == {
        "sender_id": "aaaaaaaa",
        "message": "task",
        "refresh_ingest": False,
        "barrier": "gate",
        "barrier_timeout_seconds": 90,
        "barrier_member_key": "reviewer-a",
    }
