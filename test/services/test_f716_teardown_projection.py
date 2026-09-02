"""F716 (#571): fleet projection must not stamp ERROR on a row being reaped.

AC-F716-1: a row whose tmux window is gone AND which is under an unexpired
F218 teardown intent (delete in flight — intent is opened BEFORE the window
kill) keeps its observed status and is NOT projected as ERROR; the row also
carries the additive `teardown: true` sibling key.
AC-F716-2 (safety property): a row whose tmux window is gone with NO
teardown intent is still projected as ERROR.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.backends import registry as backend_registry
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import fleet_service
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation
from cli_agent_orchestrator.services.teardown_intent_service import open_intent


class _Backend:
    def __init__(self):
        self.windows = {"w-aaaaaaaa"}

    def get_session_windows(self, _session):
        return [{"name": w, "index": str(i)} for i, w in enumerate(sorted(self.windows))]

    def get_history(self, *_a, **_k):
        return ""


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.clear_terminal_metadata_cache()
    backend = _Backend()
    monkeypatch.setattr(backend_registry, "_backend", backend)
    return backend


def _add():
    database.create_terminal(
        "aaaaaaaa",
        "cao-f716",
        "w-aaaaaaaa",
        "cline_cli",
        agent_profile="cline_dev",
    )


def _obs(status):
    return BoundaryObservation(
        observation_epoch="e",
        status=status,
        status_gen=None,
        input_gen=0,
        seq=0,
        last_non_ready_seq=None,
        last_ready_seq=None,
    )


def _project(monkeypatch, published_status):
    monkeypatch.setattr(
        fleet_service.status_monitor,
        "get_boundary_observation",
        lambda _tid: _obs(published_status),
    )
    return fleet_service.build_fleet("cao-f716")["terminals"][0]


def test_window_gone_under_teardown_intent_is_not_error(env, monkeypatch):
    _add()
    with database.SessionLocal() as db:
        open_intent(scope_kind="terminal", scope_key="aaaaaaaa", db=db)
    env.windows.clear()  # window killed mid-teardown; DB row still live
    row = _project(monkeypatch, TerminalStatus.COMPLETED)
    assert row["status"] == TerminalStatus.COMPLETED.value  # NOT error
    assert row["teardown"] is True


def test_window_gone_without_teardown_intent_is_still_error(env, monkeypatch):
    _add()
    env.windows.clear()  # window vanished with no delete in flight
    row = _project(monkeypatch, TerminalStatus.COMPLETED)
    assert row["status"] == TerminalStatus.ERROR.value
    assert row["teardown"] is False


def test_session_scope_teardown_intent_also_suppresses_error(env, monkeypatch):
    _add()
    with database.SessionLocal() as db:
        open_intent(scope_kind="session", scope_key="cao-f716", db=db)
    env.windows.clear()
    row = _project(monkeypatch, TerminalStatus.IDLE)
    assert row["status"] == TerminalStatus.IDLE.value
    assert row["teardown"] is True


def test_window_present_with_teardown_intent_is_normal(env, monkeypatch):
    _add()
    with database.SessionLocal() as db:
        open_intent(scope_kind="terminal", scope_key="aaaaaaaa", db=db)
    row = _project(monkeypatch, TerminalStatus.IDLE)  # window still alive
    assert row["status"] == TerminalStatus.IDLE.value
    assert row["teardown"] is True
