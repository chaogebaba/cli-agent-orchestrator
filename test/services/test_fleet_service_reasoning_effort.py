"""F777 (#634): build_fleet emits per-seat reasoning_effort next to
provider/resolved_model, projected from the persisted terminals column.

Also pins the sibling gap this feature closed: _terminal_row_dict now projects
resolved_model (it did not before F777), so the fleet MODEL column shows a real
value rather than always "-".
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


def _row(monkeypatch, *, effort=None, model=None):
    database.create_terminal(
        "aaaaaaaa",
        "cao-f777",
        "w-aaaaaaaa",
        "grok_cli",
        agent_profile="grok_dev",
    )
    with database.SessionLocal() as db:
        term = (
            db.query(database.TerminalModel).filter(database.TerminalModel.id == "aaaaaaaa").first()
        )
        term.resolved_model = model
        term.reasoning_effort = effort
        db.commit()
    database.clear_terminal_metadata_cache()
    monkeypatch.setattr(
        fleet_service.status_monitor,
        "get_boundary_observation",
        lambda _tid: _obs(TerminalStatus.IDLE),
    )
    monkeypatch.setattr(fleet_service.status_monitor, "get_condition", lambda *_a, **_k: None)
    return fleet_service.build_fleet("cao-f777")["terminals"][0]


def test_reasoning_effort_emitted_when_persisted(env, monkeypatch):
    row = _row(monkeypatch, effort="high", model="grok-4.6")
    assert row["reasoning_effort"] == "high"
    # F777 also fixed resolved_model projection (was omitted from the row dict).
    assert row["resolved_model"] == "grok-4.6"


def test_reasoning_effort_none_when_unpersisted(env, monkeypatch):
    """A seat whose effort was never persisted carries None (renders '-')."""
    row = _row(monkeypatch, effort=None, model="grok-4.6")
    assert row["reasoning_effort"] is None


def test_reasoning_effort_key_always_present(env, monkeypatch):
    """The key is an unconditional sibling of provider/resolved_model."""
    row = _row(monkeypatch, effort=None, model=None)
    assert "reasoning_effort" in row
    assert row["provider"] == "grok_cli"
