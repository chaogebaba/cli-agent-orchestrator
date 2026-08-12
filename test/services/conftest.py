"""Deterministic Kiro probe fixture for terminal-service unit lifecycles."""

import pytest
from unittest.mock import AsyncMock

from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities


@pytest.fixture(autouse=True)
def mock_kiro_capability_probe(monkeypatch):
    """Keep service tests independent from a locally installed Kiro wrapper."""

    def probe(_engine, _requested):
        return KiroCapabilities(
            version="2.13.0",
            flags=frozenset(
                {
                    "--agent-engine",
                    "--v3",
                    "--agent",
                    "--model",
                    "--legacy-ui",
                    "--trust-all-tools",
                    "--require-mcp-startup",
                }
            ),
        )

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.probe_kiro_capabilities",
        probe,
    )


@pytest.fixture(autouse=True)
def mock_confirm_launch_health(request, monkeypatch):
    """F138: bypass process-liveness check in unit tests (no real processes).

    Skipped for F138 test modules that exercise the real lifecycle.
    """
    mod = request.node.module.__name__
    if "f138" in mod or "f124" in mod or "test_terminal_service" == mod.rsplit(".", 1)[-1]:
        return
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service._confirm_launch_health",
        AsyncMock(),
    )


@pytest.fixture(autouse=True)
def _clean_f138_incarnations():
    """F138: clear process_incarnations between tests to prevent UNIQUE violations."""
    yield
    try:
        from cli_agent_orchestrator.clients.database import SessionLocal
        from sqlalchemy import text

        with SessionLocal() as db:
            db.execute(text("DELETE FROM process_incarnations"))
            db.execute(text("DELETE FROM orphan_reconcile_jobs"))
            db.commit()
    except Exception:
        pass
