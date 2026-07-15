"""Frozen-r8 acceptance probes for Wave 3B supervisor mailbox continuity.

The numbered tests correspond one-for-one to blueprint acceptance probes 1-15.
"""

from __future__ import annotations

import inspect
import threading
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base, InboxDeliveryAttemptMemberModel, InboxDeliveryAttemptModel, InboxModel,
    MailboxIncarnationModel, MailboxModel, TerminalModel,
    adopt_mailbox_rows_at_startup, create_inbox_message, resolve_inbox_receiver,
    settle_pending_orphan_messages,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.mailbox_service import (
    MailboxDomainError, ack_messages, claim_mailbox,
    delete_mailbox, get_mailbox_authority_lock, list_messages,
    publish_supervisor_incarnation, PublicationCleanupFailed,
)
from cli_agent_orchestrator.plugins import PluginRegistry


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wave3b.sqlite'}", connect_args={"check_same_thread": False}
    )
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


def terminal(db, terminal_id: str, session: str = "cao-wave3b") -> None:
    db.add(TerminalModel(
        id=terminal_id, tmux_session=session, tmux_window=terminal_id,
        provider="codex", agent_profile="code_supervisor", init_state="ready",
    ))


def mailbox(db, terminal_id: str = "11111111", *, generation: int = 1) -> MailboxModel:
    row = MailboxModel(
        id="mb_aaaaaaaa", session_name="cao-wave3b", role="supervisor",
        current_terminal_id=terminal_id, generation=generation,
        consumed_through_id=0, created_at=datetime.now(), updated_at=datetime.now(),
    )
    db.add(row)
    db.add(MailboxIncarnationModel(
        mailbox_id=row.id, generation=generation, terminal_id=terminal_id,
        published_at=datetime.now(),
    ))
    return row


def inbox(db, receiver: str, status: str = "pending", *, logical: str | None = None,
          sender: str = "99999999", kind: str = "send_message") -> InboxModel:
    row = InboxModel(
        sender_id=sender, receiver_id=receiver, logical_receiver_id=logical,
        message=f"message-{receiver}", orchestration_type=kind, status=status,
        created_at=datetime.now(),
    )
    db.add(row)
    db.flush()
    return row


def test_probe_01_restart_adoption_p5_and_relaunch(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db)
        first = inbox(db, "11111111")
        second = inbox(db, "11111111", logical="mb_aaaaaaaa")
    assert adopt_mailbox_rows_at_startup() == 1
    assert settle_pending_orphan_messages().settled_count == 0
    result = publish_supervisor_incarnation(
        claim_mailbox("cao-wave3b"), "22222222"
    )
    with scratch_db() as db:
        rows = db.query(InboxModel).order_by(InboxModel.id).all()
        assert [row.id for row in rows] == [first.id, second.id]
        assert all(row.receiver_id == "22222222" for row in rows)
        assert all(row.logical_receiver_id == "mb_aaaaaaaa" for row in rows)
    assert result["generation"] == 2


def test_probe_02_publication_claim_races_and_retry(scratch_db):
    absent_a = claim_mailbox("cao-race")
    absent_b = claim_mailbox("cao-race")
    winner = publish_supervisor_incarnation(absent_a, "aaaaaaaa")
    retry = publish_supervisor_incarnation(absent_a, "aaaaaaaa")
    assert retry["generation"] == winner["generation"]
    assert retry["digest_message_id"] == winner["digest_message_id"]
    with pytest.raises(MailboxDomainError, match="mailbox_conflict"):
        publish_supervisor_incarnation(absent_b, "bbbbbbbb")


def test_probe_03_paste_fence_authority_serializes_and_detects_successor(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db)
    lock = get_mailbox_authority_lock("cao-wave3b", "supervisor")
    same = get_mailbox_authority_lock("cao-wave3b", "supervisor")
    assert lock is same
    held = mailbox_service.acquire_logical_sender_authority(
        "mb_aaaaaaaa", "11111111", 1
    )
    assert held is lock
    held.release()
    publish_supervisor_incarnation(claim_mailbox("cao-wave3b"), "22222222")
    assert mailbox_service.acquire_logical_sender_authority(
        "mb_aaaaaaaa", "11111111", 1
    ) is None


