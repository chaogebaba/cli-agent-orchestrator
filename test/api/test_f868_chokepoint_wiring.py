"""F868 #724 + F870 #726 r2 — per-CALL-SITE wiring of the cell-certification
choke point (D4/D6 mutant killers).

Every terminal-creating path must thread its D5 request class down to the ONE
enforcement seam ``terminal_service.create_terminal`` (or ``session_service.create_session``
→ ``create_terminal``). These tests patch the service seam and assert the class
actually arrives on the create call for EACH path. A mutant that drops the
``cell_request_class`` kwarg from any one call site (or hard-codes the wrong
constant) makes that path's assertion go RED — the codex r1 requirement that
"a mutant that removes the call from ANY one path must be killed by a committed
test for that path".

Covered call sites:
  * ``POST /sessions``                       — bare position + provider= → EXPLICIT
  * ``POST /sessions`` (no provider)         — bare position, no provider → ROUTING
  * ``POST /sessions/start``                 — classified likewise
  * ``POST /sessions/{s}/terminals``         — honours the MCP client's class param
  * ``run_agent_step`` (handoff substrate)   — forwards its class to create_terminal
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import Terminal


def _terminal() -> Terminal:
    return Terminal(
        id="abcd1234",
        name="dev-abcd1234",
        session_name="cao-f868",
        provider="kiro_cli",
        agent_profile="dev",
    )


# ==========================================================================
# HTTP POST /sessions — classify EXPLICIT vs ROUTING from provider presence
# ==========================================================================


def test_post_sessions_bare_position_with_provider_threads_explicit(client):
    """MUTANT KILLER: POST /sessions with agent_profile=dev + provider= is
    EXPLICIT; the class must reach create_session. Dropping the kwarg or
    classifying it ROUTING goes RED."""
    mock = MagicMock()
    mock.create_session = AsyncMock(return_value=_terminal())
    with (
        patch("cli_agent_orchestrator.api.main.session_service", mock),
        patch("cli_agent_orchestrator.utils.agent_profiles._position_exists", return_value=True),
    ):
        resp = client.post("/sessions", params={"provider": "kiro_cli", "agent_profile": "dev"})
    assert resp.status_code == 201
    assert mock.create_session.call_args.kwargs["cell_request_class"] == "explicit"


def test_post_sessions_bare_position_no_provider_threads_routing(client):
    """MUTANT KILLER: POST /sessions with a bare position and NO provider is
    ROUTING (routing.toml chooses the provider)."""
    mock = MagicMock()
    mock.create_session = AsyncMock(return_value=_terminal())
    with (
        patch("cli_agent_orchestrator.api.main.session_service", mock),
        patch("cli_agent_orchestrator.api.main.resolve_provider", return_value="kiro_cli"),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles._position_exists",
            return_value=True,
        ),
    ):
        resp = client.post("/sessions", params={"agent_profile": "dev"})
    assert resp.status_code == 201
    assert mock.create_session.call_args.kwargs["cell_request_class"] == "routing"


def test_post_sessions_legacy_name_threads_legacy(client):
    """A legacy (non-position) name classifies LEGACY (no cell, no check)."""
    mock = MagicMock()
    mock.create_session = AsyncMock(return_value=_terminal())
    with (
        patch("cli_agent_orchestrator.api.main.session_service", mock),
        patch("cli_agent_orchestrator.api.main.resolve_provider", return_value="kiro_cli"),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles._position_exists",
            return_value=False,
        ),
    ):
        resp = client.post("/sessions", params={"agent_profile": "my_legacy_worker"})
    assert resp.status_code == 201
    assert mock.create_session.call_args.kwargs["cell_request_class"] == "legacy"


def test_post_sessions_client_class_override_wins(client):
    """The MCP _create_terminal client already classified; an explicit
    cell_request_class query param WINS over the route's re-classification."""
    mock = MagicMock()
    mock.create_session = AsyncMock(return_value=_terminal())
    with patch("cli_agent_orchestrator.api.main.session_service", mock):
        resp = client.post(
            "/sessions",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev-kiro_cli",
                "cell_request_class": "routing",
            },
        )
    assert resp.status_code == 201
    assert mock.create_session.call_args.kwargs["cell_request_class"] == "routing"


