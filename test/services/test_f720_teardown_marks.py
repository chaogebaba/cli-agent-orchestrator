"""F720 (#576): every tmux kill outside ``delete_terminal`` runs under a mark.

F716 (#571) bracketed the delete path only, so the five OTHER kill sites still
killed windows with nothing recorded, and a fleet sampled during any of them
stamped ERROR on rows that were being reaped on purpose. Each test here patches
the backend kill and asserts, AT THE MOMENT OF THE KILL, that
``active_teardown_scope_keys()`` carries the terminal or session key — and that
the key is gone once the path returns (a mark that outlived its kill would
suppress real ERRORs for the whole TTL).

Sites, one test each:
  (a) ``session_service.finalize_session`` and the deferred-cleanup kill in
      ``session_service.delete_session`` — session scope.
  (b) ``flow_service.execute_flow`` recycling.
  (c) ``herdr_inbox_service._reconcile`` killing an empty workspace.
  (d) ``terminal_service._rollback_terminal_creation`` and
      ``_roll_back_backend_create_locked`` — both branches.
  (e) the session-scope producer that ``fleet_service``'s
      ``session_name in teardown_scope_keys`` branch never had; the projection
      test below is what proves the branch is now reachable, AND that it stays
      narrow: a terminal of ANOTHER session whose window is gone is still ERROR.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.backends import registry as backend_registry
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import (
    fleet_service,
    flow_service,
    session_service,
    terminal_service,
)
from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation
from cli_agent_orchestrator.services.teardown_intent_service import active_teardown_scope_keys


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    """In-memory registry DB: the durable half of a mark needs a live table."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    database.clear_terminal_metadata_cache()
    yield


class _KillProbe:
    """Records ``active_teardown_scope_keys()`` at each kill."""

    def __init__(self) -> None:
        self.samples: list[set[str]] = []

    def __call__(self, *_a, **_k) -> bool:
        self.samples.append(active_teardown_scope_keys())
        return True

    @property
    def marked(self) -> set[str]:
        assert self.samples, "the kill under test never ran"
        return set().union(*self.samples)


# ---------------------------------------------------------------------------
# (a) session teardown — finalize_session
# ---------------------------------------------------------------------------


def test_finalize_session_kill_runs_under_session_mark():
    session = "cao-f720-final"
    probe = _KillProbe()
    backend = MagicMock()
    backend.kill_session.side_effect = probe
    # Alive for the first check (so the kill fires), gone afterwards.
    backend.session_exists_strict.side_effect = [True] + [False] * 8
    backend.session_exists.return_value = False

    with (
        patch.object(session_service, "clear_session_env"),
        patch.object(session_service, "dispatch_plugin_event"),
    ):
        session_service.finalize_session(session, backend=backend)

    assert session in probe.marked
    assert session not in active_teardown_scope_keys()


def test_finalize_session_mark_covers_every_verify_retry():
    """The verify loop can kill more than once; each kill is still marked."""
    session = "cao-f720-retry"
    probe = _KillProbe()
    backend = MagicMock()
    backend.kill_session.side_effect = probe
    # alive, alive (retry kills), then gone
    backend.session_exists_strict.side_effect = [True, True] + [False] * 8
    backend.session_exists.return_value = False

    with (
        patch.object(session_service, "clear_session_env"),
        patch.object(session_service, "dispatch_plugin_event"),
        patch.object(session_service, "SESSION_TEARDOWN_VERIFY_DELAY_SECONDS", 0),
    ):
        session_service.finalize_session(session, backend=backend)

    assert len(probe.samples) >= 2
    assert all(session in sample for sample in probe.samples)
    assert session not in active_teardown_scope_keys()


def test_finalize_session_releases_the_mark_when_teardown_fails():
    """A session that refuses to die must not leave a mark suppressing ERROR."""
    session = "cao-f720-stuck"
    backend = MagicMock()
    backend.session_exists_strict.return_value = True

    with patch.object(session_service, "SESSION_TEARDOWN_VERIFY_DELAY_SECONDS", 0):
        with pytest.raises(RuntimeError, match="still exists after teardown"):
            session_service.finalize_session(session, backend=backend)

    assert session not in active_teardown_scope_keys()


# ---------------------------------------------------------------------------
# (a) session teardown — delete_session's deferred-cleanup kill
# ---------------------------------------------------------------------------


def test_delete_session_deferred_cleanup_kill_runs_under_session_mark():
    """Cleanup deferred -> rows are RETAINED, so the kill needs the mark most."""
    session = "cao-f720-deferred"
    probe = _KillProbe()
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.kill_session.side_effect = probe

    with (
        patch.object(session_service, "list_terminals_by_session", return_value=[{"id": "t-dfr"}]),
        patch.object(session_service, "get_backend", return_value=backend),
        patch.object(session_service, "finalize_session") as finalize,
        patch(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.preflight_session_teardown",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.quiesce_deferred_session_sync",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease",
            return_value={"terminal_deleted": False},
        ),
    ):
        result = session_service.delete_session(session)

    assert result["errors"]  # deferred branch, not the finalize branch
    finalize.assert_not_called()
    assert session in probe.marked
    assert session not in active_teardown_scope_keys()