def test_probe_04_cross_generation_replay_and_digest_once(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db)
        delivered = inbox(db, "11111111", "delivered", logical="mb_aaaaaaaa")
    claim = claim_mailbox("cao-wave3b")
    first = publish_supervisor_incarnation(claim, "22222222")
    retry = publish_supervisor_incarnation(claim, "22222222")
    page = list_messages("mb_aaaaaaaa")
    assert delivered.id in {item["id"] for item in page["items"]}
    assert first["digest_message_id"] == retry["digest_message_id"]
    digest = next(item for item in page["items"] if item["id"] == first["digest_message_id"])
    assert digest["orchestration_type"] == "mailbox_digest"


def test_probe_05_ack_fences_range_predecessor_and_monotonicity(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db)
        visible = inbox(db, "11111111", "delivered", logical="mb_aaaaaaaa")
    with pytest.raises(MailboxDomainError, match="ack_out_of_range"):
        ack_messages("11111111", visible.id + 1)
    first = ack_messages("11111111", visible.id)
    second = ack_messages("11111111", visible.id)
    assert first["changed"] is True and second["changed"] is False
    publish_supervisor_incarnation(claim_mailbox("cao-wave3b"), "22222222")
    with pytest.raises(MailboxDomainError, match="not_current_incarnation"):
        ack_messages("11111111", visible.id)


def test_probe_06_list_pagination_since_and_unresolved_projection(scratch_db):
    with scratch_db.begin() as db:
        terminal(db, "11111111")
        one = inbox(db, "11111111")
        two = inbox(db, "11111111")
        attempt = InboxDeliveryAttemptModel(
            attempt_uuid="attempt-unresolved", receiver_terminal_id="11111111",
            provider="codex", outcome="unresolved", reason="continuity_uncertain",
            payload_hash="x", payload_length=1, evidence="{}", sender_id="99999999",
            orchestration_type="send_message", started_at=datetime.now(),
            last_at=datetime.now(), settled_at=datetime.now(),
        )
        db.add(attempt)
        db.add(InboxDeliveryAttemptMemberModel(
            attempt_uuid=attempt.attempt_uuid, message_id=two.id, position=0
        ))
    page1 = list_messages("11111111", limit=1)
    assert page1 == {"items": page1["items"], "next_after_id": one.id, "has_more": True}
    page2 = list_messages("11111111", after_id=one.id, since=datetime.now()-timedelta(days=1))
    assert page2["items"][0]["id"] == two.id
    assert page2["items"][0]["last_attempt_outcome"] == "unresolved"


def test_probe_07_scoped_surfaces_and_mcp_http_twins_are_declared():
    source = inspect.getsource(__import__(
        "cli_agent_orchestrator.api.main", fromlist=["list_messages_endpoint"]
    ))
    assert source.count("require_any_scope(SCOPE_READ, SCOPE_ADMIN)") >= 3
    assert "require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)" in source
    assert "require_any_scope(SCOPE_ADMIN)" in source
    mcp_source = inspect.getsource(__import__(
        "cli_agent_orchestrator.mcp_server.server", fromlist=["list_messages"]
    ))
    assert 'f"{API_BASE_URL}/messages"' in mcp_source
    assert 'f"{API_BASE_URL}/messages/ack"' in mcp_source
    assert "headers=_api_headers()" in mcp_source


