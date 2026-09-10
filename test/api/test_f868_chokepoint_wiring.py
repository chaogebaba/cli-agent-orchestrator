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


def test_post_sessions_client_class_agreeing_is_accepted(client):
    """F868 r4: the MCP _create_terminal client already classified; a
    caller-supplied cell_request_class that AGREES with the server-derived class
    is accepted and threads through. Here dev-kiro_cli (composed literal on a
    real position) derives EXPLICIT, and the client sends explicit."""
    mock = MagicMock()
    mock.create_session = AsyncMock(return_value=_terminal())
    with (
        patch("cli_agent_orchestrator.api.main.session_service", mock),
        patch("cli_agent_orchestrator.utils.agent_profiles._position_exists", return_value=True),
    ):
        resp = client.post(
            "/sessions",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev-kiro_cli",
                "cell_request_class": "explicit",
            },
        )
    assert resp.status_code == 201
    assert mock.create_session.call_args.kwargs["cell_request_class"] == "explicit"


def test_post_sessions_forged_class_is_refused(client):
    """MUTANT KILLER (F868 r4, codex Stage B r2 EMPIRICAL-NO): a POSITION name
    sent with a forged cell_request_class=legacy (to skip certification) is
    refused with a typed E-CELL-CLASS-FORGED 403 and NO create. Trusting the
    query param again (returning it verbatim) goes RED."""
    mock = MagicMock()
    mock.create_session = AsyncMock(return_value=_terminal())
    with (
        patch("cli_agent_orchestrator.api.main.session_service", mock),
        patch("cli_agent_orchestrator.utils.agent_profiles._position_exists", return_value=True),
    ):
        resp = client.post(
            "/sessions",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev",
                "cell_request_class": "legacy",
            },
        )
    assert resp.status_code == 403
    body = resp.json()["detail"]
    assert body["code"] == "E-CELL-CLASS-FORGED"
    assert body["derived"] == "explicit"
    assert body["supplied"] == "legacy"
    mock.create_session.assert_not_called()


# ==========================================================================
# HTTP POST /sessions/{s}/terminals — honours the client's class param
# ==========================================================================


def test_post_terminals_route_agreeing_class_threads(client):
    """MUTANT KILLER (B3, assign's real route): POST /sessions/{s}/terminals is
    what assign posts to. F868 r4: the class is DERIVED server-side; a
    caller-supplied class that AGREES with the derived one is accepted and
    threaded to create_terminal. dev-kiro_cli derives EXPLICIT; the client sends
    explicit. Dropping the kwarg (or not threading the derived class) goes RED."""
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    with (
        patch("cli_agent_orchestrator.api.main.terminal_service", mock),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles._position_exists",
            return_value=True,
        ),
    ):
        resp = client.post(
            "/sessions/cao-f868/terminals",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev-kiro_cli",
                "cell_request_class": "explicit",
            },
        )
    assert resp.status_code == 201
    assert mock.create_terminal.call_args.kwargs["cell_request_class"] == "explicit"


def test_post_terminals_route_forged_class_is_refused(client):
    """MUTANT KILLER (B3, F868 r4, codex Stage B r2 EMPIRICAL-NO): assign's real
    route must NOT forward a forged class verbatim. A POSITION name sent with
    cell_request_class=legacy is refused with a typed E-CELL-CLASS-FORGED 403 and
    NO create_terminal call. The pre-r4 verbatim-forward behaviour goes RED."""
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    with (
        patch("cli_agent_orchestrator.api.main.terminal_service", mock),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles._position_exists",
            return_value=True,
        ),
    ):
        resp = client.post(
            "/sessions/cao-f868/terminals",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev",
                "cell_request_class": "legacy",
            },
        )
    assert resp.status_code == 403
    body = resp.json()["detail"]
    assert body["code"] == "E-CELL-CLASS-FORGED"
    assert body["derived"] == "explicit"
    assert body["supplied"] == "legacy"
    mock.create_terminal.assert_not_called()


def test_post_terminals_route_omitted_class_uses_derived(client):
    """With NO cell_request_class param the route DERIVES the class server-side
    (no forgeable default). dev-kiro_cli + provider derives EXPLICIT and threads
    it through. A default that got validated against the derived class (the r4
    bug where the "explicit" literal default disagreed with a legacy derivation)
    goes RED."""
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    with (
        patch("cli_agent_orchestrator.api.main.terminal_service", mock),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles._position_exists",
            return_value=True,
        ),
    ):
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


