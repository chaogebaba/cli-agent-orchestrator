"""Deterministic Kiro probe fixture for terminal-service unit lifecycles."""

from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities


@pytest.fixture(autouse=True)
def _reset_wake_dedupe_windows():
    """F547 (#403) B1: the per-sender content-hash dedupe window in
    cc_session_registry is a MODULE-GLOBAL (``_dedupe_windows``). Left populated
    by one test, it suppresses a byte-identical native-ring payload in a later
    test in the same worker (e.g. test_f337_auth_handshake's
    ``test_native_ring_works_without_key_file`` saw ``len([]) == 0``). Reset it
    around every services test so the window never leaks across test boundaries.

    Placed here (services conftest) rather than per-file: one autouse fixture
    covers test_f337_auth_handshake, test_fx170_native_doorbell, the F547 files,
    and any future services test that exercises the wake write path — strictly
    smaller than repeating the fixture in each file.
    """
    from cli_agent_orchestrator.services.cc_session_registry import _reset_dedupe_windows

    _reset_dedupe_windows()
    yield
    _reset_dedupe_windows()


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


# --- F165: Shared real-sqlite fixture for daemon end-to-end tests -------------


@pytest.fixture()
def real_sqlite_env(tmp_path, monkeypatch):
    """F165 D11: Create a real sqlite DB, wire SessionLocal to it.

    Wires a real engine and SessionLocal, creates the schema from Base.metadata,
    and seeds only what each test declares. No production code changes for this
    fixture — a shared fixture plus tests only.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import cli_agent_orchestrator.clients.database as db_mod
    from cli_agent_orchestrator.clients.database import Base

    # A fresh DB is only half of a fresh environment: several services keep
    # process-global high-water/dedup caches that shadow DB columns and are
    # keyed by terminal id. Tests here reuse ids like "sup00001", so a cache
    # left behind by an earlier test in the same worker suppresses this test's
    # delivery ("already_notified") even though its DB is empty. Clear them.
    from cli_agent_orchestrator.services import (
        delivery_service,
        doorbell_service,
        inbox_service,
    )

    _shadow_caches = (
        doorbell_service._last_warn_time,
        inbox_service._failure_streaks,
        delivery_service._health_warning_dedup,
    )
    for cache in _shadow_caches:
        cache.clear()

    db_file = tmp_path / "test_real_sqlite.db"
    test_url = f"sqlite:///{db_file}"
    test_engine = create_engine(test_url, connect_args={"check_same_thread": False})
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    # Patch module-level engine + SessionLocal so all lazy imports get ours
    monkeypatch.setattr(db_mod, "engine", test_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)

    # Create all tables
    Base.metadata.create_all(bind=test_engine)

    yield {
        "tmp_path": tmp_path,
        "TestSession": TestSession,
        "engine": test_engine,
        "db_file": db_file,
    }

    for cache in _shadow_caches:
        cache.clear()
