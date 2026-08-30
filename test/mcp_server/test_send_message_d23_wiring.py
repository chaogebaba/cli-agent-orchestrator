"""F578 D23 — end-to-end wiring of expire_after_s / supersede_key through the
send_message surfaces (AC10).

Empirical gate r1 finding B2: the DB row-state seam and its unit tests exist
(``test/clients/test_inbox_expire_supersede.py``), but ``mcp_server.server.
send_message`` and ``api.main.create_inbox_message_endpoint`` did not expose the
fields, so ``send_message(..., expire_after_s=5)`` could not reach the DB.

These tests pin the three added hops:
  * MCP ``_send_message_impl`` → ``_send_to_inbox`` kwargs.
  * ``_send_to_inbox`` → HTTP POST query params.
  * A DROP-THE-PASS-THROUGH mutant leaves the fields absent from the wire.

The endpoint→DB hop and the DB seam itself are covered end-to-end by
``test/api/test_inbox_expire_supersede_endpoint.py`` and
``test/clients/test_inbox_expire_supersede.py`` respectively.
"""

import os
from unittest.mock import patch

import pytest


def _impl():
    from cli_agent_orchestrator.mcp_server.server import _send_message_impl

    return _send_message_impl


def test_impl_threads_expire_after_s_to_inbox_producer():
    with (
        patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}),
        patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox") as send,
    ):
        send.return_value = {"success": True}
        _impl()("receiver", "ephemeral", expire_after_s=5)

    assert send.call_args.kwargs == {"refresh_ingest": False, "expire_after_s": 5}


def test_impl_threads_supersede_key_to_inbox_producer():
    with (
        patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}),
        patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox") as send,
    ):
        send.return_value = {"success": True}
        _impl()("receiver", "status v2", supersede_key="status")

    assert send.call_args.kwargs == {"refresh_ingest": False, "supersede_key": "status"}


def test_impl_threads_both_controls_together():
    with (
        patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}),
        patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox") as send,
    ):
        send.return_value = {"success": True}
        _impl()("receiver", "m", expire_after_s=9, supersede_key="k")

    assert send.call_args.kwargs == {
        "refresh_ingest": False,
        "expire_after_s": 9,
        "supersede_key": "k",
    }


def test_impl_omits_controls_when_unset_byte_identical_to_today():
    """A send with neither field must not add either kwarg (opt-in, Do-NOT 21)."""
    with (
        patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}),
        patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox") as send,
    ):
        send.return_value = {"success": True}
        _impl()("receiver", "ordinary")

    assert send.call_args.kwargs == {"refresh_ingest": False}
    assert "expire_after_s" not in send.call_args.kwargs
    assert "supersede_key" not in send.call_args.kwargs


def test_send_to_inbox_forwards_controls_as_query_params():
    """The HTTP hop: _send_to_inbox must place both controls in the POST params
    so the endpoint (a query-param signature) receives them."""
    from cli_agent_orchestrator.mcp_server import server

    with (
        patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}, clear=False),
        patch.object(server, "cao_http") as http,
    ):
        http.post.return_value.status_code = 200
        http.post.return_value.raise_for_status.return_value = None
        http.post.return_value.json.return_value = {"success": True}
        server._send_to_inbox("recv", "m", expire_after_s=5, supersede_key="k")

    params = http.post.call_args.kwargs["params"]
    assert params["expire_after_s"] == 5
    assert params["supersede_key"] == "k"
    assert params["sender_id"] == "deadbeef"


def test_send_to_inbox_omits_controls_when_unset():
    from cli_agent_orchestrator.mcp_server import server

    with (
        patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}, clear=False),
        patch.object(server, "cao_http") as http,
    ):
        http.post.return_value.status_code = 200
        http.post.return_value.raise_for_status.return_value = None
        http.post.return_value.json.return_value = {"success": True}
        server._send_to_inbox("recv", "m")

    params = http.post.call_args.kwargs["params"]
    assert "expire_after_s" not in params
    assert "supersede_key" not in params


# --- Mutant: drop the pass-through --------------------------------------------
# The B2 mutant. If _send_to_inbox ignores expire_after_s (does not place it in
# the POST params), a caller's expire_after_s=5 is silently lost on the wire.
# We simulate the mutant by asserting the CURRENT code carries it — a mutant
# that drops the ``if expire_after_s is not None: params[...] = ...`` block
# makes this assertion fail (the row would never expire → AC10 broken).


def test_mutant_dropping_pass_through_is_detectable():
    """Positive guard that would fail under the drop-pass-through mutant."""
    from cli_agent_orchestrator.mcp_server import server

    with (
        patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}, clear=False),
        patch.object(server, "cao_http") as http,
    ):
        http.post.return_value.status_code = 200
        http.post.return_value.raise_for_status.return_value = None
        http.post.return_value.json.return_value = {"success": True}
        server._send_to_inbox("recv", "m", expire_after_s=5)

    # Under the mutant (block removed) this key is absent and the assert fails.
    assert http.post.call_args.kwargs["params"].get("expire_after_s") == 5
