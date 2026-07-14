"""Claim attacked: quiescence cannot lose a completed registered Future.

Round: WPM4-A diff gate r9.
Expected post-fix semantics: if the concurrent Future's done callback clears
record.current_call before the asyncio task consumes its exception, a
simultaneous quiescence still joins and propagates ReadyCommitInvariantBreach.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r9/test_ready_call_snapshot_race_probe.py
"""

from __future__ import annotations

import asyncio
import sqlite3
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
        f"sqlite:///{tmp_path / 'snapshot-race.db'}",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    yield engine
    engine.dispose()


@pytest.mark.asyncio
async def test_done_callback_cannot_clear_call_before_quiescence_snapshot(
    isolated_db, monkeypatch, caplog,
):
    terminal_id = "ready-snapshot-race"
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
    decided = threading.Event()
    release = threading.Event()

    def fail_after_decision(_connection):
        decided.set()
        release.wait(1)
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(isolated_db.dialect, "do_commit", fail_after_decision)
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
    assert await asyncio.to_thread(decided.wait, 1)
    record = terminals._deferred_tasks_by_terminal[terminal_id]
    registered_call = record.current_call
    assert registered_call is not None

    concurrent_done = threading.Event()
    registered_call.future.add_done_callback(lambda _future: concurrent_done.set())
    loop = asyncio.get_running_loop()

    # This callback occupies the loop while the worker completes. The quiesce
    # task is already queued ahead of wrap_future's completion callback, so it
    # deterministically sees the concurrent done callback's registry mutation.
    loop.call_soon(lambda: concurrent_done.wait(1))
    quiesce = asyncio.create_task(
        terminals.quiesce_deferred_terminal(terminal_id, timeout_s=0.2)
    )
    release.set()

    try:
        await quiesce
    except db.ReadyCommitInvariantBreach:
        pass
    else:
        assert record.current_call is None
        pytest.fail(
            "quiescence returned success after the done callback cleared current_call"
        )
    assert registered_call.future.done()
    assert isinstance(registered_call.future.exception(), db.ReadyCommitInvariantBreach)
    assert db.get_terminal_metadata(terminal_id)["init_state"] == "init_pending"
    assert f"ready_commit_invariant_breach terminal={terminal_id}" in caplog.text
