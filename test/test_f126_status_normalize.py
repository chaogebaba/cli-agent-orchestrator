"""F126: list_messages status normalization at MCP boundary.

Proves that the _list_messages_impl function normalizes the status string
(strip + lower) before forwarding to the HTTP layer, while preserving the
400 behavior for invalid values returned by the server.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest


@pytest.fixture
def mcp_module(monkeypatch):
    """Import the MCP server module and stub cao_http.get."""
    from cli_agent_orchestrator.mcp_server import server as mcp_server

    response = Mock(status_code=200)
    response.json.return_value = {"items": []}
    response.raise_for_status.return_value = None
    get_mock = Mock(return_value=response)
    monkeypatch.setattr(mcp_server.cao_http, "get", get_mock)
    return mcp_server, get_mock, response


# ---------------------------------------------------------------------------
# S1: lowercase existing behavior (already lowercase passes through unchanged)
# ---------------------------------------------------------------------------


def test_lowercase_status_forwarded_unchanged(mcp_module):
    mcp_server, get_mock, _ = mcp_module
    result = mcp_server._list_messages_impl("ab001122", status="pending")
    assert result == {"items": []}
    assert get_mock.call_args.kwargs["params"]["status"] == "pending"


# ---------------------------------------------------------------------------
# S2: uppercase is normalized to lowercase
# ---------------------------------------------------------------------------


def test_uppercase_status_normalized(mcp_module):
    mcp_server, get_mock, _ = mcp_module
    result = mcp_server._list_messages_impl("ab001122", status="PENDING")
    assert result == {"items": []}
    assert get_mock.call_args.kwargs["params"]["status"] == "pending"


def test_uppercase_delivered_normalized(mcp_module):
    mcp_server, get_mock, _ = mcp_module
    mcp_server._list_messages_impl("ab001122", status="DELIVERED")
    assert get_mock.call_args.kwargs["params"]["status"] == "delivered"


# ---------------------------------------------------------------------------
# S3: mixed case is normalized
# ---------------------------------------------------------------------------


def test_mixed_case_status_normalized(mcp_module):
    mcp_server, get_mock, _ = mcp_module
    mcp_server._list_messages_impl("ab001122", status="Pending")
    assert get_mock.call_args.kwargs["params"]["status"] == "pending"


def test_mixed_case_delivery_failed_normalized(mcp_module):
    mcp_server, get_mock, _ = mcp_module
    mcp_server._list_messages_impl("ab001122", status="Delivery_Failed")
    assert get_mock.call_args.kwargs["params"]["status"] == "delivery_failed"


# ---------------------------------------------------------------------------
# S4: surrounding whitespace is stripped
# ---------------------------------------------------------------------------


def test_leading_whitespace_stripped(mcp_module):
    mcp_server, get_mock, _ = mcp_module
    mcp_server._list_messages_impl("ab001122", status="  pending")
    assert get_mock.call_args.kwargs["params"]["status"] == "pending"


def test_trailing_whitespace_stripped(mcp_module):
    mcp_server, get_mock, _ = mcp_module
    mcp_server._list_messages_impl("ab001122", status="pending  ")
    assert get_mock.call_args.kwargs["params"]["status"] == "pending"


def test_surrounding_whitespace_stripped(mcp_module):
    mcp_server, get_mock, _ = mcp_module
    mcp_server._list_messages_impl("ab001122", status=" \t HELD \n ")
    assert get_mock.call_args.kwargs["params"]["status"] == "held"


# ---------------------------------------------------------------------------
# S5: invalid values still reach the server and 400 is preserved
# ---------------------------------------------------------------------------


def test_invalid_status_propagates_server_error(mcp_module, monkeypatch):
    """Invalid status value is normalized but still reaches the server,
    and the 400 response is surfaced to the caller."""
    import requests as http_requests

    mcp_server, get_mock, response = mcp_module
    # Simulate 400 from server for invalid enum value
    response.status_code = 400
    response.raise_for_status.side_effect = http_requests.HTTPError(
        response=response
    )
    response.json.return_value = {
        "detail": "Invalid status: bogus. Valid values: pending, held, ..."
    }

    result = mcp_server._list_messages_impl("ab001122", status="BOGUS")
    # The invalid value is still normalized (strip+lower)
    assert get_mock.call_args.kwargs["params"]["status"] == "bogus"
    # The 400 error detail is returned
    assert "detail" in result
    assert "Invalid status" in result["detail"]


def test_invalid_status_with_whitespace_still_invalid(mcp_module, monkeypatch):
    """Whitespace-padded invalid value is normalized but remains invalid."""
    import requests as http_requests

    mcp_server, get_mock, response = mcp_module
    response.status_code = 400
    response.raise_for_status.side_effect = http_requests.HTTPError(
        response=response
    )
    response.json.return_value = {
        "detail": "Invalid status: xyz. Valid values: pending, held, ..."
    }

    result = mcp_server._list_messages_impl("ab001122", status="  XYZ  ")
    assert get_mock.call_args.kwargs["params"]["status"] == "xyz"
    assert "detail" in result


# ---------------------------------------------------------------------------
# S6: absence of side effects — None status means no status param
# ---------------------------------------------------------------------------


def test_none_status_omits_param(mcp_module):
    """When status is None, the 'status' key must not appear in params."""
    mcp_server, get_mock, _ = mcp_module
    mcp_server._list_messages_impl("ab001122", status=None)
    params = get_mock.call_args.kwargs["params"]
    assert "status" not in params


def test_none_status_messages_unfiltered(mcp_module):
    """With no status filter, messages are returned without filtering."""
    mcp_server, get_mock, response = mcp_module
    response.json.return_value = {
        "items": [
            {"id": 1, "status": "pending"},
            {"id": 2, "status": "delivered"},
        ]
    }
    result = mcp_server._list_messages_impl("ab001122")
    assert len(result["items"]) == 2
    assert "status" not in get_mock.call_args.kwargs["params"]
