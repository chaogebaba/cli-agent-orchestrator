"""F160-a — one-shot watchdog re-arm for a still-running deferred init.

Pinned behaviour:
  * first expiry of a live, pre-exposure init  -> re-armed, NOT settled
  * second expiry (retry already spent)        -> settled with the original code
  * F138 exposure crossed (incarnation active) -> settled immediately, no retry
  * initialize() already completed (shell_command persisted) -> no retry
  * init task dead/absent                      -> no retry
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.clients.database import TerminalModel, get_terminal_metadata
from cli_agent_orchestrator.services import terminal_service as terminals


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f160.db'}", connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    terminals._f160_retried_terminals.clear()
    yield sessions, engine
    terminals._f160_retried_terminals.clear()
    with terminals._deferred_tasks_lock:
        terminals._deferred_tasks_by_terminal.clear()
    engine.dispose()


def _make_caller(caller_id: str) -> None:
    db.create_terminal(caller_id, "cao-s", caller_id, "claude_code", "supervisor", caller_id=None)


def _make_pending(terminal_id: str, *, caller: str, started_at: datetime, deadline: float = 17.0):
    return db.create_terminal(
        terminal_id,
        "cao-s",
        terminal_id,
        "kiro_cli",
        "developer",
        caller_id=caller,
        init_state="init_pending",
        init_started_at=started_at,
        init_owner_epoch="00000000-0000-0000-0000-000000000001",
        init_deadline_s=deadline,
    )


def _backdate(sessions, terminal_id: str, seconds: float = 120.0) -> None:
    """Push init_started_at far enough back that the row is overdue."""
    with sessions() as session:
        session.execute(
            update(TerminalModel)
            .where(TerminalModel.id == terminal_id)
            .values(init_started_at=datetime.now(timezone.utc) - timedelta(seconds=seconds))
        )
        session.commit()


def _register_live_task(terminal_id: str) -> asyncio.Task:
    """Register a never-finishing deferred-init task record for terminal_id."""

    async def _forever():
        await asyncio.sleep(3600)

    task = asyncio.get_running_loop().create_task(_forever())
    with terminals._deferred_tasks_lock:
        terminals._deferred_tasks_by_terminal[terminal_id] = terminals._DeferredTaskRecord(
            task=task,
            loop=asyncio.get_running_loop(),
            generation="gen-f160",
            session_name="cao-s",
        )
    return task


async def _drop_task(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _started_at(terminal_id: str) -> datetime:
    meta = get_terminal_metadata(terminal_id)
    assert meta is not None
    return db._as_utc(meta["init_started_at"])


def _settled(terminal_id: str) -> bool:
    meta = get_terminal_metadata(terminal_id)
    return meta is None or meta["init_state"] in (
        "init_failed_notified",
        "init_failed_caller_gone",
    )


@pytest.mark.asyncio
async def test_first_expiry_is_rearmed_not_settled(isolated_db, caplog):
    import logging

    sessions, _engine = isolated_db
    terminal_id = "t-f160-a-" + uuid.uuid4().hex[:8]
    _make_caller("caller-f160-a")
    _make_pending(
        terminal_id,
        caller="caller-f160-a",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    task = _register_live_task(terminal_id)

    before = _started_at(terminal_id)
    with caplog.at_level(logging.INFO):
        count = await terminals.sweep_overdue_deferred_inits(None)

    assert count == 0
    meta = get_terminal_metadata(terminal_id)
    assert meta is not None and meta["init_state"] == "init_pending"
    assert _started_at(terminal_id) > before
    assert meta["init_deadline_s"] == 17.0
    assert "f160_deferred_init_retry" in caplog.text
    assert terminal_id in caplog.text
    assert terminal_id in terminals._f160_retried_terminals

    await _drop_task(task)


@pytest.mark.asyncio
async def test_second_expiry_settles_with_watchdog_code(isolated_db):
    sessions, _engine = isolated_db
    terminal_id = "t-f160-b-" + uuid.uuid4().hex[:8]
    _make_caller("caller-f160-b")
    _make_pending(
        terminal_id,
        caller="caller-f160-b",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    task = _register_live_task(terminal_id)

    assert await terminals.sweep_overdue_deferred_inits(None) == 0
    _backdate(sessions, terminal_id)

    assert await terminals.sweep_overdue_deferred_inits(None) == 1
    assert _settled(terminal_id)

    with sessions() as session:
        notices = session.query(db.InboxModel).filter(db.InboxModel.sender_id == terminal_id).all()
    assert len(notices) == 1
    assert "deferred_init_watchdog_deadline" in notices[0].message

    await _drop_task(task)


@pytest.mark.asyncio
async def test_exposure_crossed_settles_immediately(isolated_db):
    sessions, _engine = isolated_db
    terminal_id = "t-f160-c-" + uuid.uuid4().hex[:8]
    _make_caller("caller-f160-c")
    _make_pending(
        terminal_id,
        caller="caller-f160-c",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    task = _register_live_task(terminal_id)

    meta = get_terminal_metadata(terminal_id)
    assert meta is not None
    generation = int(meta.get("lifecycle_generation") or 0)
    incarnation_id = db.f138_reserve_incarnation(
        terminal_id=terminal_id,
        terminal_generation=generation,
        token="tok-" + uuid.uuid4().hex,
        token_hash="hash-" + uuid.uuid4().hex,
        owner_uid=1000,
        provider="kiro_cli",
    )
    with sessions() as session:
        session.execute(
            update(db.ProcessIncarnationModel)
            .where(db.ProcessIncarnationModel.id == incarnation_id)
            .values(state="active")
        )
        session.commit()

    assert await terminals.sweep_overdue_deferred_inits(None) == 1
    assert _settled(terminal_id)
    assert terminal_id not in terminals._f160_retried_terminals

    await _drop_task(task)


@pytest.mark.asyncio
async def test_completed_initialize_is_not_retried(isolated_db):
    """shell_command persisted means initialize() returned — delivery may have happened."""
    sessions, _engine = isolated_db
    terminal_id = "t-f160-d-" + uuid.uuid4().hex[:8]
    _make_caller("caller-f160-d")
    _make_pending(
        terminal_id,
        caller="caller-f160-d",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    task = _register_live_task(terminal_id)
    db.update_terminal_shell_command(terminal_id, "zsh")

    assert await terminals.sweep_overdue_deferred_inits(None) == 1
    assert _settled(terminal_id)

    await _drop_task(task)


@pytest.mark.asyncio
async def test_dead_init_task_is_not_retried(isolated_db):
    """No live task = no progress possible; re-arming would only delay the notice."""
    sessions, _engine = isolated_db
    terminal_id = "t-f160-e-" + uuid.uuid4().hex[:8]
    _make_caller("caller-f160-e")
    _make_pending(
        terminal_id,
        caller="caller-f160-e",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )

    assert await terminals.sweep_overdue_deferred_inits(None) == 1
    assert _settled(terminal_id)
