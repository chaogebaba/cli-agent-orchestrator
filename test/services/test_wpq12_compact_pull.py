"""WPQ12 pull-only compact-boundary acceptance tests."""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxModel,
    TranscriptBindingModel,
    get_latest_compact_transcript_binding,
)
from cli_agent_orchestrator.mcp_server import server as mcp_server
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import inbox_service as inbox_service_module
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.mailbox_service import list_messages


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wpq12.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


@pytest.fixture
def client():
    app.state.plugin_registry = PluginRegistry()
    return TestClient(app, headers={"Host": "localhost"})


def _binding_payload(transcript, source: str = "compact") -> dict[str, str]:
    return {
        "terminal_id": "abcd1234",
        "session_id": "session",
        "transcript_path": str(transcript),
        "cwd": "/work",
        "source": source,
    }


def _seed_publish_preconditions(sessions, terminal_id: str = "abcd1234") -> int:
    """Recreate the old publisher's positive selection preconditions."""
    now = datetime.now()
    with sessions.begin() as db:
        db.add(
            MailboxModel(
                id="mb_probe",
                session_name="probe",
                role="supervisor",
                current_terminal_id=terminal_id,
                generation=1,
                consumed_through_id=0,
                created_at=now,
                updated_at=now,
            )
        )
        delivered = InboxModel(
            sender_id="worker",
            receiver_id=terminal_id,
            logical_receiver_id="mb_probe",
            message="recent delivered callback",
            orchestration_type="send_message",
            status="delivered",
            created_at=now - timedelta(minutes=2),
        )
        db.add(delivered)
        db.flush()
        db.add(
            InboxMessageTraceEventModel(
                message_id=delivered.id,
                kind="inferred_delivered",
                payload={"reply_message_id": 7},
                created_at=now - timedelta(minutes=1),
            )
        )
        return int(delivered.id)


def _transcript(tmp_path):
    transcript = tmp_path / ".claude" / "projects" / "repo" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    return transcript


def test_compact_bind_twice_persists_markers_without_notice_or_push(scratch_db, client, tmp_path):
    delivered_id = _seed_publish_preconditions(scratch_db)
    transcript = _transcript(tmp_path)

    with (
        patch("cli_agent_orchestrator.api.main.Path.home", return_value=tmp_path),
        patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={"id": "abcd1234"},
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.inbox_service." "reset_binding_episodes"
        ) as reset,
        patch.object(inbox_service_module.inbox_service, "deliver_pending") as deliver_pending,
        patch.object(inbox_service_module.inbox_service, "schedule_delivery_wake") as schedule_wake,
    ):
        first = client.post(
            "/terminals/abcd1234/transcript-binding",
            json=_binding_payload(transcript),
        )
        second = client.post(
            "/terminals/abcd1234/transcript-binding",
            json=_binding_payload(transcript),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert reset.call_count == 2
    deliver_pending.assert_not_called()
    schedule_wake.assert_not_called()
    with scratch_db() as db:
        assert db.query(TranscriptBindingModel).filter_by(source="compact").count() == 2
        rows = db.query(InboxModel).order_by(InboxModel.id).all()
        assert [row.id for row in rows] == [delivered_id]


def test_compact_bind_runtime_traps_retired_environment_reads(scratch_db, client, tmp_path):
    delivered_id = _seed_publish_preconditions(scratch_db)
    transcript = _transcript(tmp_path)
    retired = {
        "CAO_COMPACT_DIGEST_WINDOW_MIN",
        "CAO_COMPACT_DIGEST_FENCE_MIN",
    }
    original_get = os.environ.get

    def reject_retired_reads(key, default=None):
        if key in retired:
            raise AssertionError(f"retired environment variable read: {key}")
        return original_get(key, default)

    with (
        patch("cli_agent_orchestrator.api.main.Path.home", return_value=tmp_path),
        patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={"id": "abcd1234"},
        ),
        patch.object(inbox_service_module.inbox_service, "reset_binding_episodes"),
        patch.object(inbox_service_module.inbox_service, "deliver_pending") as deliver_pending,
        patch.object(inbox_service_module.inbox_service, "schedule_delivery_wake") as schedule_wake,
        patch.object(os.environ, "get", side_effect=reject_retired_reads),
    ):
        response = client.post(
            "/terminals/abcd1234/transcript-binding",
            json=_binding_payload(transcript),
        )

    assert response.status_code == 200
    deliver_pending.assert_not_called()
    schedule_wake.assert_not_called()
    with scratch_db() as db:
        rows = db.query(InboxModel).order_by(InboxModel.id).all()
        assert [row.id for row in rows] == [delivered_id]


