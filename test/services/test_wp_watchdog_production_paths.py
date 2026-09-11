"""Production-entry probes for WP watchdog delegation laws."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base, InboxModel, MailboxModel, TerminalModel
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.services import mailbox_service, terminal_service
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog
from cli_agent_orchestrator.services.status_monitor import status_monitor


@pytest.fixture
def prod_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpwd.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _terminal(db, terminal_id: str, *, caller_id: str | None = None) -> None:
    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session="wpwd",
            tmux_window=terminal_id,
            provider="grok_cli",
            agent_profile="developer",
            caller_id=caller_id,
            init_state="ready",
            lifecycle_generation=1,
        )
    )


@pytest.mark.parametrize("park_warm", [False, True])
@pytest.mark.parametrize("logical", [False, True])
def test_http_send_persists_park_warm_through_raw_and_logical_entry(prod_db, logical, park_warm):
    with prod_db.begin() as db:
        _terminal(db, "abcdef01")
        if logical:
            db.add(
                MailboxModel(
                    id="mb_abcdef01",
                    session_name="wpwd",
                    role="supervisor",
                    current_terminal_id="abcdef01",
                    generation=1,
                    consumed_through_id=0,
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
            )

    client = TestClient(app)
    with (
        patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={
                "id": "abcdef01",
                "tmux_session": "wpwd",
                "tmux_window": "abcdef01",
            },
        ),
        patch("cli_agent_orchestrator.api.main.require_input_allowed"),
        patch("cli_agent_orchestrator.api.main.get_backend") as backend,
        patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
        patch("cli_agent_orchestrator.services.terminal_guard_service.require_input_allowed"),
    ):
        backend.return_value.session_exists.return_value = True
        backend.return_value.get_history.return_value = ""
        response = client.post(
            f"/terminals/{'mb_abcdef01' if logical else 'abcdef01'}/inbox/messages",
            params={"sender_id": "sender", "message": "wire", "park_warm": park_warm},
            headers={"Host": "localhost"},
        )
    assert response.status_code == 200, response.text
    with prod_db() as db:
        row = db.query(InboxModel).order_by(InboxModel.id.desc()).first()
        assert row is not None and bool(row.park_warm) is park_warm


@pytest.mark.asyncio
async def test_deferred_assign_runs_real_send_input_commit_and_keeps_watchdog_parked(prod_db):
    with prod_db.begin() as db:
        _terminal(db, "worker", caller_id="supervisor")
        _terminal(db, "supervisor")

    provider = GrokCliProvider(
        terminal_id="worker",
        session_name="wpwd",
        window_name="worker",
        agent_profile=None,
        allowed_tools=["*"],
    )
    provider.shell_baseline = "shell"
    provider.initialize = AsyncMock(return_value=True)
    backend = MagicMock()
    metadata = {
        "id": "worker",
        "caller_id": "supervisor",
        "tmux_session": "wpwd",
        "tmux_window": "worker",
        "provider": "grok_cli",
        "agent_profile": "developer",
        "init_deadline_s": 60.0,
        "lifecycle_generation": 1,
    }
    watchdog = StalledCallbackWatchdog(grace_seconds=3)
    before_generation = status_monitor.get_input_gen("worker")
    with (
        patch.object(terminal_service, "get_terminal_metadata", return_value=metadata),
        patch.object(terminal_service.provider_manager, "get_provider", return_value=provider),
        patch.object(terminal_service, "get_backend", return_value=backend),
        patch.object(terminal_service, "preserve_draft_before_send", return_value=None),
        patch.object(terminal_service, "inject_memory_context", side_effect=lambda m, *_a, **_k: m),
        patch.object(terminal_service, "_prepare_fork_message", AsyncMock(return_value="task")),
        patch.object(terminal_service, "_prepare_provider_runtime_identity", return_value=None),
        patch.object(
            terminal_service, "_confirm_worker_started_or_resubmit", AsyncMock(return_value=True)
        ),
        patch.object(
            terminal_service, "_mark_ready_if_generation_current", AsyncMock(return_value=None)
        ),
        patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog",
            watchdog,
        ),
    ):
        terminal_service._schedule_deferred_init(
            provider,
            "worker",
            "task",
            OrchestrationType.ASSIGN,
            None,
            caller_snapshot=metadata,
            park_warm=True,
        )
        task = next(iter(terminal_service._deferred_init_tasks))
        await asyncio.wait_for(task, timeout=5)
    backend.send_keys.assert_called_once()
    assert status_monitor.get_input_gen("worker") == before_generation + 1
    assert not watchdog.has_episode("worker")


@pytest.mark.parametrize("park_warm", [False, True])
def test_recovery_reads_persisted_member_park_warm(prod_db, park_warm):
    with prod_db.begin() as db:
        _terminal(db, "receiver")
        row = InboxModel(
            sender_id="sender",
            receiver_id="receiver",
            message="stale",
            orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            status=MessageStatus.DELIVERING.value,
            park_warm=park_warm,
            created_at=datetime.now(),
        )
        db.add(row)
        db.flush()
        message_id = row.id

    module = __import__("cli_agent_orchestrator.services.inbox_service", fromlist=["x"])
    with prod_db() as db:
        message = database._inbox_message_from_row(db.get(InboxModel, message_id))
    with (
        patch.object(module, "list_stale_delivering_messages", return_value=[message]),
        patch.object(
            module,
            "get_message_trace",
            return_value={
                "attempts": [
                    {
                        "attempt_uuid": "a",
                        "payload_hash": "h",
                        "started_at": None,
                        "evidence": {},
                        "sender_id": "sender",
                        "orchestration_type": "send_message",
                    }
                ]
            },
        ),
        patch.object(module, "list_attempt_member_ids", return_value=[message_id]),
        patch.object(
            module, "get_terminal_metadata", return_value={"tmux_session": "s", "tmux_window": "w"}
        ),
        patch.object(module, "resolve_session_transcript", return_value=Path("/trace")),
        patch.object(module, "transcript_lookup", return_value=("hit", {})),
        patch("cli_agent_orchestrator.backends.registry.get_backend") as backend,
        patch.object(
            module, "settle_delivery_attempt", side_effect=lambda *a, **kw: kw["on_confirmed"]()
        ),
        patch.object(InboxService, "_commit_watchdog_ops") as commit,
    ):
        backend.return_value.get_history.return_value = ""
        InboxService().recover_stale_deliveries()
    assert commit.call_args.args[-1] is park_warm


# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: ``test_notify_replaying_current_stall_persists_one_durable_chain_row``
# is GONE with its subject
# ---------------------------------------------------------------------------
# It was the production-entry probe for the CHAIN notice: a real sqlite row set
# (caller -> worker -> target), two ``notify_due()`` calls over the same replayed
# stall, and one assertion — exactly one durable "chain stalled" inbox row on the
# second pass. The property was the reservation's idempotence at the database,
# not in a mock: ``_reserve_chain_notice`` had to hold across the replay so the
# second sweep could not write a second row.
#
# K4 deletes ``notify_due``, ``collect_due_notifications``, ``_push_notice`` and
# the reservation pair (``_reserve_chain_notice`` / ``_release_chain_reservation``)
# together. Nothing emits a chain notice any more, so there is no second row to
# be prevented and no reservation to be idempotent. The probe cannot be repointed
# at a surviving emitter, because the surviving emitter — ``emit_pre_delete_notice``
# — is a single call at terminal deletion with no sweep and no replay, so
# idempotence-across-sweeps is not a property it can have.
#
# The delegation laws this file exists to probe at the PRODUCTION entry are the
# three above, and all three survive untouched: park_warm persisted through both
# the raw and logical HTTP entries, the deferred assign committing real input and
# leaving the watchdog parked (``not watchdog.has_episode``), and recovery reading
# the persisted member's park_warm. Those still run against a real engine and a
# real ``TestClient``.
# ---------------------------------------------------------------------------