# ---------------------------------------------------------------------------
# (b) flow recycling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flow_recycle_kill_runs_under_session_mark():
    probe = _KillProbe()
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.kill_session.side_effect = probe

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(
            '---\nname: f720-flow\nschedule: "* * * * *"\nagent_profile: developer\n---\n\nBody.\n'
        )
        f.flush()
        flow_path = f.name

    flow = Flow(
        name="f720-flow",
        file_path=flow_path,
        schedule="* * * * *",
        agent_profile="developer",
        provider="kiro_cli",
        script="",
        enabled=True,
        next_run=datetime.now(),
    )
    terminals = [{"id": "t-flow-1"}, {"id": "t-flow-2"}]
    purge_marks: list[set[str]] = []

    with (
        patch.object(flow_service, "db_get_flow", return_value=flow),
        patch.object(flow_service, "db_update_flow_run_times"),
        patch.object(flow_service, "get_backend", return_value=backend),
        patch.object(flow_service, "list_terminals_by_session", return_value=terminals),
        patch.object(flow_service, "_is_terminal_busy", return_value=False),
        patch.object(flow_service, "create_terminal", return_value=MagicMock(id="t-new")),
        patch.object(flow_service, "send_input"),
        patch.object(flow_service.fifo_manager, "stop_reader"),
        patch.object(flow_service.status_monitor, "clear_terminal"),
        patch("cli_agent_orchestrator.services.terminal_service.quiesce_deferred_terminals"),
        patch(
            "cli_agent_orchestrator.providers.manager.provider_manager.cleanup_provider",
            return_value=True,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service._delete_terminal_core",
            side_effect=lambda _tid: purge_marks.append(active_teardown_scope_keys()),
        ),
    ):
        assert await flow_service.execute_flow("f720-flow") is True

    killed_session = backend.kill_session.call_args[0][0]
    assert killed_session in probe.marked
    # The mark must span kill -> row purge: rows outlive the window in between.
    assert len(purge_marks) == len(terminals)
    assert all(killed_session in marks for marks in purge_marks)
    assert killed_session not in active_teardown_scope_keys()


# ---------------------------------------------------------------------------
# (c) herdr reconcile — empty workspace kill
# ---------------------------------------------------------------------------


def test_herdr_reconcile_workspace_kill_runs_under_session_mark():
    session = "cao-f720-herdr"
    probe = _KillProbe()
    backend = MagicMock()
    backend.kill_session.side_effect = probe

    service = HerdrInboxService(socket_path="/data/claude-scratch/worker-scratch/f720/no.sock")
    service.register_terminal("t-herdr", "pane-stale")
    service._workspace_to_session = {"ws-1": session}

    snapshot = {"panes": [], "tabs": [], "workspaces": []}
    meta = {
        "id": "t-herdr",
        "init_state": "ready",
        "tmux_session": session,
        "tmux_window": "w-t-herdr",
    }

    def _drop(terminal_id, pane_id):
        service._pane_to_terminal.pop(pane_id, None)
        service._terminal_to_pane.pop(terminal_id, None)

    with (
        patch.object(HerdrInboxService, "_fetch_snapshot", return_value=snapshot),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.clients.database.get_terminal_metadata", return_value=meta),
        patch("cli_agent_orchestrator.clients.database.list_terminals_by_session", return_value=[]),
        patch("cli_agent_orchestrator.clients.database.record_workspace_mapping"),
        patch.object(HerdrInboxService, "_label_still_live", return_value=False),
        patch.object(
            HerdrInboxService, "_route_spontaneous_terminal", new=AsyncMock(return_value=True)
        ),
        patch.object(HerdrInboxService, "_drop_terminal_identity", side_effect=_drop),
    ):
        asyncio.run(service._reconcile())

    backend.kill_session.assert_called_once_with(session)
    assert session in probe.marked
    assert session not in active_teardown_scope_keys()


# ---------------------------------------------------------------------------
# (d) create-rollback paths
# ---------------------------------------------------------------------------


def test_rollback_terminal_creation_session_kill_runs_under_session_mark():
    session = "cao-f720-rb"
    probe = _KillProbe()
    backend = MagicMock()
    backend.kill_session.side_effect = probe

    with (
        patch.object(terminal_service, "get_backend", return_value=backend),
        patch.object(terminal_service, "clear_session_env"),
    ):
        terminal_service._rollback_terminal_creation(
            "t-rb",
            session,
            "w-t-rb",
            session_created=True,
            window_created=True,
            fifo_attached=False,
            db_created=False,
        )

    assert session in probe.marked
    assert session not in active_teardown_scope_keys()


