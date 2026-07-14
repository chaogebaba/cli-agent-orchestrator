"""Claim attacked: nonblocking winner fallback reconciles its real late result.

Round: WPM4-A diff gate r11.
Expected post-fix semantics: when the commit thread holds the winner lock past
the quiescence budget, quiescence returns mutation-in-flight responsively; once
released, the real SQLite ready commit completes and one reconciler records it.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r11/test_ready_nonblocking_winner_reconcile_probe.py
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.services import terminal_service as terminals


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'winner-reconcile.db'}",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


@pytest.mark.asyncio
async def test_nonblocking_winner_fallback_reconciles_late_ready(
    isolated_db, caplog,
):
    terminal_id = "ready-nonblocking-reconcile"
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
    before_commit_entered = threading.Event()
    allow_commit = threading.Event()
    lock_held = threading.Event()
    release_lock = threading.Event()

    def before_commit(_session):
        before_commit_entered.set()
        allow_commit.wait(1)

    event.listen(isolated_db.class_, "before_commit", before_commit)
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
    assert await asyncio.to_thread(before_commit_entered.wait, 1)
    record = terminals._deferred_tasks_by_terminal[terminal_id]
    call = record.current_call
    assert call is not None

    def hold_winner() -> None:
        with call.ready_winner_lock:
            lock_held.set()
            release_lock.wait(1)

    holder = threading.Thread(target=hold_winner)
    holder.start()
    assert await asyncio.to_thread(lock_held.wait, 1)
    allow_commit.set()
    await asyncio.sleep(0.005)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(30):
            ticks += 1
            await asyncio.sleep(0.001)

    ticking = asyncio.create_task(ticker())
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="quiesce_timeout_mutation_in_flight"):
            await terminals.quiesce_deferred_terminal(terminal_id, timeout_s=0.02)
        elapsed = time.monotonic() - started
        assert elapsed < 0.1
        assert ticks >= 3
        assert call.result_owner == "reconciler"

        release_lock.set()
        holder.join(1)
        for _ in range(200):
            if "reconcile_settlement_result" in caplog.text:
                break
            await asyncio.sleep(0.002)
        assert call.future.done() and call.future.exception() is None
        assert call.future.result() is True
        assert db.get_terminal_metadata(terminal_id)["init_state"] == "ready"
        assert (
            f"reconcile_settlement_result terminal={terminal_id}" in caplog.text
        )
    finally:
        release_lock.set()
        allow_commit.set()
        holder.join(1)
        await asyncio.gather(ticking, return_exceptions=True)
        event.remove(isolated_db.class_, "before_commit", before_commit)