# ==========================================================================
# F868 r4 — the forged-class bypass on the resume_from create surface
# ==========================================================================


def _admit_resume_stub(claimed_key="rk-1"):
    """Stand in for _f829_admit_resume: return a link admission + a claimed key
    + resolved overrides, so the resume path proceeds to the cell-class
    reconcile without a live cao-server."""

    async def _stub(**_kwargs):
        overrides = {
            "provider": "kiro_cli",
            "agent_profile": "dev",
            "working_directory": "/repo/wt",
            "fork_context": None,
            "authority_files": None,
        }
        return (object(), claimed_key, overrides)

    return _stub


def test_resume_from_forged_class_is_refused_and_compensates_claim(client):
    """MUTANT KILLER (F868 r4, codex Stage B r2 EMPIRICAL-NO): a resume_from
    create carrying a POSITION name with a forged cell_request_class=legacy is
    refused with a typed E-CELL-CLASS-FORGED 403, NO create_terminal call, and
    the resume claim taken during admission is COMPENSATED (cleared). The pre-r4
    behaviour (forwarding the query param verbatim into create_terminal) goes
    RED. A bare position supplied on resume derives EXPLICIT (the caller's own
    cell choice), so legacy is a forgery."""
    svc = MagicMock()
    svc.create_terminal = AsyncMock(return_value=_terminal())
    svc.seed_resume_bootstrap = AsyncMock(return_value=None)
    cleared = {}

    def _clear(key, event=None):
        cleared["key"] = key
        cleared["event"] = event

    with (
        patch("cli_agent_orchestrator.api.main.terminal_service", svc),
        patch(
            "cli_agent_orchestrator.api.main._f829_admit_resume",
            new=_admit_resume_stub("rk-forged"),
        ),
        patch("cli_agent_orchestrator.clients.database.clear_resume_claim", new=_clear),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles._position_exists",
            return_value=True,
        ),
    ):
        resp = client.post(
            "/sessions/cao-f868/terminals",
            params={
                "agent_profile": "dev",
                "cell_request_class": "legacy",
            },
            json={"resume_from": "old12345"},
        )
    assert resp.status_code == 403
    body = resp.json()["detail"]
    assert body["code"] == "E-CELL-CLASS-FORGED"
    assert body["supplied"] == "legacy"
    # bare position on resume => the caller's EXPLICIT cell choice
    assert body["derived"] == "explicit"
    svc.create_terminal.assert_not_called()
    # the resume claim taken during admission was compensated
    assert cleared.get("key") == "rk-forged"
    assert cleared.get("event") == "resume_failed"


def test_resume_from_agreeing_class_proceeds(client):
    """A plain resume (no bare-position override) derives RESUME; a client that
    sends cell_request_class=resume AGREES and the create proceeds with the
    derived class. Confirms the reconcile does not spuriously refuse a genuine
    resume."""
    svc = MagicMock()
    svc.create_terminal = AsyncMock(return_value=_terminal())
    svc.seed_resume_bootstrap = AsyncMock(return_value=None)
    with (
        patch("cli_agent_orchestrator.api.main.terminal_service", svc),
        patch(
            "cli_agent_orchestrator.api.main._f829_admit_resume",
            new=_admit_resume_stub("rk-ok"),
        ),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles._position_exists",
            return_value=False,
        ),
    ):
        resp = client.post(
            "/sessions/cao-f868/terminals",
            params={
                "agent_profile": "my_legacy_worker",
                "cell_request_class": "resume",
            },
            json={"resume_from": "old12345"},
        )
    assert resp.status_code == 201
    assert svc.create_terminal.call_args.kwargs["cell_request_class"] == "resume"


def test_post_sessions_start_has_no_forgeable_class_param(client):
    """POST /sessions/start derives the class server-side and exposes NO
    cell_request_class query param, so it cannot be forged. Passing one is
    ignored (FastAPI drops the unknown query param); the derived class is used."""
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
            "/sessions/start",
            params={
                "provider": "kiro_cli",
                "agent_profile": "dev",
                "cell_request_class": "legacy",  # ignored — not a route param
            },
        )
    assert resp.status_code == 200
    # server-derived EXPLICIT, NOT the forged legacy
    assert mock.start_session.call_args.kwargs["cell_request_class"] == "explicit"