def test_rollback_terminal_creation_window_kill_runs_under_terminal_mark():
    """Window-only rollback marks the TERMINAL: the session may hold live peers."""
    session = "cao-f720-rb-win"
    probe = _KillProbe()
    backend = MagicMock()
    backend.kill_window.side_effect = probe

    with patch.object(terminal_service, "get_backend", return_value=backend):
        terminal_service._rollback_terminal_creation(
            "t-rb-win",
            session,
            "w-t-rb-win",
            session_created=False,
            window_created=True,
            fifo_attached=False,
            db_created=False,
        )

    assert "t-rb-win" in probe.marked
    assert session not in probe.marked  # peers on this session keep their ERROR
    assert "t-rb-win" not in active_teardown_scope_keys()


def test_roll_back_backend_create_locked_session_kill_runs_under_session_mark():
    session = "cao-f720-locked"
    probe = _KillProbe()
    backend = MagicMock()
    backend.kill_session.side_effect = probe

    with (
        patch.object(terminal_service, "get_backend", return_value=backend),
        patch.object(terminal_service, "clear_session_env"),
    ):
        terminal_service._roll_back_backend_create_locked(session, "w-locked", created_session=True)

    assert session in probe.marked
    assert session not in active_teardown_scope_keys()


def test_roll_back_backend_create_locked_window_kill_marks_a_known_terminal():
    """The committed-row caller passes terminal_id; the window kill is marked."""
    session = "cao-f720-locked-win"
    probe = _KillProbe()
    backend = MagicMock()
    backend.kill_window.side_effect = probe

    with patch.object(terminal_service, "get_backend", return_value=backend):
        terminal_service._roll_back_backend_create_locked(
            session, "w-locked", created_session=False, terminal_id="t-locked"
        )

    assert "t-locked" in probe.marked
    assert session not in probe.marked
    assert "t-locked" not in active_teardown_scope_keys()


def test_roll_back_cancelled_create_passes_the_committed_terminal_id():
    """Wiring test: the caller that HAS a row is the one that supplies the key."""
    seen: dict[str, object] = {}

    def _capture(session_name, window_name, *, created_session, terminal_id=None):
        seen["terminal_id"] = terminal_id

    with (
        patch.object(terminal_service, "_roll_back_backend_create_locked", side_effect=_capture),
        patch.object(terminal_service, "db_delete_terminal"),
        patch.object(terminal_service, "session_lifecycle_lock"),
    ):
        terminal_service._roll_back_cancelled_create(
            "cao-f720-cancel", "t-cancel", "w-cancel", created_session=False
        )

    assert seen["terminal_id"] == "t-cancel"


# ---------------------------------------------------------------------------
# (e) the session-scope producer, seen through the fleet projection
# ---------------------------------------------------------------------------


class _FleetBackend:
    """Inventory reader for build_fleet. Every window is gone."""

    def get_session_windows(self, _session):
        return []

    def get_history(self, *_a, **_k):
        return ""


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


def test_session_scope_mark_covers_its_own_session_only(monkeypatch):
    """AC: during finalize_session every row of THAT session projects non-ERROR,
    while a row of another session whose window vanished is still ERROR."""
    teardown_session = "cao-f720-proj"
    other_session = "cao-f720-other"
    database.create_terminal("aaaaaaaa", teardown_session, "w-a", "cline_cli")
    database.create_terminal("bbbbbbbb", teardown_session, "w-b", "cline_cli")
    database.create_terminal("cccccccc", other_session, "w-c", "cline_cli")

    monkeypatch.setattr(backend_registry, "_backend", _FleetBackend())
    monkeypatch.setattr(
        fleet_service.status_monitor,
        "get_boundary_observation",
        lambda _tid: _obs(TerminalStatus.IDLE),
    )

    projections: dict[str, list[str]] = {}

    def _kill(_name):
        projections["teardown"] = [
            row["status"] for row in fleet_service.build_fleet(teardown_session)["terminals"]
        ]
        projections["other"] = [
            row["status"] for row in fleet_service.build_fleet(other_session)["terminals"]
        ]
        return True

    backend = MagicMock()
    backend.kill_session.side_effect = _kill
    backend.session_exists_strict.side_effect = [True] + [False] * 8
    backend.session_exists.return_value = False

    with (
        patch.object(session_service, "clear_session_env"),
        patch.object(session_service, "dispatch_plugin_event"),
    ):
        session_service.finalize_session(teardown_session, backend=backend)

    assert projections["teardown"] == [TerminalStatus.IDLE.value] * 2
    assert projections["other"] == [TerminalStatus.ERROR.value]

    # And once the teardown is over, the suppression is gone.
    after = [row["status"] for row in fleet_service.build_fleet(teardown_session)["terminals"]]
    assert after == [TerminalStatus.ERROR.value] * 2
