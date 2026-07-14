"""Claim attacked: rollback failure cannot mask a late ready invariant breach.

Round: WPM4-A diff gate r8.
Expected post-fix semantics: after a mutation-in-flight timeout, a decided
commit failure plus rollback failure still raises ReadyCommitInvariantBreach,
preserves the commit failure as cause, and is observed by the live reconciler.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r8/test_ready_rollback_reconcile_probe.py
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
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
        f"sqlite:///{tmp_path / 'rollback-reconcile.db'}",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    yield sessions, engine
    engine.dispose()


@pytest.mark.asyncio
async def test_rollback_failure_is_reconciled_after_timeout(
    isolated_db, monkeypatch, caplog,
):
    terminal_id = "ready-rollback-reconcile"
    sessions, engine = isolated_db
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

    def fail_rollback(_session):
        raise RuntimeError("rollback_failed")

    monkeypatch.setattr(engine.dialect, "do_commit", fail_after_decision)
    monkeypatch.setattr(sessions.class_, "rollback", fail_rollback)
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

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="quiesce_timeout_mutation_in_flight"):
        await terminals.quiesce_deferred_terminal(terminal_id, timeout_s=0.02)
    elapsed = time.monotonic() - started
    assert elapsed < 0.1
    call = terminals._deferred_tasks_by_terminal[terminal_id].current_call
    assert call is not None and not call.future.done()
    release.set()

    for _ in range(100):
        if "reconcile_settlement_result" in caplog.text:
            break
        await asyncio.sleep(0.005)

    assert call.future.done()
    breach = call.future.exception()
    assert isinstance(breach, db.ReadyCommitInvariantBreach)
    assert isinstance(breach.__cause__, db.OperationalError)
    assert isinstance(breach.__cause__.orig, sqlite3.OperationalError)
    assert "disk full" in str(breach.__cause__.orig)
    assert f"ready_commit_invariant_breach terminal={terminal_id}" in caplog.text
    assert f"ready_commit_rollback_failed terminal={terminal_id}" in caplog.text
    assert (
        f"reconcile_settlement_result terminal={terminal_id} "
        "error=ReadyCommitInvariantBreach"
    ) in caplog.text