def test_probe_07_scope_enforcement_is_401_and_403(client, monkeypatch):
    from cli_agent_orchestrator.security import auth
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    requests = [
        ("get", "/messages?to=11111111"),
        ("get", "/terminals/11111111/inbox/messages"),
        ("post", "/messages/ack"),
        ("get", "/mailboxes"),
        ("delete", "/mailboxes/mb_aaaaaaaa"),
    ]
    for method, path in requests:
        response = (
            client.post(
                path,
                json={"terminal_id": "11111111", "up_to_id": 1},
            )
            if method == "post"
            else getattr(client, method)(path)
        )
        assert response.status_code == 401

    async def wrong_scope():
        return [auth.SCOPE_WRITE]
    app.dependency_overrides[auth.get_current_scopes] = wrong_scope
    try:
        assert client.get("/messages?to=11111111").status_code == 403
        assert client.get("/terminals/11111111/inbox/messages").status_code == 403
        assert client.get("/mailboxes").status_code == 403
        # write is insufficient for operator deletion.
        assert client.delete("/mailboxes/mb_aaaaaaaa").status_code == 403
    finally:
        app.dependency_overrides.pop(auth.get_current_scopes, None)


def test_probe_08_all_direct_writers_use_choke_point():
    source = inspect.getsource(database)
    seams = [
        ("def claim_deferred_init_failure", "def list_deferred_init_recovery_rows"),
        ("def _record_p5_orphan_notices", "def settle_pending_orphan_messages"),
        ("def record_wpm1_stalled_notice", "def settle_wpm1_terminal_batch"),
        ("def settle_wpm1_terminal_batch", "def get_callback_status_since"),
    ]
    for start, end in seams:
        body = source[source.index(start):source.index(end)]
        assert "resolve_inbox_receiver" in body


def test_probe_09_raw_addressed_create_is_byte_identical(scratch_db):
    with scratch_db.begin() as db:
        terminal(db, "11111111")
    row = create_inbox_message("99999999", "11111111", "raw body")
    assert row.receiver_id == "11111111"
    assert row.logical_receiver_id is None
    assert row.message == "raw body" and row.status == MessageStatus.PENDING


def test_probe_10_mailbox_delete_settles_refuses_and_p5_straggler(scratch_db):
    with scratch_db.begin() as db:
        terminal(db, "99999999")
        mailbox(db)
        pending = inbox(db, "11111111", logical="mb_aaaaaaaa")
    result = delete_mailbox("mb_aaaaaaaa")
    assert result == {"settled_pending": 1, "notices_sent": 1}
    with scratch_db() as db:
        settled = db.query(InboxModel).filter_by(id=pending.id).one()
        assert (settled.status, settled.failure_reason) == (
            "delivery_failed", "mailbox_deleted"
        )
    with pytest.raises(MailboxDomainError, match="unknown_mailbox"):
        delete_mailbox("mb_aaaaaaaa")


def test_probe_11_incarnation_mapper_pk_and_global_uniqueness(scratch_db):
    assert [column.name for column in MailboxIncarnationModel.__table__.primary_key] == [
        "mailbox_id", "generation"
    ]
    with scratch_db.begin() as db:
        mailbox(db)
        db.add(MailboxModel(
            id="mb_bbbbbbbb", session_name="cao-other", role="supervisor",
            current_terminal_id="11111111", generation=1, consumed_through_id=0,
            created_at=datetime.now(), updated_at=datetime.now(),
        ))
        db.add(MailboxIncarnationModel(
            mailbox_id="mb_bbbbbbbb", generation=1, terminal_id="11111111",
            published_at=datetime.now(),
        ))
        with pytest.raises(IntegrityError):
            db.flush()


def test_probe_12_delete_busy_route_and_misscoped_retry(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db)
        busy = inbox(db, "11111111", "delivering", logical="mb_aaaaaaaa")
    with pytest.raises(MailboxDomainError, match="mailbox_busy"):
        delete_mailbox("mb_aaaaaaaa")
    with scratch_db.begin() as db:
        db.query(InboxModel).filter_by(id=busy.id).update({"status": "pending"})
    mis_scoped = mailbox_service.MailboxClaim(
        "cao-other", "supervisor", "mb_aaaaaaaa", 1
    )
    with pytest.raises(MailboxDomainError, match="mailbox_conflict"):
        publish_supervisor_incarnation(mis_scoped, "22222222")