def test_latest_compact_helper_returns_full_row_and_ignores_later_startup(scratch_db):
    instant = datetime(2026, 7, 18, 12, 0, 0)
    with scratch_db.begin() as db:
        first = TranscriptBindingModel(
            terminal_id="abcd1234",
            session_id="compact-one",
            transcript_path="/tmp/one.jsonl",
            inode=1,
            source="compact",
            received_at=instant,
        )
        winner = TranscriptBindingModel(
            terminal_id="abcd1234",
            session_id="compact-two",
            transcript_path="/tmp/two.jsonl",
            inode=2,
            source="compact",
            received_at=instant,
        )
        startup = TranscriptBindingModel(
            terminal_id="abcd1234",
            session_id="startup",
            transcript_path="/tmp/startup.jsonl",
            inode=3,
            source="startup",
            received_at=instant + timedelta(hours=1),
        )
        db.add_all([first, winner, startup])
        db.flush()
        winner_id = winner.id

    row = get_latest_compact_transcript_binding("abcd1234")
    assert row == {
        "id": winner_id,
        "terminal_id": "abcd1234",
        "session_id": "compact-two",
        "transcript_path": "/tmp/two.jsonl",
        "inode": 2,
        "source": "compact",
        "received_at": instant,
    }


def test_latest_compact_helper_missing_table_and_empty_terminal_are_no_row(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite'}")
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    try:
        assert get_latest_compact_transcript_binding("abcd1234") is None
        assert get_latest_compact_transcript_binding("") is None
    finally:
        engine.dispose()


def test_creation_time_pull_includes_recent_row_and_documents_clock_skew_miss(scratch_db):
    marker = datetime(2026, 7, 18, 12, 0, 0)
    cutoff = marker - timedelta(hours=1)
    with scratch_db.begin() as db:
        recent = InboxModel(
            sender_id="sender-a",
            receiver_id="abcd1234",
            message="recent",
            orchestration_type="send_message",
            status="delivered",
            created_at=cutoff + timedelta(minutes=1),
        )
        skewed = InboxModel(
            sender_id="sender-b",
            receiver_id="abcd1234",
            message="old-created-but-recently-confirmed",
            orchestration_type="send_message",
            status="delivered",
            created_at=cutoff - timedelta(minutes=1),
        )
        db.add_all([recent, skewed])
        db.flush()
        db.add(
            InboxMessageTraceEventModel(
                message_id=skewed.id,
                kind="inferred_delivered",
                payload={"reply_message_id": 99},
                created_at=marker - timedelta(minutes=1),
            )
        )

    page = list_messages("abcd1234", since=cutoff)
    assert [item["message"] for item in page["items"]] == ["recent"]


def test_legacy_compact_digest_row_keeps_ordinary_pending_list_behavior(scratch_db):
    with scratch_db.begin() as db:
        legacy = InboxModel(
            sender_id="compact-digest",
            receiver_id="abcd1234",
            message="legacy notice",
            orchestration_type="mailbox_digest",
            status="pending",
            created_at=datetime(2026, 7, 17, 12, 0, 0),
        )
        db.add(legacy)
        db.flush()
        legacy_id = legacy.id

    page = list_messages("abcd1234")
    assert page["items"] == [
        {
            "id": legacy_id,
            "sender_id": "compact-digest",
            "receiver_id": "abcd1234",
            "logical_receiver_id": None,
            "message": "legacy notice",
            "orchestration_type": "mailbox_digest",
            "status": "pending",
            "failure_reason": None,
            "digested_into": None,
            "enqueue_generation": None,
            "barrier_id": None,
            "barrier_member_key": None,
            "last_attempt_outcome": "none",
            "created_at": "2026-07-17T12:00:00",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {
            "terminal_id": "abcd1234",
            "source": "compact",
            "received_at": "2026-07-18T12:34:56.000000",
            "transcript_path": "/tmp/compact.jsonl",
        },
        {
            "detail": {
                "code": "no_compact_binding",
                "message": "no compact binding",
            }
        },
    ],
)
async def test_mcp_get_compact_marker_returns_http_json_unchanged(body):
    response = Mock()
    response.json.return_value = body
    with patch.object(mcp_server.cao_http, "get", return_value=response) as get:
        result = await mcp_server.get_compact_marker("abcd1234")

    assert result is body
    get.assert_called_once_with(
        "/terminals/abcd1234/transcript-binding/compact-latest",
        headers=mcp_server._api_headers(),
        timeout=mcp_server._mcp_timeout(),
    )
