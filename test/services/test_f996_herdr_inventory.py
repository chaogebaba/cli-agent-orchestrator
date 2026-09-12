"""F996: fleet window absence follows herdr's live pane inventory."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.backends import registry as backend_registry
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import fleet_service
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation

SESSION = "cao-f996"
WINDOWS = {
    "live0001": "live-window-a",
    "live0002": "live-window-b",
    "retired1": "retired-window",
}


def _response(key: str, rows: list[dict[str, str]]) -> MagicMock:
    return MagicMock(returncode=0, stdout=json.dumps({"result": {key: rows}}), stderr="")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> HerdrBackend:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.clear_terminal_metadata_cache()

    with patch.object(HerdrBackend, "_socket_is_live", return_value=True):
        backend = HerdrBackend(send_delay_ms=0)
    responses = {
        ("workspace", "list"): _response("workspaces", [{"workspace_id": "w1", "label": SESSION}]),
        ("tab", "list"): _response(
            "tabs",
            [
                {"tab_id": "w1:t1", "workspace_id": "w1", "label": WINDOWS["live0001"]},
                {"tab_id": "w1:t2", "workspace_id": "w1", "label": WINDOWS["retired1"]},
                {"tab_id": "w1:t3", "workspace_id": "w1", "label": WINDOWS["live0002"]},
            ],
        ),
        ("pane", "list"): _response(
            "panes",
            [
                {
                    "pane_id": "w1:p1",
                    "tab_id": "w1:t1",
                    "workspace_id": "w1",
                    # F926 #778: herdr may report unknown for a healthy
                    # wrapped launch; inventory liveness is pane ownership,
                    # not the agent_status tell.
                    "agent_status": "unknown",
                },
                {"pane_id": "w1:p3", "tab_id": "w1:t3", "workspace_id": "w1"},
            ],
        ),
    }
    backend._run_herdr = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda args, check=True: responses[tuple(args)]
    )
    monkeypatch.setattr(backend_registry, "_backend", backend)

    for terminal_id, window_name in WINDOWS.items():
        database.create_terminal(
            terminal_id,
            SESSION,
            window_name,
            "claude_code",
            agent_profile="chao_supervisor",
        )
    return backend


def _idle_observation() -> BoundaryObservation:
    return BoundaryObservation(
        observation_epoch="f996",
        status=TerminalStatus.IDLE,
        status_gen=None,
        input_gen=0,
        seq=0,
        last_non_ready_seq=None,
        last_ready_seq=None,
    )


def test_build_fleet_marks_only_the_retired_herdr_pane_absent(
    env: HerdrBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        fleet_service.status_monitor,
        "get_boundary_observation",
        lambda _terminal_id: _idle_observation(),
    )
    monkeypatch.setattr(fleet_service.status_monitor, "get_condition", lambda *_args: None)
    monkeypatch.setattr(fleet_service, "_observe_model_effort", lambda _row: (None, None))
    monkeypatch.setattr(
        fleet_service._wt_legacy_egress,
        "record_fleet_override",
        lambda terminal_id, reason, detail: overrides.append((terminal_id, reason, detail)),
    )

    rows = {row["id"]: row for row in fleet_service.build_fleet(SESSION)["terminals"]}

    assert rows["live0001"]["status"] == TerminalStatus.IDLE.value
    assert rows["live0002"]["status"] == TerminalStatus.IDLE.value
    assert rows["retired1"]["status"] == TerminalStatus.ERROR.value
    assert {(terminal_id, reason) for terminal_id, reason, _detail in overrides} == {
        ("retired1", "window_absent")
    }
