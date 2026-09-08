"""F826 (#683) — build_fleet merges observations additively.

Covers the D6 projection (model_obs/effort_obs additive keys), the configured
columns are NEVER overwritten (D1 / AC1 conflict), AC4 (no body bytes retained
or surfaced), AC5 (one bad source never blocks the fleet), and the two
remaining AC6 mutants: overwrite configured column, and skip identity binding
(cross-attribution).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.backends import registry as backend_registry
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import fleet_service
from cli_agent_orchestrator.services import model_effort_observation as meo
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


class _Backend:
    def __init__(self, windows):
        self.windows = windows

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
    meo.reset_caches()
    monkeypatch.setattr(
        fleet_service.status_monitor,
        "get_boundary_observation",
        lambda _tid: BoundaryObservation(
            observation_epoch="e",
            status=TerminalStatus.IDLE,
            status_gen=None,
            input_gen=0,
            seq=0,
            last_non_ready_seq=None,
            last_ready_seq=None,
        ),
    )
    monkeypatch.setattr(fleet_service.status_monitor, "get_condition", lambda *_a, **_k: None)
    yield
    meo.reset_caches()


def _make_terminal(tid, window, provider, *, model=None, effort=None, session_id=None):
    database.create_terminal(tid, "cao-f826", window, provider, agent_profile="dev")
    with database.SessionLocal() as db:
        term = db.query(database.TerminalModel).filter(database.TerminalModel.id == tid).first()
        term.resolved_model = model
        term.reasoning_effort = effort
        term.provider_session_id = session_id
        db.commit()
    database.clear_terminal_metadata_cache()


def _row(session="cao-f826", tid=None):
    fleet = fleet_service.build_fleet(session)
    rows = fleet["terminals"]
    if tid is None:
        return rows[0]
    return next(r for r in rows if r["id"] == tid)


def test_claude_sidecar_projects_live_model_obs(env, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "observe").mkdir(parents=True)
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home, raising=False)
    (home / "observe" / "aaaaaaaa.json").write_text(
        json.dumps(
            {
                "terminal_id": "aaaaaaaa",
                "model": "Opus",
                "effort": "high",
                "event_time": time.time_ns(),
            }
        )
    )
    _make_terminal("aaaaaaaa", "w-aaaaaaaa", "claude_code", model="opus", effort="high")
    monkeypatch.setattr(backend_registry, "_backend", _Backend({"w-aaaaaaaa"}))
    row = _row()
    assert row["model_obs"] is not None
    assert row["model_obs"]["value"] == "Opus"
    assert row["model_obs"]["marker"] == "L"
    # D1 / Do-NOT: the configured column is NEVER overwritten.
    assert row["resolved_model"] == "opus"
    assert row["model_obs"]["configured"] == "opus"


def test_additive_keys_always_present(env, monkeypatch):
    """model_obs/effort_obs are unconditional siblings (old-server fallback = None)."""
    _make_terminal("bbbbbbbb", "w-bbbbbbbb", "grok_cli", model="grok-4.6")
    monkeypatch.setattr(backend_registry, "_backend", _Backend({"w-bbbbbbbb"}))
    row = _row()
    assert "model_obs" in row
    assert "effort_obs" in row
    # grok is not observable -> None (TUI renders configured [C]).
    assert row["model_obs"] is None


def test_mutant_overwrite_configured_column_is_not_done(env, monkeypatch, tmp_path):
    """AC6 mutant: overwriting resolved_model with the observation. We assert the
    configured column and the observation are SEPARATE keys."""
    home = tmp_path / "home"
    (home / "observe").mkdir(parents=True)
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home, raising=False)
    (home / "observe" / "cccccccc.json").write_text(
        json.dumps(
            {
                "terminal_id": "cccccccc",
                "model": "sonnet",
                "effort": "low",
                "event_time": time.time_ns(),
            }
        )
    )
    _make_terminal("cccccccc", "w-cccccccc", "claude_code", model="opus", effort="high")
    monkeypatch.setattr(backend_registry, "_backend", _Backend({"w-cccccccc"}))
    row = _row()
    assert row["resolved_model"] == "opus"  # NOT "sonnet"
    assert row["model_obs"]["value"] == "sonnet"  # observation is separate
    assert row["reasoning_effort"] == "high"


def test_mutant_skip_identity_binding_no_cross_attribution(env, monkeypatch, tmp_path):
    """AC6 mutant: skipping identity binding ("newest file") would cross-attribute.
    Two claude seats each read their OWN terminal-id sidecar, never the other's."""
    home = tmp_path / "home"
    (home / "observe").mkdir(parents=True)
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home, raising=False)
    now = time.time_ns()
    (home / "observe" / "dddddddd.json").write_text(
        json.dumps(
            {"terminal_id": "dddddddd", "model": "opus-seat-d", "effort": "high", "event_time": now}
        )
    )
    (home / "observe" / "eeeeeeee.json").write_text(
        json.dumps(
            {
                "terminal_id": "eeeeeeee",
                "model": "sonnet-seat-e",
                "effort": "low",
                "event_time": now + 5,
            }
        )
    )
    _make_terminal("dddddddd", "w-dddddddd", "claude_code", model="opus")
    _make_terminal("eeeeeeee", "w-eeeeeeee", "claude_code", model="sonnet")
    monkeypatch.setattr(backend_registry, "_backend", _Backend({"w-dddddddd", "w-eeeeeeee"}))
    row_d = _row(tid="dddddddd")
    row_e = _row(tid="eeeeeeee")
    assert row_d["model_obs"]["value"] == "opus-seat-d"
    assert row_e["model_obs"]["value"] == "sonnet-seat-e"  # never seat d's newer file


