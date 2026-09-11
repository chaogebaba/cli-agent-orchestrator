"""F707 (#562): the per-seat hook edge binds its caller to the route terminal.

The guard is ``_require_caller_is_route_terminal``. Before it, these edges
authorized on token SCOPE alone, so any write-scope holder could act for any
terminal — the #562 sweep finding. The caller must now present the ROUTE
terminal's own F332 per-terminal token (``X-CAO-Terminal-Token`` ==
``$CAO_TERMINAL_TOKEN`` inside that terminal).

**Why this file exists at all.** WP-ARCH 3c K1 deleted the two drain edges these
arms were written against, and with them ``test/api/test_f707_drain_authz.py``.
The GUARD did not go: it is now the ONLY authorization on
``POST /terminals/{id}/native-unpublished``, the register hook's journal edge.
Deleting a test whose subject survives leaves a live regression untested, so the
four arms that test the GUARD rather than the drain are retargeted here. The six
that tested the drain edges themselves went with their subject, and
``test/api/test_3c_k1_drain_edges_gone.py`` pins that those edges are gone.
"""

from unittest.mock import patch

import pytest

_TERMINALS = ["e7070001", "e7070002"]
_EDGE = "/terminals/e7070001/native-unpublished"


@pytest.fixture
def _clean_terminals():
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

    yield
    with SessionLocal() as db:
        db.query(TerminalModel).filter(TerminalModel.id.in_(_TERMINALS)).delete(
            synchronize_session=False
        )
        db.commit()


@pytest.fixture
def client(_clean_terminals):
    from test.api.conftest import TestClientWithHost

    with patch("cli_agent_orchestrator.api.main.status_monitor"):
        from cli_agent_orchestrator.api.main import app

        return TestClientWithHost(app)


def _create_terminal(terminal_id: str, token: str) -> None:
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

    with SessionLocal() as db:
        db.add(
            TerminalModel(
                id=terminal_id,
                tmux_session="test-session",
                tmux_window="test-window",
                provider="mock_cli",
                auth_token=token,
            )
        )
        db.commit()


def _body(terminal_id: str) -> dict:
    return {"terminal_id": terminal_id, "ts": "2026-09-02T00:00:00Z"}


def test_foreign_token_returns_403(client):
    """The #562 finding itself: a worker holding its OWN token cannot speak for
    the seat. The trace write must not happen."""
    _create_terminal("e7070001", "token_seat")
    _create_terminal("e7070002", "token_worker")

    with patch("cli_agent_orchestrator.clients.database.record_message_trace_event") as trace:
        response = client.post(
            _EDGE,
            json=_body("e7070001"),
            headers={"X-CAO-Terminal-Token": "token_worker"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "E-NATIVE-UNPUB-CALLER"
    trace.assert_not_called()


def test_missing_token_returns_403(client):
    """Scope alone is no longer sufficient — the header is required."""
    _create_terminal("e7070001", "token_seat")

    with patch("cli_agent_orchestrator.clients.database.record_message_trace_event") as trace:
        response = client.post(_EDGE, json=_body("e7070001"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "E-NATIVE-UNPUB-CALLER"
    trace.assert_not_called()


def test_own_token_returns_200(client):
    """The positive control. Without it the guard would be satisfied by refusing
    everything, which is a different bug wearing the same green."""
    _create_terminal("e7070001", "token_seat")

    response = client.post(
        _EDGE,
        json=_body("e7070001"),
        headers={"X-CAO-Terminal-Token": "token_seat"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "terminal_id": "e7070001",
        "op": "native-unpublished",
    }


def test_admin_bypass_only_when_auth_enabled(client):
    """SCOPE_ADMIN bypasses only against a real IdP.

    Default-off, every caller is handed the full scope set, so an unconditional
    admin bypass would disable the guard on exactly the deployment that needs it.
    The bypass is therefore gated on ``is_auth_enabled()``.
    """
    _create_terminal("e7070001", "token_seat")

    with patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=False):
        refused = client.post(_EDGE, json=_body("e7070001"))
    with patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=True):
        allowed = client.post(_EDGE, json=_body("e7070001"))
    assert refused.status_code == 403
    assert allowed.status_code == 200


def test_unknown_terminal_still_404(client):
    response = client.post(
        _EDGE,
        json=_body("e7070001"),
        headers={"X-CAO-Terminal-Token": "token_seat"},
    )
    assert response.status_code == 404


def test_body_route_mismatch_still_400(client):
    _create_terminal("e7070001", "token_seat")
    _create_terminal("e7070002", "token_worker")

    response = client.post(
        _EDGE,
        json=_body("e7070002"),
        headers={"X-CAO-Terminal-Token": "token_seat"},
    )
    assert response.status_code == 400
