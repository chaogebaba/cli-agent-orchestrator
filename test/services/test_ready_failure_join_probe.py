"""Claim attacked: quiescence joins and observes a decided ready Future.

Round: WPM4-A diff gate r8.
Expected post-fix semantics: when post-decision commit I/O fails before the
shared budget expires, quiescence must not report success; the retained
ReadyCommitInvariantBreach must be surfaced or reconciled visibly.
Run: cd cli-agent-orchestrator && uv run --frozen pytest -q ../tmp/orch/promote/wpm4a-r8/test_ready_failure_join_probe.py
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
        f"sqlite:///{tmp_path / 'failure-join.db'}",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    yield engine
    engine.dispose()


@pytest.mark.asyncio
async def test_completed_decided_failure_is_observed_by_quiescence(
    isolated_db, monkeypatch, caplog,
):
    terminal_id = "ready-fast-failure"
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

    async def release_inside_budget() -> None:
        await asyncio.sleep(0.02)
        release.set()

    releaser = asyncio.create_task(release_inside_budget())
    with pytest.raises(db.ReadyCommitInvariantBreach):
        await terminals.quiesce_deferred_terminal(terminal_id, timeout_s=0.2)
    await releaser
    assert db.get_terminal_metadata(terminal_id)["init_state"] == "init_pending"
    assert f"ready_commit_invariant_breach terminal={terminal_id}" in caplog.text

