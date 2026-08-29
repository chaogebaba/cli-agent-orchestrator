"""F568 D12c: `delegating (N)` fleet projection over (final status, children).

AC-F568-4: the fleet JSON row carries `delegating: true` + `children_count: N`
(status enum unchanged). AC-F568-8 projection rows: an IDLE/COMPLETED seat with
children>0 is `delegating`; a PROCESSING seat stays `working` (delegating False);
an ERROR/quarantined seat is never `delegating`. `delegating` is computed over
the FINAL projected status — after the three ERROR overrides at the seam.
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


def _add(children=0, recovery_state=None):
    meta = None
    if children:
        meta = {"children": [{"id": f"c{i}", "started_at": 1e12 + i} for i in range(children)]}
    database.create_terminal(
        "aaaaaaaa",
        "cao-d12c",
        "w-aaaaaaaa",
        "claude_code",
        agent_profile="chao_supervisor",
        metadata=meta,
    )
    if recovery_state is not None:
        with database.SessionLocal() as db:
            row = (
                db.query(database.TerminalModel)
                .filter(database.TerminalModel.id == "aaaaaaaa")
                .first()
            )
            row.recovery_state = recovery_state
            db.commit()
        database.clear_terminal_metadata_cache()


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
    return fleet_service.build_fleet("cao-d12c")["terminals"][0]


def test_idle_with_children_is_delegating(env, monkeypatch):
    _add(children=2)
    row = _project(monkeypatch, TerminalStatus.IDLE)
    assert row["status"] == TerminalStatus.IDLE.value  # enum unchanged
    assert row["delegating"] is True
    assert row["children_count"] == 2


def test_completed_with_children_is_delegating(env, monkeypatch):
    _add(children=1)
    row = _project(monkeypatch, TerminalStatus.COMPLETED)
    assert row["status"] == TerminalStatus.COMPLETED.value
    assert row["delegating"] is True
    assert row["children_count"] == 1


def test_processing_with_children_stays_working(env, monkeypatch):
    """A PROCESSING seat's own turn is open: `working`, never `delegating`."""
    _add(children=3)
    row = _project(monkeypatch, TerminalStatus.PROCESSING)
    assert row["status"] == TerminalStatus.PROCESSING.value
    assert row["delegating"] is False
    assert row["children_count"] == 3


def test_error_with_children_never_delegating(env, monkeypatch):
    """recovery_state ERROR override at the seam ⇒ FINAL status ERROR ⇒ not delegating."""
    _add(children=2, recovery_state="failed")
    row = _project(monkeypatch, TerminalStatus.IDLE)
    assert row["status"] == TerminalStatus.ERROR.value
    assert row["delegating"] is False
    assert row["children_count"] == 2


def test_idle_no_children_not_delegating(env, monkeypatch):
    _add(children=0)
    row = _project(monkeypatch, TerminalStatus.IDLE)
    assert row["delegating"] is False
    assert row["children_count"] == 0


def test_helper_counts_free_form_children_key():
    assert fleet_service._children_count_from_row({"metadata": {"children": [1, 2]}}) == 2
    assert fleet_service._children_count_from_row({"metadata": {}}) == 0
    assert fleet_service._children_count_from_row({"metadata": None}) == 0
    assert fleet_service._children_count_from_row({}) == 0
