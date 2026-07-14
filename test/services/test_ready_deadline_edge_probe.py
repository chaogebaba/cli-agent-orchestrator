"""Claim attacked: ready completion at the quiescence deadline has one branch.

Round: WPM4-A diff gate r9.
Expected post-fix semantics: a successful decided commit racing the exact
budget edge yields either joined-ready success or mutation-in-flight followed
by one late reconciliation, never dual/neither ownership or a false timeout.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r9/test_ready_deadline_edge_probe.py
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.services import terminal_service as terminals


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'deadline-edge.db'}",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    yield engine
    engine.dispose()


@pytest.mark.asyncio
async def test_ready_completion_at_deadline_has_one_lawful_owner(
    isolated_db, monkeypatch,
):
    original_do_commit = isolated_db.dialect.do_commit
    outcomes: list[str] = []

    for iteration in range(60):
        terminal_id = f"ready-edge-{iteration}"
        db.create_terminal(
            terminal_id,
            "cao-s",
            terminal_id,
            "grok_cli",
            "developer",
            caller_id="caller",
            init_state="init_pending",
            init_started_at=db._utcnow(),
            init_owner_epoch="00000000-0000-0000-0000-000000000001",
            init_deadline_s=17.0,
        )
        entered = threading.Event()
        release = threading.Event()

        def blocked_commit(connection):
            entered.set()
            release.wait(1)
            original_do_commit(connection)

        monkeypatch.setattr(isolated_db.dialect, "do_commit", blocked_commit)
        provider = SimpleNamespace(
            initialize=AsyncMock(), supports_reauth_rebind=False, shell_baseline=None,
        )
        terminals._schedule_deferred_init(
            provider,
            terminal_id,
            None,
            None,
            None,
            caller_snapshot={
                "caller_id": "caller",
                "agent_profile": "developer",
                "provider": "grok_cli",
                "init_deadline_s": 3.0,
            },
        )
        assert await asyncio.to_thread(entered.wait, 1)
        record = terminals._deferred_tasks_by_terminal[terminal_id]
        registered_call = record.current_call
        assert registered_call is not None

        delay = (0.005, 0.010, 0.015)[iteration % 3]
        timer = threading.Timer(delay, release.set)
        timer.start()
        try:
            await terminals.quiesce_deferred_terminal(terminal_id, timeout_s=0.010)
        except RuntimeError as exc:
            assert str(exc) == "quiesce_timeout_mutation_in_flight"
            outcomes.append("mutation_in_flight")
        else:
            outcomes.append("joined_ready")
        finally:
            timer.join(1)
            release.set()

        for _ in range(200):
            if registered_call.future.done():
                break
            await asyncio.sleep(0.001)
        assert registered_call.future.done()
        assert registered_call.future.exception() is None
        assert registered_call.future.result() is True
        assert db.get_terminal_metadata(terminal_id)["init_state"] == "ready"

    assert outcomes.count("joined_ready") >= 15
    assert outcomes.count("mutation_in_flight") >= 15
    assert len(outcomes) == 60

