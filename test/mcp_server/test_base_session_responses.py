"""Provider-session MCP response serialization tests."""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.mcp_server.server import list_base_sessions, mark_base_ready


@pytest.mark.asyncio
async def test_mark_base_ready_replaces_dirty_hashes_with_count(monkeypatch):
    row = {"name": "base", "dirty_hashes": '{"one.py":"abc","two.py":null}'}
    monkeypatch.setenv("CAO_TERMINAL_ID", "terminal-1")
    terminal_response = patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    with patch("cli_agent_orchestrator.services.fork_context_service.mark_ready", return_value=row), terminal_response as mock_get:
        mock_get.return_value.json.return_value = {"caller_id": None}
        response = await mark_base_ready("base")

    assert response["base"]["dirty_file_count"] == 2
    assert "dirty_hashes" not in response["base"]
    assert response["callback"] == {"status": "not_applicable"}


@pytest.mark.asyncio
async def test_e3_mark_base_ready_threads_anchor_kind(monkeypatch):
    row = {"name": "root", "kind": "anchor", "dirty_hashes": "{}"}
    monkeypatch.setenv("CAO_TERMINAL_ID", "terminal-1")
    with patch(
        "cli_agent_orchestrator.services.fork_context_service.mark_ready",
        return_value=row,
    ) as mark, patch(
        "cli_agent_orchestrator.mcp_server.server.requests.get"
    ) as mock_get:
        mock_get.return_value.json.return_value = {"caller_id": None}
        response = await mark_base_ready("root", summary=None, kind="anchor")

    assert response["base"]["kind"] == "anchor"
    mark.assert_called_once_with("terminal-1", "root", None, "anchor")


@pytest.mark.asyncio
async def test_mark_base_ready_notifies_recorded_caller(monkeypatch):
    row = {"name": "infra", "dirty_hashes": "{}"}
    monkeypatch.setenv("CAO_TERMINAL_ID", "terminal-1")
    with patch("cli_agent_orchestrator.services.fork_context_service.mark_ready", return_value=row), patch(
        "cli_agent_orchestrator.mcp_server.server.requests.get"
    ) as mock_get, patch(
        "cli_agent_orchestrator.mcp_server.server._send_to_inbox"
    ) as mock_inbox:
        mock_get.return_value.json.return_value = {"caller_id": "caller-1"}
        response = await mark_base_ready("infra", "loaded context")

    assert response["success"] is True
    assert response["callback"] == {"status": "delivered"}
    mock_inbox.assert_called_once_with("caller-1", "Base 'infra' ready: loaded context")


@pytest.mark.asyncio
async def test_mark_base_ready_reports_callback_failure_without_failing_mark(monkeypatch):
    row = {"name": "infra", "dirty_hashes": "{}"}
    monkeypatch.setenv("CAO_TERMINAL_ID", "terminal-1")
    with patch("cli_agent_orchestrator.services.fork_context_service.mark_ready", return_value=row), patch(
        "cli_agent_orchestrator.mcp_server.server.requests.get", side_effect=RuntimeError("offline")
    ):
        response = await mark_base_ready("infra", "loaded context")

    assert response["success"] is True
    assert response["callback"] == {"status": "failed", "error": "offline"}


@pytest.mark.asyncio
async def test_list_base_sessions_replaces_dirty_hashes_with_count():
    rows = [
        {"name": "dirty", "dirty_hashes": '{"one.py":"abc"}'},
        {"name": "clean", "dirty_hashes": None},
    ]
    with patch("cli_agent_orchestrator.services.fork_context_service.list_bases", return_value=rows):
        response = await list_base_sessions()

    assert [row["dirty_file_count"] for row in response["bases"]] == [1, 0]
    assert all("dirty_hashes" not in row for row in response["bases"])
