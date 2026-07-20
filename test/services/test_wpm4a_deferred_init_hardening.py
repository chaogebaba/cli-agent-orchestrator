"""Frozen WPM4-A r14 drains, derived from the PIN rather than implementation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.providers.base import (
    RetryableArtifactValidation,
    TerminalArtifactValidation,
)
from cli_agent_orchestrator.services import terminal_service as terminals
from cli_agent_orchestrator.services.deferred_dispatcher import (
    DaemonDispatcher,
    DeferredExecutorSaturated,
)
from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpm4a.db'}", connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    yield sessions, engine
    engine.dispose()


def _pending(terminal_id: str, *, caller: str | None = "caller", deadline: float = 17.0):
    return db.create_terminal(
        terminal_id,
        "cao-s",
        terminal_id,
        "grok_cli",
        "developer",
        caller_id=caller,
        init_state="init_pending",
        init_started_at=db._utcnow(),
        init_owner_epoch="00000000-0000-0000-0000-000000000001",
        init_deadline_s=deadline,
    )


def test_typed_provider_validation_split(tmp_path, monkeypatch):
    from cli_agent_orchestrator.providers.codex import CodexProvider
    from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    codex = object.__new__(CodexProvider)
    with pytest.raises(RetryableArtifactValidation) as missing:
        codex.validate_session_artifact("u", "/work")
    assert missing.value.code == "session_artifact_missing"

    sessions = tmp_path / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    for prefix in ("one", "two"):
        (sessions / f"rollout-{prefix}-u.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "u"}}) + "\n"
        )
    with pytest.raises(TerminalArtifactValidation) as ambiguous:
        codex.validate_session_artifact("u", "/work")
    assert ambiguous.value.code == "session_artifact_ambiguous"

    grok = object.__new__(GrokCliProvider)
    with pytest.raises(RetryableArtifactValidation) as inert:
        grok.validate_session_artifact("g", "/work")
    assert inert.value.code == "session_artifact_missing_or_inert"


@pytest.mark.parametrize("raw", ["", "bad", "0", "-1", "nan", "inf", "1e309", "700"])
def test_deadline_env_invalid_matrix_warns_and_defaults(raw, monkeypatch, caplog, tmp_path):
    from cli_agent_orchestrator.services import settings_service as settings

    monkeypatch.setattr(settings, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("CAO_ARTIFACT_VALIDATE_DEADLINE_S", raw)
    settings._server_settings_cache = None
    assert settings.get_server_settings()["artifact_validate_deadline_s"] == 60.0
    assert "using default 60.0" in caplog.text
    assert "CAO_ARTIFACT_VALIDATE_DEADLINE_S" in caplog.text


def test_terminal_migration_is_atomic_and_preserves_legacy_data(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE terminals (id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, "
            "tmux_window TEXT NOT NULL, provider TEXT NOT NULL, agent_profile TEXT, "
            "last_active DATETIME)"
        )
        conn.execute("INSERT INTO terminals VALUES ('t','s','w','codex','dev','2026-01-01')")
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    db._migrate_terminals_schema()
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT id,tmux_session,tmux_window,provider,agent_profile,init_state " "FROM terminals"
        ).fetchall() == [("t", "s", "w", "codex", "dev", "ready")]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO terminals (id,tmux_session,tmux_window,provider,init_state) "
                "VALUES ('bad','s','w','codex','init_pending')"
            )
        conn.execute(
            "UPDATE terminals SET init_failure_token=? WHERE id='t'",
            ("00000000-0000-0000-0000-000000000020",),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE terminals SET init_failure_token=? WHERE id='t'",
                ("00000000-0000-0000-0000-000000000021",),
            )


def test_terminal_migration_failure_is_fatal_and_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "partial.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE terminals (id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, "
            "tmux_window TEXT NOT NULL, provider TEXT NOT NULL, init_state TEXT, "
            "init_started_at TEXT, init_owner_epoch TEXT, init_failure_token TEXT, "
            "init_deadline_s REAL)"
        )
        for terminal_id in ("a", "b"):
            conn.execute(
                "INSERT INTO terminals VALUES (?,?,?,?,?,?,?,?,?)",
                (terminal_id, "s", terminal_id, "codex", "ready", None, None, "dup", None),
            )
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path)
    with pytest.raises(sqlite3.IntegrityError):
        db._migrate_terminals_schema()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM terminals").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='terminals_wpm4a_legacy'"
            ).fetchone()
            is None
        )


def test_h3_notice_and_state_commit_together_and_deduplicate(isolated_db):
    db.create_terminal("caller", "s", "caller", "codex")
    _pending("worker")
    first = db.claim_deferred_init_failure(
        "worker",
        caller_id="caller",
        failure_token="00000000-0000-0000-0000-000000000010",
        notice="code=x deadline_s=17.0 token=t worker=worker profile=developer provider=grok_cli",
    )
    second = db.claim_deferred_init_failure(
        "worker",
        caller_id="caller",
        failure_token="00000000-0000-0000-0000-000000000011",
        notice="duplicate",
    )
    assert first["status"] == "claimed_notified"
    assert second["status"] == "already_claimed"
    assert db.get_terminal_metadata("worker")["init_failure_token"].endswith("0010")
    assert len(db.get_inbox_messages("caller")) == 1


def test_h3_insert_failure_rolls_back_claim_and_token(isolated_db):
    sessions, _engine = isolated_db
    db.create_terminal("caller", "s", "caller", "codex")
    _pending("worker")

    def reject(_mapper, _connection, _target):
        raise RuntimeError("insert_failed")

    event.listen(db.InboxModel, "before_insert", reject)
    try:
        with pytest.raises(RuntimeError, match="insert_failed"):
            db.claim_deferred_init_failure(
                "worker",
                caller_id="caller",
                failure_token="00000000-0000-0000-0000-000000000012",
                notice="x",
            )
    finally:
        event.remove(db.InboxModel, "before_insert", reject)
    row = db.get_terminal_metadata("worker")
    assert row["init_state"] == "init_pending"
    assert row["init_failure_token"] is None
    with sessions() as session:
        assert session.query(db.InboxModel).count() == 0


def test_h3_missing_receiver_commits_caller_gone_once(isolated_db):
    _pending("worker", caller="gone")
    result = db.claim_deferred_init_failure(
        "worker",
        caller_id="gone",
        failure_token="00000000-0000-0000-0000-000000000013",
        notice="x",
    )
    assert result["status"] == "claimed_caller_gone"
    assert db.get_terminal_metadata("worker")["init_state"] == "init_failed_caller_gone"


def test_deferred_init_failure_marks_barrier_member_gone_and_fires_same_transaction(
    isolated_db,
):
    sessions, _engine = isolated_db
    db.create_terminal("caller", "s", "caller", "codex", "supervisor")
    _pending("worker")
    db.create_inbox_message("caller", "worker", "task", dispatch_barrier={"label": "init-failure"})
    result = db.claim_deferred_init_failure(
        "worker",
        caller_id="caller",
        failure_token="00000000-0000-0000-0000-000000000014",
        notice="init failed",
    )
    assert result["status"] == "claimed_notified"
    with sessions() as session:
        member = session.query(db.CallbackBarrierMemberModel).one()
        barrier = session.query(db.CallbackBarrierModel).one()
        assert member.state == "GONE"
        assert barrier.state == "FIRED_COMPLETE"
        assert barrier.combined_message_id is not None


def test_atomic_terminal_warm_delete_rolls_back_both_on_second_operation(isolated_db):
    sessions, engine = isolated_db
    db.create_terminal_with_warm_intent(
        terminal_id="worker",
        tmux_session="s",
        tmux_window="w",
        provider="codex",
        agent_profile="dev",
        allowed_tools=None,
        caller_id=None,
        parent_base_name="base",
        fork_mode="fork",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER reject_terminal_delete BEFORE DELETE ON terminals "
                "BEGIN SELECT RAISE(ABORT, 'terminal_delete_failed'); END"
            )
        )
    with pytest.raises(IntegrityError):
        db.delete_terminal_and_warm_intent("worker", preserve_warm_intent=False)
    with sessions() as session:
        assert session.query(db.TerminalModel).count() == 1
        assert session.query(db.WarmIntentModel).count() == 1


def test_atomic_delete_preserves_keep_bases_intent(isolated_db):
    sessions, _engine = isolated_db
    db.create_terminal_with_warm_intent(
        terminal_id="worker",
        tmux_session="s",
        tmux_window="w",
        provider="codex",
        agent_profile="dev",
        allowed_tools=None,
        caller_id=None,
        parent_base_name="base",
        fork_mode="fork",
    )
    assert db.delete_terminal_and_warm_intent("worker", preserve_warm_intent=True) == {
        "terminal_deleted": True,
        "intent_deleted": False,
    }
    with sessions() as session:
        assert session.query(db.TerminalModel).count() == 0
        assert session.query(db.WarmIntentModel).count() == 1


def test_retention_cleanup_uses_atomic_terminal_warm_intent_seam(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from datetime import datetime, timedelta

    from cli_agent_orchestrator.services import cleanup_service

    sessions, _engine = isolated_db
    db.create_terminal_with_warm_intent(
        terminal_id="worker",
        tmux_session="s",
        tmux_window="w",
        provider="codex",
        agent_profile="dev",
        allowed_tools=None,
        caller_id=None,
        parent_base_name="base",
        fork_mode="fork",
    )
    with sessions.begin() as session:
        session.query(db.TerminalModel).filter_by(id="worker").update(
            {"last_active": datetime.now() - timedelta(days=60)}
        )
    calls: list[tuple[str, bool]] = []

    def atomic_delete(terminal_id, *, preserve_warm_intent):
        calls.append((terminal_id, preserve_warm_intent))
        return db.delete_terminal_and_warm_intent(
            terminal_id,
            preserve_warm_intent=preserve_warm_intent,
        )

    monkeypatch.setattr(cleanup_service, "SessionLocal", sessions)
    monkeypatch.setattr(cleanup_service, "delete_terminal_and_warm_intent", atomic_delete)
    monkeypatch.setattr(cleanup_service, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(cleanup_service, "TERMINAL_LOG_DIR", tmp_path / "terminals")
    monkeypatch.setattr(cleanup_service, "MEMORY_BASE_DIR", tmp_path / "memory")
    cleanup_service.cleanup_old_data()

    assert calls == [("worker", False)]
    with sessions() as session:
        assert session.query(db.TerminalModel).count() == 0
        assert session.query(db.WarmIntentModel).count() == 0


def test_startup_purge_never_defaults_absent_init_state_to_ready(monkeypatch):
    monkeypatch.setattr(
        terminals,
        "db_list_all_terminals",
        lambda: [{"id": "legacy-shape", "tmux_session": "s", "tmux_window": "w"}],
    )
    backend = MagicMock()
    backend.get_history.side_effect = RuntimeError("gone")
    monkeypatch.setattr(terminals, "get_backend", lambda: backend)
    deleted = MagicMock()
    monkeypatch.setattr(terminals, "delete_terminal_and_warm_intent", deleted)
    assert terminals.purge_stale_terminal_records() == 0
    deleted.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_saturation_keeps_event_loop_responsive():
    dispatcher = DaemonDispatcher(max_workers=1, max_queue=1)
    gate = threading.Event()
    running = asyncio.create_task(dispatcher.run("a", "g", "abandonable", "block", gate.wait))
    await asyncio.sleep(0.01)
    ticks = 0

    async def ticker():
        nonlocal ticks
        until = time.monotonic() + 0.04
        while time.monotonic() < until:
            ticks += 1
            await asyncio.sleep(0.002)

    with pytest.raises(DeferredExecutorSaturated):
        await asyncio.gather(
            dispatcher.run(
                "b",
                "g",
                "abandonable",
                "queued",
                lambda: None,
                deadline=time.monotonic() + 0.03,
            ),
            ticker(),
        )
    assert ticks > 5
    gate.set()
    await running


@pytest.mark.asyncio
async def test_queued_admission_is_cancellable_without_leaking_slot():
    dispatcher = DaemonDispatcher(max_workers=1, max_queue=1)
    gate = threading.Event()
    running = asyncio.create_task(dispatcher.run("a", "g", "abandonable", "block", gate.wait))
    await asyncio.sleep(0.01)
    queued = asyncio.create_task(dispatcher.run("b", "g", "mutating", "queued", lambda: "wrong"))
    await asyncio.sleep(0.01)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    gate.set()
    await running
    result, _grant = await dispatcher.run("c", "g", "abandonable", "after", lambda: "ok")
    assert result == "ok"


@pytest.mark.asyncio
async def test_dispatcher_workers_are_daemon_threads():
    dispatcher = DaemonDispatcher(max_workers=1)
    result, _grant = await dispatcher.run(
        "a", "g", "abandonable", "probe", lambda: threading.current_thread().daemon
    )
    assert result is True


@pytest.mark.asyncio
async def test_dispatcher_uses_slot_grant_not_delayed_validator_entry(monkeypatch):
    from cli_agent_orchestrator.services import deferred_dispatcher as module

    real_thread = threading.Thread

    class DelayedThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            timer = real_thread(target=lambda: (time.sleep(0.03), self.target()), daemon=True)
            timer.start()

    monkeypatch.setattr(module.threading, "Thread", DelayedThread)
    dispatcher = DaemonDispatcher(max_workers=1)
    deadline = time.monotonic() + 0.01
    calls: list[float] = []
    result, grant = await dispatcher.run(
        "worker",
        "g",
        "abandonable",
        "validate",
        lambda: calls.append(time.monotonic()) or "ready",
        deadline=deadline,
    )
    assert result == "ready"
    assert grant <= deadline < calls[0]


@pytest.mark.asyncio
async def test_h1_retries_only_validation_then_succeeds(monkeypatch):
    calls = 0

    class Provider:
        def validate_session_artifact(self, _uuid, _cwd):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RetryableArtifactValidation("session_artifact_missing")

    monkeypatch.setattr(terminals, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(terminals, "_deferred_worker_live", lambda _terminal: True)
    prepared = terminals._PreparedRuntimeIdentity("u", "/work", "bash", "first_time")
    await terminals._validate_deferred_artifact(Provider(), prepared, "worker", "g", 0.1)
    assert calls == 3


@pytest.mark.asyncio
async def test_h1_lawful_past_deadline_result_is_final(monkeypatch):
    calls = 0

    class Provider:
        def validate_session_artifact(self, _uuid, _cwd):
            nonlocal calls
            calls += 1
            time.sleep(0.025)
            raise RetryableArtifactValidation("session_artifact_missing")

    prepared = terminals._PreparedRuntimeIdentity("u", "/work", "bash", "first_time")
    with pytest.raises(RetryableArtifactValidation):
        await terminals._validate_deferred_artifact(Provider(), prepared, "worker", "g", 0.01)
    assert calls == 1


@pytest.mark.asyncio
async def test_ready_commits_only_after_initial_send(monkeypatch):
    events: list[str] = []
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )
    monkeypatch.setattr(terminals, "send_input", lambda *_a, **_k: events.append("send"))
    monkeypatch.setattr(
        terminals,
        "_confirm_worker_started_or_resubmit",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        terminals,
        "mark_terminal_init_ready",
        lambda _terminal, **_kwargs: events.append("ready") or True,
    )
    snapshot = {
        "caller_id": "caller",
        "agent_profile": "dev",
        "provider": "grok_cli",
        "init_deadline_s": 1.0,
    }
    terminals._schedule_deferred_init(
        provider,
        "worker",
        "task",
        OrchestrationType.ASSIGN,
        None,
        caller_snapshot=snapshot,
    )
    await asyncio.gather(*list(terminals._deferred_init_tasks))
    assert events == ["send", "ready"]


@pytest.mark.asyncio
async def test_quiesce_wins_between_ready_guard_and_persist(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    ready: list[str] = []
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )
    original_commit = terminals._commit_ready_if_generation_current

    def barrier_commit(terminal_id, generation):
        entered.set()
        release.wait()
        return original_commit(terminal_id, generation)

    def guarded_ready(
        terminal_id,
        *,
        should_commit=None,
        on_committed=None,
        **_winner_callbacks,
    ):
        if should_commit is not None and not should_commit():
            return False
        ready.append(terminal_id)
        if on_committed is not None:
            on_committed()
        return True

    monkeypatch.setattr(terminals, "_commit_ready_if_generation_current", barrier_commit)
    monkeypatch.setattr(terminals, "mark_terminal_init_ready", guarded_ready)
    terminals._schedule_deferred_init(
        provider,
        "ready-race",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
        },
    )
    assert await asyncio.to_thread(entered.wait, 1)
    with pytest.raises(RuntimeError, match="deferred_task_quiesce_timeout"):
        await terminals.quiesce_deferred_terminal("ready-race", timeout_s=0.02)
    release.set()
    await asyncio.sleep(0.05)
    assert ready == []


@pytest.mark.asyncio
async def test_quiesce_wins_after_ready_sync_call_starts(
    isolated_db,
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    ticks = 0
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )
    _pending("inverse-ready")
    sessions, _engine = isolated_db

    def before_commit(_session):
        # Query.update() and both Python guards have completed; the real DB
        # commit has not started.
        entered.set()
        release.wait()

    async def ticker():
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await asyncio.sleep(0.001)

    event.listen(sessions.class_, "before_commit", before_commit)
    terminals._schedule_deferred_init(
        provider,
        "inverse-ready",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
        },
    )
    assert await asyncio.to_thread(entered.wait, 1)
    ticking = asyncio.create_task(ticker())
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="deferred_task_quiesce_timeout"):
        await terminals.quiesce_deferred_terminal("inverse-ready", timeout_s=0.02)
    elapsed = time.monotonic() - started
    assert (
        terminals._deferred_tasks_by_terminal["inverse-ready"].current_call.ready_winner
        == "timeout"
    )
    release.set()
    await ticking
    await asyncio.sleep(0.03)

    assert elapsed < 0.1
    assert ticks >= 3
    assert db.get_terminal_metadata("inverse-ready")["init_state"] == "init_pending"
    event.remove(sessions.class_, "before_commit", before_commit)


@pytest.mark.asyncio
async def test_quiesce_reports_ready_when_commit_precedes_helper_publication(
    isolated_db,
    caplog,
):
    committed = threading.Event()
    release = threading.Event()
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )
    _pending("ready-published-late")
    sessions, _engine = isolated_db

    def blocked_after_commit(_session):
        committed.set()
        release.wait()

    event.listen(sessions.class_, "after_commit", blocked_after_commit)
    terminals._schedule_deferred_init(
        provider,
        "ready-published-late",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
        },
    )
    assert await asyncio.to_thread(committed.wait, 1)
    with pytest.raises(RuntimeError, match="quiesce_timeout_mutation_in_flight"):
        await terminals.quiesce_deferred_terminal(
            "ready-published-late",
            timeout_s=0.02,
        )
    assert db.get_terminal_metadata("ready-published-late")["init_state"] == "ready"
    release.set()
    await asyncio.sleep(0.03)
    assert "reconcile_settlement_result terminal=ready-published-late" in caplog.text
    event.remove(sessions.class_, "after_commit", blocked_after_commit)


@pytest.mark.asyncio
async def test_quiesce_resolves_ready_before_prepended_after_commit_observer(
    isolated_db,
    caplog,
):
    committed = threading.Event()
    release = threading.Event()
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )
    _pending("ready-prepended-observer")
    sessions, _engine = isolated_db

    def blocked_after_commit(_session):
        committed.set()
        release.wait()

    event.listen(sessions.class_, "after_commit", blocked_after_commit, insert=True)
    terminals._schedule_deferred_init(
        provider,
        "ready-prepended-observer",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
        },
    )
    assert await asyncio.to_thread(committed.wait, 1)
    with pytest.raises(RuntimeError, match="quiesce_timeout_mutation_in_flight"):
        await terminals.quiesce_deferred_terminal(
            "ready-prepended-observer",
            timeout_s=0.02,
        )
    assert db.get_terminal_metadata("ready-prepended-observer")["init_state"] == "ready"
    release.set()
    await asyncio.sleep(0.03)
    assert "reconcile_settlement_result terminal=ready-prepended-observer" in caplog.text
    event.remove(sessions.class_, "after_commit", blocked_after_commit)


@pytest.mark.asyncio
async def test_quiesce_resolves_ready_after_dbapi_commit_before_observers(
    isolated_db,
    monkeypatch,
    caplog,
):
    committed = threading.Event()
    release = threading.Event()
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )
    _pending("ready-dbapi-committed")
    _sessions, engine = isolated_db
    original_do_commit = engine.dialect.do_commit

    def blocked_do_commit(connection):
        original_do_commit(connection)
        committed.set()
        release.wait()

    monkeypatch.setattr(engine.dialect, "do_commit", blocked_do_commit)
    terminals._schedule_deferred_init(
        provider,
        "ready-dbapi-committed",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
        },
    )
    assert await asyncio.to_thread(committed.wait, 1)
    with pytest.raises(RuntimeError, match="quiesce_timeout_mutation_in_flight"):
        await terminals.quiesce_deferred_terminal(
            "ready-dbapi-committed",
            timeout_s=0.02,
        )
    assert db.get_terminal_metadata("ready-dbapi-committed")["init_state"] == "ready"
    release.set()
    await asyncio.sleep(0.03)
    assert "reconcile_settlement_result terminal=ready-dbapi-committed" in caplog.text


@pytest.mark.asyncio
async def test_quiesce_reports_mutation_in_flight_after_decision_before_durability(
    isolated_db,
    monkeypatch,
    caplog,
):
    decided = threading.Event()
    release = threading.Event()
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )
    _pending("ready-decided")
    _sessions, engine = isolated_db
    original_do_commit = engine.dialect.do_commit

    def blocked_do_commit(connection):
        decided.set()
        release.wait()
        original_do_commit(connection)

    monkeypatch.setattr(engine.dialect, "do_commit", blocked_do_commit)
    terminals._schedule_deferred_init(
        provider,
        "ready-decided",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
        },
    )
    assert await asyncio.to_thread(decided.wait, 1)
    assert db.get_terminal_metadata("ready-decided")["init_state"] == "init_pending"
    with pytest.raises(RuntimeError, match="quiesce_timeout_mutation_in_flight"):
        await terminals.quiesce_deferred_terminal("ready-decided", timeout_s=0.02)
    call = terminals._deferred_tasks_by_terminal["ready-decided"].current_call
    assert call is not None
    assert call.ready_winner == "commit_decided"
    assert not call.future.done()
    assert db.get_terminal_metadata("ready-decided")["init_state"] == "init_pending"
    release.set()
    await asyncio.sleep(0.03)
    assert db.get_terminal_metadata("ready-decided")["init_state"] == "ready"
    assert "reconcile_settlement_result terminal=ready-decided" in caplog.text


@pytest.mark.asyncio
async def test_ready_commit_failure_after_decision_is_loud(
    isolated_db,
    monkeypatch,
    caplog,
):
    decided = threading.Event()
    release = threading.Event()
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )
    _pending("ready-io-failure")
    _sessions, engine = isolated_db

    def failing_do_commit(_connection):
        decided.set()
        release.wait()
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(engine.dialect, "do_commit", failing_do_commit)
    terminals._schedule_deferred_init(
        provider,
        "ready-io-failure",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
        },
    )
    assert await asyncio.to_thread(decided.wait, 1)
    with pytest.raises(RuntimeError, match="quiesce_timeout_mutation_in_flight"):
        await terminals.quiesce_deferred_terminal("ready-io-failure", timeout_s=0.02)
    release.set()
    await asyncio.sleep(0.05)
    assert db.get_terminal_metadata("ready-io-failure")["init_state"] == "init_pending"
    assert "ready_commit_invariant_breach terminal=ready-io-failure" in caplog.text
    assert (
        "reconcile_settlement_result terminal=ready-io-failure " "error=ReadyCommitInvariantBreach"
    ) in caplog.text


def test_ready_commit_failure_survives_rollback_failure(
    isolated_db,
    monkeypatch,
    caplog,
):
    sessions, engine = isolated_db
    _pending("ready-rollback-failure")

    def failing_commit(_connection):
        raise sqlite3.OperationalError("disk full")

    def failing_rollback(_session):
        raise RuntimeError("rollback_failed")

    monkeypatch.setattr(engine.dialect, "do_commit", failing_commit)
    monkeypatch.setattr(sessions.class_, "rollback", failing_rollback)
    with pytest.raises(db.ReadyCommitInvariantBreach) as breach:
        db.mark_terminal_init_ready(
            "ready-rollback-failure",
            decide_commit=lambda: True,
            commit_is_decided=lambda: True,
        )

    assert isinstance(breach.value.__cause__, db.OperationalError)
    assert isinstance(breach.value.__cause__.orig, sqlite3.OperationalError)
    assert "disk full" in str(breach.value.__cause__.orig)
    assert "ready_commit_invariant_breach terminal=ready-rollback-failure" in caplog.text
    assert "ready_commit_rollback_failed terminal=ready-rollback-failure" in caplog.text


@pytest.mark.asyncio
async def test_shutdown_reports_permanently_blocked_decided_commit_as_in_flight(
    isolated_db,
    monkeypatch,
    caplog,
):
    decided = threading.Event()
    release = threading.Event()
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )
    _pending("ready-shutdown-blocked")
    _sessions, engine = isolated_db
    original_do_commit = engine.dialect.do_commit

    def blocked_do_commit(connection):
        decided.set()
        release.wait()
        original_do_commit(connection)

    monkeypatch.setattr(engine.dialect, "do_commit", blocked_do_commit)
    terminals._schedule_deferred_init(
        provider,
        "ready-shutdown-blocked",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
        },
    )
    assert await asyncio.to_thread(decided.wait, 1)
    await asyncio.wait_for(
        terminals.shutdown_deferred_tasks(timeout_s=0.02),
        timeout=0.1,
    )
    assert (
        "deferred_shutdown_timeout terminal=ready-shutdown-blocked "
        "code=quiesce_timeout_mutation_in_flight"
    ) in caplog.text
    call = terminals._deferred_tasks_by_terminal["ready-shutdown-blocked"].current_call
    assert call is not None and not call.future.done()
    assert db.get_terminal_metadata("ready-shutdown-blocked")["init_state"] == "init_pending"
    release.set()
    await asyncio.sleep(0.03)


@pytest.mark.asyncio
async def test_send_failure_claims_before_settlement_and_never_marks_ready(monkeypatch):
    events: list[str] = []
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )
    monkeypatch.setattr(
        terminals, "send_input", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("send"))
    )
    monkeypatch.setattr(
        terminals,
        "claim_deferred_init_failure",
        lambda *_a, **_k: events.append("claim")
        or {"status": "claimed_notified", "init_state": "init_failed_notified"},
    )
    monkeypatch.setattr(
        terminals,
        "_settle_deferred_failure_sync",
        lambda *_a, **_k: events.append("settle") or {"status": "deleted"},
    )
    monkeypatch.setattr(
        terminals,
        "mark_terminal_init_ready",
        lambda _terminal, **_kwargs: events.append("ready") or True,
    )
    terminals._schedule_deferred_init(
        provider,
        "worker",
        "task",
        OrchestrationType.ASSIGN,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
        },
    )
    await asyncio.gather(*list(terminals._deferred_init_tasks))
    assert events == ["claim", "settle"]


@pytest.mark.asyncio
async def test_failure_routing_uses_schedule_time_caller_snapshot(monkeypatch):
    observed: list[str | None] = []
    provider = SimpleNamespace(
        initialize=AsyncMock(side_effect=RuntimeError("init")),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )

    def claim(*_args, **kwargs):
        observed.append(kwargs.get("caller_id"))
        return {"status": "claimed_notified", "init_state": "init_failed_notified"}

    monkeypatch.setattr(terminals, "claim_deferred_init_failure", claim)
    monkeypatch.setattr(
        terminals, "get_terminal_metadata", lambda _terminal: {"caller_id": "replacement"}
    )
    monkeypatch.setattr(
        terminals, "_settle_deferred_failure_sync", lambda *_a, **_k: {"status": "deleted"}
    )
    terminals._schedule_deferred_init(
        provider,
        "snapshot-worker",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "original",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
            "tmux_session": "s",
        },
    )
    await asyncio.gather(*list(terminals._deferred_init_tasks))
    assert observed == ["original"]


@pytest.mark.asyncio
async def test_quiesce_joins_underlying_abandonable_future(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )

    def blocked_send(*_args, **_kwargs):
        started.set()
        release.wait()

    monkeypatch.setattr(terminals, "send_input", blocked_send)
    monkeypatch.setattr(
        terminals,
        "mark_terminal_init_ready",
        lambda _terminal, **_kwargs: True,
    )
    terminals._schedule_deferred_init(
        provider,
        "worker",
        "task",
        OrchestrationType.ASSIGN,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
        },
    )
    await asyncio.to_thread(started.wait, 1)
    with pytest.raises(RuntimeError, match="deferred_task_quiesce_timeout"):
        await terminals.quiesce_deferred_terminal("worker", timeout_s=0.03)
    release.set()
    await asyncio.sleep(0.03)


@pytest.mark.asyncio
async def test_external_delete_waits_for_deferred_future_before_core(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    core_called = threading.Event()
    provider = SimpleNamespace(
        initialize=AsyncMock(),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )

    def blocked_send(*_args, **_kwargs):
        started.set()
        release.wait()

    monkeypatch.setattr(terminals, "send_input", blocked_send)
    monkeypatch.setattr(
        terminals,
        "mark_terminal_init_ready",
        lambda _terminal, **_kwargs: True,
    )
    monkeypatch.setattr(
        terminals,
        "_delete_terminal_core",
        lambda *_a, **_k: core_called.set() or True,
    )
    terminals._schedule_deferred_init(
        provider,
        "delete-worker",
        "task",
        OrchestrationType.ASSIGN,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
            "tmux_session": "s",
        },
    )
    await asyncio.to_thread(started.wait, 1)
    deleting = asyncio.create_task(asyncio.to_thread(terminals.delete_terminal, "delete-worker"))
    await asyncio.sleep(0.03)
    assert not core_called.is_set()
    release.set()
    assert await deleting is True
    assert core_called.is_set()


@pytest.mark.asyncio
async def test_mutating_timeout_reconciles_late_h3_completion(monkeypatch, caplog):
    started = threading.Event()
    release = threading.Event()
    settled = threading.Event()
    provider = SimpleNamespace(
        initialize=AsyncMock(side_effect=RuntimeError("init")),
        supports_reauth_rebind=False,
        shell_baseline=None,
    )

    def claim(*_args, **_kwargs):
        started.set()
        release.wait()
        return {"status": "claimed_notified", "init_state": "init_failed_notified"}

    monkeypatch.setattr(terminals, "claim_deferred_init_failure", claim)
    monkeypatch.setattr(
        terminals,
        "_settle_deferred_failure_sync",
        lambda *_a, **_k: settled.set() or {"status": "deleted"},
    )
    terminals._schedule_deferred_init(
        provider,
        "mutation-worker",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "dev",
            "provider": "grok_cli",
            "init_deadline_s": 3.0,
            "tmux_session": "s",
        },
    )
    await asyncio.to_thread(started.wait, 1)
    with pytest.raises(RuntimeError, match="quiesce_timeout_mutation_in_flight"):
        await terminals.quiesce_deferred_terminal("mutation-worker", timeout_s=0.02)
    release.set()
    await asyncio.to_thread(settled.wait, 1)
    await asyncio.sleep(0.02)
    assert "reconcile_h3_committed" in caplog.text


@pytest.mark.asyncio
async def test_reconciler_closed_audit_codes(monkeypatch, caplog):
    import concurrent.futures

    rolled_back = concurrent.futures.Future()
    rolled_back.set_exception(RuntimeError("rollback"))
    await terminals._late_mutation_reconciler("worker", "h3_claim", rolled_back)

    settled = concurrent.futures.Future()
    settled.set_result({"status": "retained"})
    await terminals._late_mutation_reconciler("worker", "settlement", settled)

    deleted = concurrent.futures.Future()
    deleted.set_result({"terminal_deleted": True})
    await terminals._late_mutation_reconciler("worker", "delete", deleted)
    assert "reconcile_h3_rolled_back" in caplog.text
    assert "reconcile_settlement_result" in caplog.text
    assert "reconcile_delete_result" in caplog.text


@pytest.mark.asyncio
async def test_h5_uses_stored_deadline_and_notifies_once(isolated_db, monkeypatch):
    db.create_terminal("caller", "s", "caller", "codex")
    _pending("worker", deadline=17.0)
    monkeypatch.setattr(
        terminals,
        "_settle_deferred_failure_sync",
        lambda *_a, **_k: {"status": "retained"},
    )
    await terminals.recover_deferred_inits(owner_epoch=terminals.SERVER_INIT_OWNER_EPOCH)
    messages = db.get_inbox_messages("caller")
    assert len(messages) == 1
    assert messages[0].message.startswith(
        "code=server_restart_during_deferred_init deadline_s=17.0 token="
    )
    await terminals.recover_deferred_inits(owner_epoch=terminals.SERVER_INIT_OWNER_EPOCH)
    assert len(db.get_inbox_messages("caller")) == 1


@pytest.mark.asyncio
async def test_h5_busy_exhaustion_fails_startup(isolated_db, monkeypatch):
    db.create_terminal("caller", "s", "caller", "codex")
    _pending("busy-worker")
    monkeypatch.setattr(
        terminals,
        "claim_deferred_init_failure",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("deferred_init_claim_busy_exhausted")),
    )
    with pytest.raises(RuntimeError, match="deferred_init_claim_busy_exhausted"):
        await terminals.recover_deferred_inits(owner_epoch=terminals.SERVER_INIT_OWNER_EPOCH)


def test_teardown_intent_supersedes_and_consumes_once(isolated_db):
    first = db.begin_teardown_intent("W", "S")
    assert db.settle_teardown_intent("W", first["generation"], issued=True)
    second = db.begin_teardown_intent("W", "S")
    assert second["generation"] == first["generation"] + 1
    assert db.settle_teardown_intent("W", second["generation"], issued=True)
    assert db.consume_current_teardown_intent("W", ttl_s=60)["generation"] == second["generation"]
    assert db.consume_current_teardown_intent("W", ttl_s=60) is None


def test_failed_close_intent_is_void_not_ttl_authority(isolated_db):
    intent = db.begin_teardown_intent("W", "S")
    assert db.settle_teardown_intent("W", intent["generation"], issued=False)
    assert db.get_teardown_intent("W")["state"] == "void"
    assert db.consume_current_teardown_intent("W", ttl_s=60) is None


def test_workspace_mapping_retires_reused_session_generation(isolated_db):
    db.record_workspace_mapping("W1", "S")
    db.record_workspace_mapping("W2", "S")
    assert db.resolve_workspace_mapping("W1") is None
    assert db.resolve_workspace_mapping("W2") == "S"
    assert db.current_workspace_for_session("S") == "W2"


def test_raw_creation_rollback_logs_helper_failure_and_continues(monkeypatch, caplog):
    stopped: list[str] = []
    monkeypatch.setattr(
        terminals,
        "delete_terminal_and_warm_intent",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("busy")),
    )
    monkeypatch.setattr(
        terminals.fifo_manager, "stop_reader", lambda terminal: stopped.append(terminal)
    )
    backend = MagicMock()
    monkeypatch.setattr(terminals, "get_backend", lambda: backend)
    terminals._rollback_terminal_creation("worker", "S", "W", False, True, True, True)
    assert caplog.text.count("create_rollback_cleanup_failed") == 1
    assert stopped == ["worker"]
    backend.kill_window.assert_called_once_with("S", "W")


def test_raw_creation_rollback_uses_one_nonpreserving_helper(monkeypatch):
    helper = MagicMock(return_value={"terminal_deleted": True, "intent_deleted": True})
    monkeypatch.setattr(terminals, "delete_terminal_and_warm_intent", helper)
    backend = MagicMock()
    monkeypatch.setattr(terminals, "get_backend", lambda: backend)
    monkeypatch.setattr(terminals.fifo_manager, "stop_reader", MagicMock())
    terminals._rollback_terminal_creation("worker", "S", "W", False, True, True, True)
    helper.assert_called_once_with("worker", preserve_warm_intent=False)


@pytest.mark.parametrize("returncode,issued", [(0, True), (1, False)])
def test_herdr_close_intent_is_committed_before_command_and_voids_failure(
    monkeypatch,
    returncode,
    issued,
):
    from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

    order: list[str] = []
    backend = object.__new__(HerdrBackend)
    backend._workspace_cache = {"S": ("W", time.time())}
    monkeypatch.setattr(backend, "_resolve_workspace_id", lambda _session: "W")
    monkeypatch.setattr(
        backend,
        "_run_herdr",
        lambda *_a, **_k: order.append("command") or SimpleNamespace(returncode=returncode),
    )
    monkeypatch.setattr(
        db,
        "begin_teardown_intent",
        lambda *_a: order.append("intent") or {"generation": 4},
    )
    settled: list[bool] = []
    monkeypatch.setattr(
        db,
        "settle_teardown_intent",
        lambda *_a, **kwargs: settled.append(kwargs["issued"]) or True,
    )
    assert backend.kill_session("S") is issued
    assert order == ["intent", "command"]
    assert settled == [issued]


def test_herdr_close_exception_voids_issuing_intent(monkeypatch):
    from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

    backend = object.__new__(HerdrBackend)
    backend._workspace_cache = {"S": ("W", time.time())}
    monkeypatch.setattr(backend, "_resolve_workspace_id", lambda _session: "W")
    monkeypatch.setattr(
        backend, "_run_herdr", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("close"))
    )
    monkeypatch.setattr(db, "begin_teardown_intent", lambda *_a: {"generation": 4})
    settled: list[bool] = []
    monkeypatch.setattr(
        db,
        "settle_teardown_intent",
        lambda *_a, **kwargs: settled.append(kwargs["issued"]) or True,
    )
    with pytest.raises(RuntimeError, match="close"):
        backend.kill_session("S")
    assert settled == [False]


@pytest.mark.asyncio
async def test_delayed_proven_close_cannot_delete_recreated_session(isolated_db, monkeypatch):
    db.record_workspace_mapping("W1", "S")
    intent = db.begin_teardown_intent("W1", "S")
    assert db.settle_teardown_intent("W1", intent["generation"], issued=True)
    db.record_workspace_mapping("W2", "S")
    db.create_terminal(
        "new-worker",
        "S",
        "new-worker",
        "grok_cli",
        "developer",
        caller_id="caller",
        init_state="init_pending",
        init_started_at=db._utcnow(),
        init_owner_epoch="00000000-0000-0000-0000-000000000001",
        init_deadline_s=17.0,
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        terminals, "_delete_terminal_core", lambda terminal, **_kwargs: deleted.append(terminal)
    )
    service = HerdrInboxService(socket_path="/unused")
    await service._route_workspace_close("W1", proven=True)
    assert deleted == []
    assert db.get_teardown_intent("W1")["state"] == "consumed"
    assert db.get_terminal_metadata("new-worker")["init_state"] == "init_pending"


@pytest.mark.asyncio
async def test_proven_close_is_class3_zero_notice(isolated_db, monkeypatch):
    db.record_workspace_mapping("W", "cao-s")
    intent = db.begin_teardown_intent("W", "cao-s")
    db.settle_teardown_intent("W", intent["generation"], issued=True)
    _pending("worker")
    order: list[str] = []

    async def quiesce(rows):
        order.append("quiesce:" + rows[0]["id"])

    monkeypatch.setattr(terminals, "quiesce_deferred_terminals", quiesce)
    monkeypatch.setattr(
        terminals,
        "_delete_terminal_core",
        lambda terminal, **_kwargs: order.append("delete:" + terminal),
    )
    monkeypatch.setattr(
        terminals,
        "_claim_and_settle_deferred_failure",
        AsyncMock(side_effect=AssertionError("proven close must not notify")),
    )
    service = HerdrInboxService(socket_path="/unused")
    await service._route_workspace_close("W", proven=True)
    assert order == ["quiesce:worker", "delete:worker"]


@pytest.mark.asyncio
async def test_duplicate_issued_close_events_have_one_owned_route_and_zero_notices(
    isolated_db,
    monkeypatch,
):
    db.record_workspace_mapping("W", "cao-s")
    intent = db.begin_teardown_intent("W", "cao-s")
    assert db.settle_teardown_intent("W", intent["generation"], issued=True)
    _pending("worker")
    entered = asyncio.Event()
    release = asyncio.Event()
    deleted: list[str] = []
    notices: list[str] = []

    async def pause_after_consumption(_rows):
        entered.set()
        await release.wait()

    async def class2_quiesce(_terminal, **_kwargs):
        return None

    async def notice(_terminal, _generation, _snapshot, code, *_args, **_kwargs):
        notices.append(code)

    monkeypatch.setattr(terminals, "quiesce_deferred_terminals", pause_after_consumption)
    monkeypatch.setattr(terminals, "quiesce_deferred_terminal", class2_quiesce)
    monkeypatch.setattr(terminals, "_claim_and_settle_deferred_failure", notice)
    monkeypatch.setattr(
        terminals,
        "_delete_terminal_core",
        lambda terminal, **_kwargs: deleted.append(terminal),
    )
    service = HerdrInboxService(socket_path="/unused")
    event = {"workspace_id": "W"}
    service._handle_lifecycle_event("workspace.closed", event)
    await entered.wait()
    service._handle_lifecycle_event("workspace.closed", event)
    await asyncio.sleep(0)
    assert len(service._workspace_close_routes) == 1
    release.set()
    await asyncio.gather(*service._lifecycle_tasks)

    assert deleted == ["worker"]
    assert notices == []
    assert db.resolve_workspace_mapping("W") is None


@pytest.mark.asyncio
async def test_unproven_close_routes_pending_worker_class2(isolated_db, monkeypatch):
    db.record_workspace_mapping("W", "cao-s")
    _pending("worker")
    order: list[str] = []

    async def quiesce(terminal, **_kwargs):
        order.append("quiesce:" + terminal)

    async def claim(terminal, _generation, _snapshot, code, _registry, *_args, **_kwargs):
        order.append(f"claim:{terminal}:{code}")

    monkeypatch.setattr(terminals, "quiesce_deferred_terminal", quiesce)
    monkeypatch.setattr(terminals, "_claim_and_settle_deferred_failure", claim)
    service = HerdrInboxService(socket_path="/unused")
    await service._route_workspace_close("W", proven=False)
    assert order == ["quiesce:worker", "claim:worker:worker_vanished"]


@pytest.mark.asyncio
async def test_issuing_intent_ack_wait_is_background_and_generation_current(
    isolated_db,
    monkeypatch,
):
    intent = db.begin_teardown_intent("W", "S")
    routed: list[tuple[str, bool]] = []
    service = HerdrInboxService(socket_path="/unused")

    async def route(workspace, *, proven):
        routed.append((workspace, proven))

    monkeypatch.setattr(service, "_route_workspace_close", route)
    before = time.monotonic()
    service._handle_lifecycle_event("workspace.closed", {"workspace_id": "W"})
    assert time.monotonic() - before < 0.02
    await asyncio.sleep(0.01)
    db.settle_teardown_intent("W", intent["generation"], issued=True)
    await asyncio.sleep(0.07)
    assert routed == [("W", True)]
