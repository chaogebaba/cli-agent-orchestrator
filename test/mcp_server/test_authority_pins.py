"""MCP envelopes for the WPQ13 authority-pin tools."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import Base, TerminalModel
from cli_agent_orchestrator.mcp_server import server


@pytest.fixture
def mcp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'authority-pin.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    with sessions.begin() as db:
        db.add_all(
            [
                TerminalModel(
                    id="aaaaaaaa",
                    tmux_session="cao-test",
                    tmux_window="owner",
                    provider="codex",
                    agent_profile="supervisor",
                    lifecycle_generation=1,
                ),
                TerminalModel(
                    id="bbbbbbbb",
                    tmux_session="cao-test",
                    tmux_window="worker",
                    provider="codex",
                    agent_profile="developer",
                    caller_id="aaaaaaaa",
                    lifecycle_generation=1,
                ),
            ]
        )
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    yield sessions
    engine.dispose()


def test_registry_tools_are_exposed():
    assert hasattr(server, "pin_authority")
    assert hasattr(server, "update_pin")
    assert hasattr(server, "verify_pin")


def test_missing_terminal_id_is_structured_transport_error(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    result = asyncio.run(server.verify_pin(str(tmp_path / "authority.md")))
    assert result == {"success": False, "error": {"code": "missing_terminal_id"}}


def test_mcp_tools_use_real_registry_and_exact_envelopes(mcp_db, monkeypatch, tmp_path: Path):
    path = tmp_path / "authority.md"
    path.write_text("authority")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    result = asyncio.run(
        server.pin_authority("bbbbbbbb", [{"file_path": str(path), "sha256": sha}])
    )
    assert result["task_key"] == "bbbbbbbb"
    assert result["results"][0]["current_version"] == 1

    monkeypatch.setenv("CAO_TERMINAL_ID", "bbbbbbbb")
    assert asyncio.run(server.verify_pin(str(path))) == {"verdict": "VALID", "version": 1}
    monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")
    updated = hashlib.sha256(b"amended").hexdigest()
    amended = asyncio.run(server.update_pin("bbbbbbbb", str(path), updated))
    assert amended["current_version"] == 2
    path.write_text("amended")
    monkeypatch.setenv("CAO_TERMINAL_ID", "bbbbbbbb")
    assert asyncio.run(server.verify_pin(str(path)))["verdict"] == "SUPERSEDED"

    result = asyncio.run(server.update_pin("bbbbbbbb", "relative", sha))
    assert result == {"success": False, "error": {"code": "path_not_absolute"}}


def test_mcp_verify_wrapper_returns_real_unpinned_verdict(mcp_db, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CAO_TERMINAL_ID", "bbbbbbbb")
    assert asyncio.run(server.verify_pin(str(tmp_path / "unregistered.md"))) == {
        "verdict": "UNPINNED"
    }
