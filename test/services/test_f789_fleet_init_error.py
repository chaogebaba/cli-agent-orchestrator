"""F789 (#646): a worker dead at deferred init must NOT project as `working`.

The fleet projection (``fleet_service.build_fleet``) is the status source. When a
terminal died at deferred init — the reported ``code=deferred_init_internal``
TimeoutError, or a claimed ``init_failed_*`` state — the projected row must:

  1. carry ``status == "error"`` (never ``processing``/``working``), even when the
     provider boundary observation still publishes PROCESSING; and
  2. surface a typed ``terminal_error`` code on the row so the TUI can render
     *why*, instead of a stale ``● working`` with a growing elapsed timer.

A healthy terminal is the control: ``terminal_error`` is ``None`` and a PROCESSING
observation projects as ``processing`` unchanged.

Uses a fake terminal record (in-memory sqlite + ``create_terminal``), mirroring
``test_fleet_delegating.py``.
"""

from datetime import datetime, timedelta, timezone

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

_EPOCH = "0123abcd-4567-89ab-cdef-0123456789ab"  # 36-char lowercase UUID (CHECK ok)


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


def _add(**kwargs):
    database.create_terminal(
        "aaaaaaaa",
        "cao-f789",
        "w-aaaaaaaa",
        "kiro_cli",
        agent_profile="kiro_cli_dev",
        **kwargs,
    )
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


def _project(monkeypatch, published_status=TerminalStatus.PROCESSING):
    """Project the single row, with the provider boundary publishing PROCESSING.

    PROCESSING is the `working` status: it is exactly the state #646 saw ride on
    a dead worker, so the tests drive it and assert the override wins.
    """
    monkeypatch.setattr(
        fleet_service.status_monitor,
        "get_boundary_observation",
        lambda _tid: _obs(published_status),
    )
    return fleet_service.build_fleet("cao-f789")["terminals"][0]


# ── The claimed failure states (init_failed_notified / _caller_gone) ──────────


def test_init_failed_notified_not_working(env, monkeypatch):
    _add(init_state="init_failed_notified")
    row = _project(monkeypatch)
    assert row["status"] == TerminalStatus.ERROR.value
    assert row["status"] != TerminalStatus.PROCESSING.value
    assert row["terminal_error"] == "init_failed"


def test_init_failed_caller_gone_not_working(env, monkeypatch):
    _add(init_state="init_failed_caller_gone")
    row = _project(monkeypatch)
    assert row["status"] == TerminalStatus.ERROR.value
    assert row["terminal_error"] == "init_failed_caller_gone"


# ── The #646 signature: init_pending, deadline elapsed, worker dead ───────────


def test_overdue_init_pending_not_working(env, monkeypatch):
    # Started 300s ago with a 180s deadline → overdue (the reported TimeoutError
    # window). _compute_init_health → "failed"; terminal_error → generic code.
    started = datetime.now(timezone.utc) - timedelta(seconds=300)
    _add(
        init_state="init_pending",
        init_started_at=started,
        init_owner_epoch=_EPOCH,
        init_deadline_s=180.0,
    )
    row = _project(monkeypatch)
    assert row["status"] == TerminalStatus.ERROR.value
    assert row["init_health"] == "failed"
    assert row["terminal_error"] == "deferred_init_failed"


# ── Controls: healthy rows are untouched ──────────────────────────────────────


def test_ready_processing_stays_working(env, monkeypatch):
    _add(init_state="ready")
    row = _project(monkeypatch, TerminalStatus.PROCESSING)
    assert row["status"] == TerminalStatus.PROCESSING.value  # still working
    assert row["terminal_error"] is None


def test_launching_init_pending_is_not_error(env, monkeypatch):
    # Within its deadline → "launching", not failed; no terminal_error, and the
    # provider's live status is preserved.
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    _add(
        init_state="init_pending",
        init_started_at=started,
        init_owner_epoch=_EPOCH,
        init_deadline_s=180.0,
    )
    row = _project(monkeypatch, TerminalStatus.PROCESSING)
    assert row["init_health"] == "launching"
    assert row["terminal_error"] is None
    assert row["status"] == TerminalStatus.PROCESSING.value


# ── The row key is always present (additive contract) ─────────────────────────


def test_terminal_error_key_always_present(env, monkeypatch):
    _add(init_state="ready")
    row = _project(monkeypatch, TerminalStatus.IDLE)
    assert "terminal_error" in row
    assert row["terminal_error"] is None
