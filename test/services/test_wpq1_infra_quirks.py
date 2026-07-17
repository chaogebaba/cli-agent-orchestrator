"""Discriminating WPQ1 tests for deferred-init retention and replay."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.services import deferred_deadletter_service as deadletters
from cli_agent_orchestrator.services import terminal_service as terminals


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpq1.sqlite'}", connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    monkeypatch.setattr(deadletters, "DEFERRED_DEADLETTER_DIR", tmp_path / "deadletters")
    yield sessions
    engine.dispose()


def _pending(terminal_id: str = "worker", caller: str | None = "caller") -> None:
    db.create_terminal(
        terminal_id,
        "cao-s",
        terminal_id,
        "codex",
        "developer",
        caller_id=caller,
        init_state="init_pending",
        init_started_at=db._utcnow(),
        init_owner_epoch="00000000-0000-0000-0000-000000000001",
        init_deadline_s=60.0,
    )


def _payload() -> dict:
    return {
        "terminal_id": "worker",
        "caller_id": "caller",
        "owner_epoch": "00000000-0000-0000-0000-000000000001",
        "failure_token": "00000000-0000-0000-0000-000000000010",
        "notice": "deferred init failed",
        "stage": "h3_claim",
        "attempt_log": [{"attempt": 1, "exception": "ProgrammingError"}],
    }


def test_f15_deadletter_permissions_replay_and_commit_loss_dedup(isolated_db):
    db.create_terminal("caller", "cao-s", "caller", "claude_code", "supervisor")
    _pending()
    payload = _payload()

    path = deadletters.write_deferred_failure_deadletter(payload)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert deadletters.replay_deferred_failure_deadletters() == {
        "replayed": 1,
        "archived": 1,
        "failed": 0,
    }
    assert len(db.get_inbox_messages("caller")) == 1
    assert db.get_terminal_metadata("worker")["init_state"] == "init_failed_notified"

    # Commit-response-loss replay: the CAS reports already_claimed and archives
    # the recreated file without inserting a second notice.
    path = deadletters.write_deferred_failure_deadletter(payload)
    assert path.exists()
    assert deadletters.replay_deferred_failure_deadletters()["archived"] == 1
    assert len(db.get_inbox_messages("caller")) == 1


def test_f15_claim_logs_engine_pool_and_dbapi_identity(isolated_db, caplog):
    db.create_terminal("caller", "cao-s", "caller", "claude_code", "supervisor")
    _pending()
    caplog.set_level("INFO")

    result = db.claim_deferred_init_failure(
        "worker",
        caller_id="caller",
        failure_token="00000000-0000-0000-0000-000000000011",
        notice="failed",
    )

    assert result["status"] == "claimed_notified"
    assert "deferred_init_claim_connection" in caplog.text
    assert "engine=" in caplog.text and "dbapi=" in caplog.text and "pool=" in caplog.text


@pytest.mark.asyncio
async def test_f15_runtime_claim_retries_four_times_without_holding_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(deadletters, "DEFERRED_DEADLETTER_DIR", tmp_path / "deadletters")
    h3_calls = 0
    slot_held = False

    async def tracked(*args, **kwargs):
        nonlocal h3_calls, slot_held
        operation = args[3]
        assert not slot_held
        slot_held = True
        try:
            if operation == "h3_claim":
                h3_calls += 1
                raise RuntimeError("database connection closed")
            result = args[4](*args[5:], **kwargs)
            return result, 0.0
        finally:
            slot_held = False

    sleeps = AsyncMock(side_effect=lambda _delay: assert_slot_released(slot_held))

    monkeypatch.setattr(terminals, "_tracked_blocking", tracked)
    monkeypatch.setattr(terminals.asyncio, "sleep", sleeps)
    await terminals._claim_and_settle_deferred_failure(
        "worker",
        "generation",
        {
            "caller_id": "caller",
            "agent_profile": "developer",
            "provider": "codex",
            "init_deadline_s": 60.0,
            "init_owner_epoch": "00000000-0000-0000-0000-000000000001",
        },
        "provider_init_failed",
        None,
    )

    assert h3_calls == 4
    assert [call.args[0] for call in sleeps.await_args_list] == [1.0, 5.0, 25.0]
    files = list((tmp_path / "deadletters").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["stage"] == "h3_claim"
    assert len(payload["attempt_log"]) == 4


def assert_slot_released(slot_held: bool) -> None:
    assert slot_held is False


@pytest.mark.asyncio
async def test_f15_runtime_busy_exhaustion_deadletters_without_slow_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(deadletters, "DEFERRED_DEADLETTER_DIR", tmp_path / "deadletters")
    operations = []

    async def tracked(*args, **kwargs):
        operations.append(args[3])
        if args[3] == "h3_claim":
            raise RuntimeError("deferred_init_claim_busy_exhausted")
        return args[4](*args[5:], **kwargs), 0.0

    sleep = AsyncMock()
    monkeypatch.setattr(terminals, "_tracked_blocking", tracked)
    monkeypatch.setattr(terminals.asyncio, "sleep", sleep)

    await terminals._claim_and_settle_deferred_failure(
        "worker",
        "generation",
        {
            "caller_id": "caller",
            "agent_profile": "developer",
            "provider": "codex",
            "init_deadline_s": 60.0,
            "init_owner_epoch": "00000000-0000-0000-0000-000000000001",
        },
        "provider_init_failed",
        None,
    )

    assert operations == ["h3_claim", "deadletter"]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_f15_h5_busy_behavior_remains_fatal(monkeypatch):
    tracked = AsyncMock(side_effect=RuntimeError("deferred_init_claim_busy_exhausted"))
    monkeypatch.setattr(terminals, "_tracked_blocking", tracked)

    with pytest.raises(RuntimeError, match="deferred_init_claim_busy_exhausted"):
        await terminals._claim_and_settle_deferred_failure(
            "worker",
            "generation",
            {
                "caller_id": "caller",
                "agent_profile": "developer",
                "provider": "codex",
                "init_deadline_s": 60.0,
                "init_owner_epoch": "00000000-0000-0000-0000-000000000001",
            },
            "server_restart_during_deferred_init",
            None,
            fatal_claim_failure=True,
        )
    tracked.assert_awaited_once()


@pytest.mark.asyncio
async def test_f15_preclaim_validation_is_critical_deadletter(tmp_path, monkeypatch):
    monkeypatch.setattr(deadletters, "DEFERRED_DEADLETTER_DIR", tmp_path / "deadletters")

    async def tracked(*args, **kwargs):
        assert args[3] == "deadletter"
        return args[4](*args[5:], **kwargs), 0.0

    monkeypatch.setattr(terminals, "_tracked_blocking", tracked)
    await terminals._claim_and_settle_deferred_failure(
        "worker",
        "generation",
        {
            "caller_id": "caller",
            "agent_profile": "developer",
            "provider": "codex",
            "init_deadline_s": float("nan"),
            "init_owner_epoch": "00000000-0000-0000-0000-000000000001",
        },
        "provider_init_failed",
        None,
    )

    [path] = list((tmp_path / "deadletters").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["stage"] == "pre_claim_validation"
    assert payload["rejection_reason"] == "invalid_stored_deadline"


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("flush", db.NoticeInsertOutcome.FAILED_BEFORE_COMMIT),
        ("commit", db.NoticeInsertOutcome.UNCERTAIN_COMMIT),
        ("refresh", db.NoticeInsertOutcome.FAILED_AFTER_COMMIT),
        (None, db.NoticeInsertOutcome.INSERTED),
    ],
)
def test_f23_identity_notice_wrapper_reports_commit_phase(monkeypatch, phase, expected):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = object()
    session.add.side_effect = lambda row: setattr(row, "id", 1)
    if phase is not None:
        getattr(session, phase).side_effect = RuntimeError(phase)
    monkeypatch.setattr(db, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        db,
        "resolve_inbox_receiver",
        lambda _session, receiver: (receiver, None, None),
    )

    result = db.insert_identity_authority_notice(
        "message-trace:receiver", "caller", "[identity-authority] notice"
    )

    assert result == expected


@pytest.mark.asyncio
async def test_f21_deferred_owner_awaits_named_block_notice_to_persisted_caller(
    monkeypatch,
):
    notices = []
    provider = SimpleNamespace(
        blocked_wait_notifier=None,
        supports_reauth_rebind=False,
        shell_baseline=None,
    )

    async def initialize():
        await provider.blocked_wait_notifier("codex-update-available")
        raise RuntimeError("stop after notice")

    provider.initialize = initialize

    async def tracked(*args, **kwargs):
        return args[4](*args[5:], **kwargs), 0.0

    monkeypatch.setattr(terminals, "_tracked_blocking", tracked)
    monkeypatch.setattr(
        terminals,
        "get_terminal_metadata",
        lambda terminal_id: {"id": terminal_id} if terminal_id == "caller" else None,
    )
    monkeypatch.setattr(
        terminals,
        "create_inbox_message",
        lambda sender, receiver, message: notices.append((sender, receiver, message)),
    )
    monkeypatch.setattr(
        terminals,
        "claim_deferred_init_failure",
        lambda *_args, **_kwargs: {"status": "row_missing", "init_state": None},
    )

    terminals._schedule_deferred_init(
        provider,
        "worker",
        None,
        None,
        None,
        caller_snapshot={
            "caller_id": "caller",
            "agent_profile": "developer",
            "provider": "codex",
            "init_deadline_s": 60.0,
            "tmux_session": "cao-s",
        },
    )
    await asyncio.gather(*list(terminals._deferred_init_tasks))

    assert len(notices) == 1
    assert notices[0][0:2] == ("worker", "caller")
    assert "codex-update-available" in notices[0][2]
