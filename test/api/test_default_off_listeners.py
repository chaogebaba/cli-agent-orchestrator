"""Regression: `cao-server` with no flags opens no listener beyond :9889.

The A2A / Agent Card transport is a separate, additive surface that must not
exist on this branch at all — no module, no lifespan wiring, no ``/a2a`` routes. This
asserts that stronger contract, plus the AG-UI surface's own default-off
behavior (no flags => 404).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app


@pytest.fixture(autouse=True)
def _no_surface_env(monkeypatch):
    # Ensure a clean env: no AG-UI / MCP Apps / legacy agent-card flags set.
    for var in (
        "CAO_AGENT_CARD_ENABLED",
        "CAO_AGENT_CARD_DISABLED",
        "CAO_AGUI_ENABLED",
        "CAO_MCP_APPS_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)


def test_a2a_surface_absent_from_this_build():
    """The A2A/Agent Card transport is not part of this branch."""
    with pytest.raises(ImportError):
        import cli_agent_orchestrator.a2a  # noqa: F401
    with pytest.raises(ImportError):
        import cli_agent_orchestrator.agent_card  # noqa: F401


def test_no_agent_card_listener_state_without_flag():
    """Default (no flag) => no listener handle and no /a2a routes are mounted."""
    with TestClient(app, base_url="http://localhost"):
        assert getattr(app.state, "agent_card_listener", None) is None
        paths = {getattr(route, "path", "") for route in app.routes}
        assert not any(p.startswith("/a2a") for p in paths)


def test_agui_surface_is_off_when_apps_is_off(monkeypatch):
    """AG-UI routes 404 when ``apps.enabled`` is off.

    F747 (#747): ``_agui_enabled()`` ORs ``CAO_AGUI_ENABLED`` with
    ``apps.enabled``, and apps.enabled now SHIPS ON -- so "no env vars set" is
    no longer the off posture; the shipped default is itself a flag. Deleting
    the env vars (the autouse fixture above) leaves the surface ENABLED, and
    ``GET /agui/v1/stream`` then opens the live event bus, which never
    terminates. That blocked the TestClient forever and took an xdist worker
    with it -- one of the two hangs behind the F747 suite crawl.

    Turning apps.enabled off explicitly is what this test always meant.
    """
    monkeypatch.setenv("CAO_MCP_APPS_ENABLED", "false")
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/agui/v1/stream").status_code == 404
        resp = client.post("/agui/v1/emit_ui", json={"component": "progress", "props": {}})
        assert resp.status_code == 404
