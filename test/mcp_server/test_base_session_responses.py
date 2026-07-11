"""Provider-session MCP response serialization tests."""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.mcp_server.server import list_base_sessions, mark_base_ready


@pytest.mark.asyncio
async def test_mark_base_ready_replaces_dirty_hashes_with_count(monkeypatch):
    row = {"name": "base", "dirty_hashes": '{"one.py":"abc","two.py":null}'}
    monkeypatch.setenv("CAO_TERMINAL_ID", "terminal-1")
    with patch("cli_agent_orchestrator.services.fork_context_service.mark_ready", return_value=row):
        response = await mark_base_ready("base")

    assert response["base"]["dirty_file_count"] == 2
    assert "dirty_hashes" not in response["base"]


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