def test_ac4_no_body_sentinel_in_projection(env, monkeypatch, tmp_path):
    """AC4: a rollout stuffed with a body sentinel never leaks into the output."""
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions" / "2026" / "09" / "07"
    sessions.mkdir(parents=True)
    sentinel = "SENTINEL_BODY_TEXT_DO_NOT_LEAK"
    rollout = sessions / "rollout-2026-09-07T10-00-00-ffffffff-uuid.jsonl"
    lines = [
        json.dumps(
            {
                "type": "turn_context",
                "timestamp": "2026-09-07T10:00:00Z",
                "payload": {"model": "gpt-5.6-sol", "effort": "high"},
            }
        ),
        json.dumps(
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": sentinel},
            }
        ),
    ]
    rollout.write_text("\n".join(lines) + "\n")

    class _Home:
        home = codex_home

    monkeypatch.setattr(fleet_service, "provider_home", lambda _p: _Home())
    _make_terminal(
        "ffffffff", "w-ffffffff", "codex", model="gpt-5.6-sol", session_id="ffffffff-uuid"
    )
    monkeypatch.setattr(backend_registry, "_backend", _Backend({"w-ffffffff"}))
    row = _row()
    blob = json.dumps(row)
    assert sentinel not in blob
    assert row["model_obs"]["value"] == "gpt-5.6-sol"


def test_ac5_one_bad_source_never_blocks_fleet(env, monkeypatch, tmp_path):
    """AC5: an adapter that raises degrades that row, not the whole fleet."""
    home = tmp_path / "home"
    (home / "observe").mkdir(parents=True)
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", home, raising=False)

    def _boom(*a, **k):
        raise RuntimeError("bad source")

    monkeypatch.setattr(meo, "observe_provider", _boom)
    _make_terminal("11111111", "w-11111111", "claude_code", model="opus")
    monkeypatch.setattr(backend_registry, "_backend", _Backend({"w-11111111"}))
    row = _row()
    # Fleet still builds; the row degrades to no observation.
    assert row["model_obs"] is None
    assert row["resolved_model"] == "opus"


def test_codex_ambiguous_session_id_binds_nothing(env, monkeypatch, tmp_path):
    """SHOULD-2: two rollouts matching the id is ambiguous -> bind nothing (no guess)."""
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    for suffix in ("a", "b"):
        (sessions / f"rollout-2026-09-07T10-00-0{suffix}-dup-uuid.jsonl").write_text(
            json.dumps({"type": "turn_context", "payload": {"model": "m", "effort": "high"}}) + "\n"
        )

    class _Home:
        home = codex_home

    monkeypatch.setattr(fleet_service, "provider_home", lambda _p: _Home())
    _make_terminal("22222222", "w-22222222", "codex", session_id="dup-uuid")
    monkeypatch.setattr(backend_registry, "_backend", _Backend({"w-22222222"}))
    row = _row()
    assert row["model_obs"] is None  # ambiguous -> no binding