# ==========================================================================
# HTTP POST /sessions/{s}/terminals — honours the client's class param
# ==========================================================================


def test_post_terminals_route_threads_client_class(client):
    """MUTANT KILLER (B3, assign's real route): POST /sessions/{s}/terminals is
    what assign posts to. It must forward the caller's cell_request_class to
    create_terminal verbatim. Dropping the kwarg goes RED."""
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    with patch("cli_agent_orchestrator.api.main.terminal_service", mock):
        resp = client.post(
            "/sessions/cao-f868/terminals",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev-kiro_cli",
                "cell_request_class": "routing",
            },
        )
    assert resp.status_code == 201
    assert mock.create_terminal.call_args.kwargs["cell_request_class"] == "routing"


def test_post_terminals_route_default_class_is_explicit(client):
    """With no client class param the route defaults to EXPLICIT (fail-closed)."""
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    with patch("cli_agent_orchestrator.api.main.terminal_service", mock):
        resp = client.post(
            "/sessions/cao-f868/terminals",
            params={"provider": "kiro_cli", "agent_profile": "dev-kiro_cli"},
        )
    assert resp.status_code == 201
    assert mock.create_terminal.call_args.kwargs["cell_request_class"] == "explicit"


# ==========================================================================
# HTTP POST /sessions/start — classify like POST /sessions
# ==========================================================================


def test_post_sessions_start_threads_class(client):
    """MUTANT KILLER: POST /sessions/start forwards the classified class through
    start_session(**kwargs) → create_session → create_terminal."""
    mock = MagicMock()
    mock.start_session = AsyncMock(
        return_value={
            "schema_version": "cao.session-start/v1",
            "session": {"name": "cao-f868"},
            "supervisor_terminal": _terminal().model_dump(mode="json"),
            "bootstrap": {"mode": "not_applicable", "status": "not_required"},
            "manifest": None,
            "manifest_error": None,
        }
    )
    with (
        patch("cli_agent_orchestrator.api.main.session_service", mock),
        patch("cli_agent_orchestrator.utils.agent_profiles._position_exists", return_value=True),
    ):
        resp = client.post(
            "/sessions/start", params={"provider": "kiro_cli", "agent_profile": "dev"}
        )
    assert resp.status_code == 200
    assert mock.start_session.call_args.kwargs["cell_request_class"] == "explicit"


# ==========================================================================
# run_agent_step (handoff substrate, B2) — forwards its class to create_terminal
# ==========================================================================


@pytest.mark.asyncio
async def test_run_agent_step_forwards_cell_request_class():
    """MUTANT KILLER (B2): run_agent_step is the handoff/run-step substrate. It
    must forward its cell_request_class to terminal_service.create_terminal.
    Dropping the kwarg goes RED. Default is 'routing' (a bare-position handoff
    resolves its provider from context, not an operator override)."""
    import cli_agent_orchestrator.services.agent_step as agent_step

    created = {}

    async def fake_create(*a, **k):
        created.update(k)
        raise RuntimeError("stop-after-create")  # abort before readiness wait

    with (
        patch.object(agent_step.terminal_service, "create_terminal", side_effect=fake_create),
        patch.object(
            agent_step,
            "resolve_effective_working_directory",
            new=AsyncMock(return_value="/repo"),
        ),
    ):
        try:
            await agent_step.run_agent_step(
                provider="kiro_cli",
                agent="dev",
                prompt="task",
                session_name="cao-f868",
                cell_request_class="routing",
            )
        except Exception:
            pass  # we only care that create_terminal was reached with the class

    assert created.get("cell_request_class") == "routing"
