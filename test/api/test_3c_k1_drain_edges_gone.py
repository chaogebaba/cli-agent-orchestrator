"""WP-ARCH 3c K1: the supervisor drain/ack HTTP edges are gone from the surface.

The drain hook and its two server edges were a second seat carrier over the same
message id, with an ack watermark the native carrier never consulted (#506/#499).
Deleting the hook alone would leave the edges reachable by anything holding a
write-scope token, so the route table is asserted directly here: a POST must 404
because no route matches, not merely because a terminal is unknown.

What must NOT go with them is ``/terminals/{id}/native-unpublished`` — the
register hook's journal edge, which shares the F707 caller-binding guard and the
request model the drain edges used. An over-broad deletion that took it out would
silence the only fleet-visible signal that a seat published no socket.
"""

from __future__ import annotations

from cli_agent_orchestrator.api.main import app


def _route_paths() -> set[str]:
    return {getattr(route, "path", "") for route in app.router.routes}


def test_the_drain_route_is_absent_from_the_route_table() -> None:
    assert "/terminals/{terminal_id}/inbox/drain" not in _route_paths()


def test_the_drain_ack_route_is_absent_from_the_route_table() -> None:
    assert "/terminals/{terminal_id}/inbox/drain-ack" not in _route_paths()


def test_the_ws_supervisor_doorbell_route_is_absent() -> None:
    """K3b: the WebSocket doorbell plane's route goes with its module."""
    assert "/ws/supervisor/{terminal_id}" not in _route_paths()


#: A POST to a path the app does not serve comes back 404 when nothing matches
#: and 405 when something matches the path under another method. Which one an
#: environment produces is not the property under test -- the laptop answers 404
#: and a box answers 405 for these same two paths on the same commit -- so the
#: arms below accept either and the exact absence is pinned by the route-table
#: arms above. What both codes rule out is the only thing that matters here: no
#: POST reaches a drain handler.
_NO_SUCH_POST = (404, 405)


def test_posting_to_the_drain_edge_does_not_reach_a_handler(client) -> None:
    """The behavioural half: the request is refused by the router itself, before
    any terminal lookup, scope check or caller binding runs."""
    resp = client.post(
        "/terminals/abcd1234/inbox/drain",
        json={"terminal_id": "abcd1234"},
    )
    assert resp.status_code in _NO_SUCH_POST, resp.text
    assert "op" not in resp.text, "a drain handler answered: the edge is still live"


def test_posting_to_the_drain_ack_edge_does_not_reach_a_handler(client) -> None:
    resp = client.post(
        "/terminals/abcd1234/inbox/drain-ack",
        json={"terminal_id": "abcd1234"},
    )
    assert resp.status_code in _NO_SUCH_POST, resp.text
    assert "op" not in resp.text, "an ack handler answered: the edge is still live"


def test_the_register_hooks_journal_edge_survives() -> None:
    """The negative control for an over-broad deletion.

    ``native-unpublished`` is the register hook's edge, not the drain's. It uses
    the same ``_require_caller_is_route_terminal`` guard and the same request
    model, so a deletion that swept by symbol rather than by route would take it
    too — and the seat's registration failure would stop reaching the journal.
    """
    assert "/terminals/{terminal_id}/native-unpublished" in _route_paths()
