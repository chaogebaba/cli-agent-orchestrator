"""F332: bind_mcp_server_identity forwards CAO_TERMINAL_TOKEN when set."""

import os
from unittest.mock import patch

import pytest


def test_bind_mcp_server_identity_includes_terminal_token():
    """bind_mcp_server_identity sets CAO_TERMINAL_TOKEN in env when available."""
    from cli_agent_orchestrator.utils.sandbox_guard import bind_mcp_server_identity

    config = {"command": "cao-mcp-server", "args": [], "env": {}}

    with patch.dict(os.environ, {
        "CAO_TERMINAL_TOKEN": "secret_abc",
        "CAO_INSTANCE_ID": "inst_1",
    }):
        with patch(
            "cli_agent_orchestrator.utils.http.resolve_endpoint",
            return_value="http://localhost:9889",
        ):
            result = bind_mcp_server_identity(config, "ab12cd34")

    assert result["env"]["CAO_TERMINAL_ID"] == "ab12cd34"
    assert result["env"]["CAO_TERMINAL_TOKEN"] == "secret_abc"


def test_bind_mcp_server_identity_omits_token_when_unset():
    """bind_mcp_server_identity does not add empty CAO_TERMINAL_TOKEN."""
    from cli_agent_orchestrator.utils.sandbox_guard import bind_mcp_server_identity

    config = {"command": "cao-mcp-server", "args": [], "env": {}}

    env = {k: v for k, v in os.environ.items() if k != "CAO_TERMINAL_TOKEN"}
    env["CAO_INSTANCE_ID"] = "inst_1"

    with patch.dict(os.environ, env, clear=True):
        with patch(
            "cli_agent_orchestrator.utils.http.resolve_endpoint",
            return_value="http://localhost:9889",
        ):
            result = bind_mcp_server_identity(config, "ab12cd34")

    assert result["env"]["CAO_TERMINAL_ID"] == "ab12cd34"
    assert "CAO_TERMINAL_TOKEN" not in result["env"]