@pytest.mark.parametrize("error,expected_status,expected_code", [
    (MailboxDomainError("mailbox_conflict", "conflict"), 409, "mailbox_conflict"),
    (MailboxDomainError("mailbox_authority_timeout", "timeout"), 409,
     "mailbox_authority_timeout"),
    (PublicationCleanupFailed(MailboxDomainError("mailbox_conflict", "cause")), 500,
     "publication_cleanup_failed"),
])
def test_probe_13_http_projections_guard_delivery_and_cold_registry(
    client, monkeypatch, error, expected_status, expected_code,
):
    assert get_mailbox_authority_lock("cold", "supervisor") is get_mailbox_authority_lock(
        "cold", "supervisor"
    )
    with patch(
        "cli_agent_orchestrator.api.main.session_service.start_session",
        side_effect=error,
    ):
        response = client.post("/sessions/start", params={"agent_profile": "code_supervisor"})
    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code
    if expected_code == "publication_cleanup_failed":
        assert response.json()["detail"]["cause"]["code"] == "mailbox_conflict"


@pytest.mark.parametrize("code,status,cause", [
    ("mailbox_conflict", 409, None),
    ("mailbox_authority_timeout", 409, None),
    ("publication_cleanup_failed", 500,
     {"code": "mailbox_conflict", "message": "conflict"}),
])
def test_probe_14_both_session_start_cli_clients_decode_typed_errors(
    monkeypatch, code, status, cause,
):
    response = Mock(status_code=status)
    detail = {"code": code, "message": "typed failure"}
    if cause:
        detail["cause"] = cause
    response.json.return_value = {"detail": detail}
    response.raise_for_status.side_effect = RuntimeError("must not flatten")
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: response)
    runner = CliRunner()
    canonical = runner.invoke(cli, ["session", "start", "demo", "--agents", "code_supervisor"])
    deprecated = runner.invoke(cli, [
        "launch", "--agents", "code_supervisor", "--session-name", "demo",
        "--headless", "--auto-approve",
    ])
    for result in (canonical, deprecated):
        assert result.exit_code == 1
        assert code in result.output
        if cause:
            assert "cause=mailbox_conflict" in result.output


def test_probe_14_seed_failure_exit_two_is_retained_for_both_clients(monkeypatch):
    response = Mock(status_code=422)
    response.json.return_value = {
        "bootstrap": {"status": "seed_failed", "error_code": "seed_timeout"}
    }
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: response)
    runner = CliRunner()
    results = [
        runner.invoke(cli, ["session", "start", "demo", "--agents", "code_supervisor"]),
        runner.invoke(cli, ["launch", "--agents", "code_supervisor", "--session-name",
                            "demo", "--headless", "--auto-approve"]),
    ]
    assert all(result.exit_code == 2 for result in results)
    assert all("seed_timeout" in result.output for result in results)


def test_probe_15_send_timeout_is_409_no_insert_and_mcp_structured(
    scratch_db, monkeypatch, client,
):
    with scratch_db.begin() as db:
        mailbox(db)
    lock = get_mailbox_authority_lock("cao-wave3b", "supervisor")
    lock.acquire()
    monkeypatch.setattr(mailbox_service, "MAILBOX_AUTHORITY_TIMEOUT_SECONDS", 0.01)
    try:
        response = client.post(
            "/terminals/mb_aaaaaaaa/inbox/messages",
            params={"sender_id": "99999999", "message": "blocked"},
        )
    finally:
        lock.release()
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "mailbox_authority_timeout"
    with scratch_db() as db:
        assert db.query(InboxModel).count() == 0
    from cli_agent_orchestrator.mcp_server.server import _extract_structured_detail
    response = Mock()
    response.json.return_value = {"detail": {
        "code": "mailbox_authority_timeout", "message": "timed out"
    }}
    assert _extract_structured_detail(response, "fallback") == {
        "code": "mailbox_authority_timeout", "message": "timed out"
    }
