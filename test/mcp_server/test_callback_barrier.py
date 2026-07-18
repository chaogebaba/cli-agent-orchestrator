"""MCP callback-barrier tools and send-message tag threading."""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.mcp_server import server


def test_typed_selector_requires_exactly_one(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    assert server._barrier_params(7, None) == {"barrier_id": 7, "owner_id": "aaaaaaaa"}
    assert server._barrier_params(None, "123") == {
        "barrier_label": "123",
        "owner_id": "aaaaaaaa",
    }
    with pytest.raises(ValueError):
        server._barrier_params(None, None)
    with pytest.raises(ValueError):
        server._barrier_params(7, "seven")


@pytest.mark.asyncio
async def test_barrier_status_and_cancel_use_principal_bound_internal_seam(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    with (
        patch(
            "cli_agent_orchestrator.services.callback_barrier_service.status",
            return_value={"id": 4, "state": "OPEN"},
        ) as status,
        patch.object(server.cao_http, "get") as get,
    ):
        result = await server.barrier_status(barrier_id=None, barrier_label="gate")
    assert result == {"id": 4, "state": "OPEN"}
    status.assert_called_once_with(
        barrier_label="gate",
        owner_id="aaaaaaaa",
    )
    get.assert_not_called()
    with (
        patch(
            "cli_agent_orchestrator.services.callback_barrier_service.cancel",
            return_value={"id": 4, "state": "CANCELLED", "receiver_ids": []},
        ) as cancel,
        patch.object(server.cao_http, "post") as post,
    ):
        result = await server.cancel_barrier(barrier_id=4, barrier_label=None)
    assert result["state"] == "CANCELLED"
    cancel.assert_called_once_with(
        barrier_id=4,
        owner_id="aaaaaaaa",
    )
    post.assert_not_called()


def test_worker_cannot_create_callback_barrier_via_send_message(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "worker-terminal")
    with (
        patch.object(server, "_barrier_dispatch_is_supervisor_owned", return_value=False),
        patch.object(server, "_send_barrier_to_inbox") as send,
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
    with (
        patch.object(server, "_barrier_dispatch_is_supervisor_owned", return_value=True),
        patch.object(server, "_send_barrier_to_inbox", return_value={"success": True}) as send,
    ):
        result = server._send_message_impl("owned-worker", "task", barrier="gate")
    assert result == {"success": True}
    assert send.call_args.kwargs["barrier"] == "gate"
