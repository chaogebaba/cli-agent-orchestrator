"""F332 AC14: Both MCP send paths present the X-CAO-Terminal-Token header.

server.py:_send_to_inbox and app_tools.py:_post_json each attach the header
from os.environ["CAO_TERMINAL_TOKEN"].
"""

import os
from unittest.mock import patch, MagicMock

import pytest


class TestSendToInboxHeader:
    """AC14a: server.py _send_to_inbox attaches X-CAO-Terminal-Token."""

    def test_token_header_attached(self):
        from cli_agent_orchestrator.mcp_server import server

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": 1}

        captured_kwargs = {}

        def fake_post(url, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        with patch.object(server, "cao_http") as mock_http:
            mock_http.post = fake_post
            with patch.dict(os.environ, {
                "CAO_TERMINAL_ID": "ab12cd34",
                "CAO_TERMINAL_TOKEN": "test_secret_token_123",
            }):
                server._send_to_inbox("receiver_id", "hello")

        headers = captured_kwargs.get("headers", {})
        assert headers.get("X-CAO-Terminal-Token") == "test_secret_token_123"

    def test_no_token_env_means_no_header(self):
        from cli_agent_orchestrator.mcp_server import server

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": 1}

        captured_kwargs = {}

        def fake_post(url, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        env = {"CAO_TERMINAL_ID": "ab12cd34"}
        # Ensure CAO_TERMINAL_TOKEN is not set
        env_clean = {k: v for k, v in os.environ.items() if k != "CAO_TERMINAL_TOKEN"}
        env_clean.update(env)

        with patch.object(server, "cao_http") as mock_http:
            mock_http.post = fake_post
            with patch.dict(os.environ, env_clean, clear=True):
                server._send_to_inbox("receiver_id", "hello")

        headers = captured_kwargs.get("headers")
        # Headers should be None or not contain the token
        if headers:
            assert "X-CAO-Terminal-Token" not in headers


class TestPostJsonHeader:
    """AC14b: app_tools.py _post_json attaches X-CAO-Terminal-Token."""

    def test_token_header_attached_alongside_auth(self):
        from cli_agent_orchestrator.mcp_server import app_tools

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "ok"}

        captured_kwargs = {}

        def fake_post(url, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        with patch.object(app_tools, "cao_http") as mock_http:
            mock_http.post = fake_post
            with patch.object(app_tools, "get_local_bearer", return_value="operator_bearer"):
                with patch.dict(os.environ, {"CAO_TERMINAL_TOKEN": "worker_secret_456"}):
                    app_tools._post_json("/some/path", params={"key": "val"})

        headers = captured_kwargs.get("headers", {})
        # Both headers present
        assert headers.get("X-CAO-Terminal-Token") == "worker_secret_456"
        assert "Bearer operator_bearer" in headers.get("Authorization", "")
