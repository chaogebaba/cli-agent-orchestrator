"""MCP unregister_base tests."""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.mcp_server.server import unregister_base


@pytest.mark.asyncio
async def test_unregister_base_returns_retired_row_without_backend_calls():
    row = {"name": "base", "status": "retired", "session_uuid": "uuid-1"}
    with patch(
        "cli_agent_orchestrator.services.fork_context_service.retire", return_value=row
    ) as retire, patch("cli_agent_orchestrator.backends.registry.get_backend") as backend:
        assert await unregister_base("base") == {"success": True, "base": row}
    retire.assert_called_once_with("base")
    backend.assert_not_called()


@pytest.mark.asyncio
async def test_unregister_base_unknown_name_is_structured_error():
    with patch("cli_agent_orchestrator.services.fork_context_service.retire", return_value=None):
        assert await unregister_base("missing") == {
            "success": False,
            "error": "no ready base named missing",
        }

