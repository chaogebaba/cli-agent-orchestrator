"""F1006 #854 — the create ROUTES honour resolution provenance.

The route half of the fix. ``POST /sessions/{s}/terminals`` and ``POST /sessions``
accept ``cell_request_origin`` — the bare position an upstream resolver composed
``agent_profile`` from — and derive the cell class from it INSTEAD of the
composed name's shape, after verifying the declaration against the server's own
routing composition. The service seam is mocked; what is asserted is the class
that reaches it (or the typed 403 that replaces the create).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import Terminal


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """routing.toml binds dev->kiro_cli. Only the routing store and the position
    existence check are redirected — CAO_HOME_DIR is left alone so the route's
    own database/session machinery stays on the conftest fixture."""
    rt = tmp_path / "routing.toml"
    _write(
        rt,
        """\
        [[binding]]
        position = "dev"
        provider = "kiro_cli"
        kind = "cao"
        """,
    )
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles._position_exists",
        lambda name: name in {"dev", "general"},
    )
    return rt


def _terminal() -> Terminal:
    return Terminal(
        id="abcd1234",
        name="dev-kiro_cli-abcd1234",
        session_name="cao-f1006",
        provider="kiro_cli",
        agent_profile="dev-kiro_cli",
    )


def test_terminals_route_honours_verified_provenance(client, store):
    """THE FIX at the route the MCP shim actually calls: the composed name plus
    its origin position classifies ROUTING and the create proceeds. Before
    F1006 this exact request was the AC15 smoke's 403."""
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    with patch("cli_agent_orchestrator.api.main.terminal_service", mock):
        resp = client.post(
            "/sessions/cao-f1006/terminals",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev-kiro_cli",
                "cell_request_class": "routing",
                "cell_request_origin": "dev",
            },
        )
    assert resp.status_code == 201
    assert mock.create_terminal.call_args.kwargs["cell_request_class"] == "routing"


def test_terminals_route_without_provenance_still_refuses_the_routing_label(client, store):
    """FAIL-BEFORE pin: the same request with no origin declared derives EXPLICIT
    from the composed shape and is refused — the pre-F1006 behaviour, kept for
    any caller that does not account for its own composition."""
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    with patch("cli_agent_orchestrator.api.main.terminal_service", mock):
        resp = client.post(
            "/sessions/cao-f1006/terminals",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev-kiro_cli",
                "cell_request_class": "routing",
            },
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "E-CELL-CLASS-FORGED"
    assert mock.create_terminal.await_count == 0


def test_terminals_route_refuses_a_routing_claim_routing_would_not_compose(client, store):
    """MUTANT KILLER at the route: routing binds dev→kiro_cli, so a claim that
    ``dev-claude_code`` was routing-composed from ``dev`` does not verify —
    typed 403, zero spawn. Trusting the declaration goes RED."""
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    with patch("cli_agent_orchestrator.api.main.terminal_service", mock):
        resp = client.post(
            "/sessions/cao-f1006/terminals",
            params={
                "provider": "claude_code",
                "agent_profile": "dev-claude_code",
                "cell_request_class": "routing",
                "cell_request_origin": "dev",
            },
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "E-CELL-CLASS-FORGED"
    assert mock.create_terminal.await_count == 0


def test_terminals_route_forged_legacy_still_refused_with_provenance_present(client, store):
    """The F868 r4 hole stays closed: a POSITION request labelled ``legacy`` is
    refused even when it also declares a (valid) origin."""
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    with patch("cli_agent_orchestrator.api.main.terminal_service", mock):
        resp = client.post(
            "/sessions/cao-f1006/terminals",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev-kiro_cli",
                "cell_request_class": "legacy",
                "cell_request_origin": "dev",
            },
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "E-CELL-CLASS-FORGED"
    assert mock.create_terminal.await_count == 0


def test_sessions_route_honours_verified_provenance(client, store):
    """The new-session create route (an assign with no CAO_TERMINAL_ID) carries
    the same declaration."""
    mock = MagicMock()
    mock.create_session = AsyncMock(return_value=_terminal())
    with patch("cli_agent_orchestrator.api.main.session_service", mock):
        resp = client.post(
            "/sessions",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev-kiro_cli",
                "cell_request_class": "routing",
                "cell_request_origin": "dev",
            },
        )
    assert resp.status_code == 201
    assert mock.create_session.call_args.kwargs["cell_request_class"] == "routing"
