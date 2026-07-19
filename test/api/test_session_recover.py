from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.wpd1_decontam import RECOVERY_NUDGE_MESSAGE


@pytest.fixture
def wpd1_nudge_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpd1-nudge.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    try:
        yield
    finally:
        engine.dispose()


def _run_ordinary_nudge(
    client,
    monkeypatch,
    *,
    status: TerminalStatus,
    composer_state: str = "empty",
    send_error: Exception | None = None,
):
    suffix = uuid4().hex[:8]
    sender_id = f"wpd1-sender-{suffix}"
    terminal_id = f"wpd1-worker-{suffix}"
    database.create_terminal(sender_id, "cao-test", sender_id, "codex")
    database.create_terminal(
        terminal_id,
        "cao-test",
        terminal_id,
        "claude_code",
        caller_id=sender_id,
    )
    recovered = {"results": [{"terminal_id": terminal_id, "status": "rebound"}]}
    metadata = database.get_terminal_metadata(terminal_id)
    assert metadata is not None
    api_metadata = dict(metadata)
    api_metadata["caller_mailbox_id"] = "mb_wpd1_owner"

    provider = MagicMock()
    provider.composer_stash_keys = []
    provider.read_composer_draft_state.return_value = composer_state
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0.0

    backend = MagicMock()
    backend.supports_identity_readback = False
    backend.session_exists.return_value = True
    backend.get_history.return_value = ""
    backend.read_native_identity.return_value = SimpleNamespace(verdict="match")

    def send_keys(*_args, **_kwargs):
        rows = database.get_inbox_messages(terminal_id)
        assert len(rows) == 1
        assert rows[0].message == RECOVERY_NUDGE_MESSAGE
        if send_error is not None:
            raise send_error

    backend.send_keys.side_effect = send_keys
    monitor = MagicMock()
    monitor.get_status.return_value = status
    monitor.get_input_gen.return_value = 1
    monitor.get_status_gen.return_value = 1
    monitor.mark_injection_completed.return_value = None
    attempt = {
        "attempt_uuid": "wpd1-attempt",
        "started_at": "2026-07-18T00:00:00+00:00",
        "evidence": {},
    }

    def settle_attempt(_attempt_uuid, message_status, _outcome, **_kwargs):
        rows = database.get_inbox_messages(terminal_id)
        assert len(rows) == 1
        database.update_message_status(rows[0].id, message_status)
        return True

    with (
        patch(
            "cli_agent_orchestrator.services.provider_rebind_service.recover_provider_reauth",
            new=AsyncMock(return_value=recovered),
        ),
        patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=api_metadata),
        patch(
            "cli_agent_orchestrator.clients.database.get_current_mailbox_terminal",
            return_value=sender_id,
        ),
        patch("cli_agent_orchestrator.api.main.require_input_allowed"),
        patch("cli_agent_orchestrator.api.main.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch("cli_agent_orchestrator.services.terminal_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=provider,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.provider_manager.get_provider",
            return_value=provider,
        ),
        patch("cli_agent_orchestrator.services.terminal_service.get_backend", return_value=backend),
        patch.object(InboxService, "_handle_wpm1_gate", return_value=("normal", {})),
        patch(
            "cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt",
            return_value="wpd1-attempt",
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_message_trace",
            return_value={"attempts": [attempt]},
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "accepted"}),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.settle_delivery_attempt",
            side_effect=settle_attempt,
        ),
    ):
        response = client.post(
            "/sessions/cao-test/recover",
            json={"reason": "provider-reauth", "nudge": True},
        )
    rows = database.get_inbox_messages(terminal_id)
    assert len(rows) == 1
    return response, rows[0], backend, provider


def test_recover_requires_exact_reason(client):
    response = client.post("/sessions/cao-test/recover", json={"reason": "quota-banner"})
    assert response.status_code == 422


def test_recover_rejects_unsupported_provider(client):
    response = client.post(
        "/sessions/cao-test/recover",
        json={"reason": "provider-reauth", "provider": "claude_code"},
    )
    assert response.status_code == 422


def test_recover_forwards_selectors_and_interrupt(client):
    expected = {"schema_version": "cao.session-recover/v1", "results": []}
    with patch(
        "cli_agent_orchestrator.services.provider_rebind_service.recover_provider_reauth",
        new=AsyncMock(return_value=expected),
    ) as recover:
        response = client.post(
            "/sessions/cao-test/recover",
            json={
                "reason": "provider-reauth",
                "provider": "grok_cli",
                "terminal_ids": ["a", "b"],
                "interrupt": True,
            },
        )
    assert response.status_code == 200
    recover.assert_awaited_once_with(
        "cao-test", provider="grok_cli", terminal_ids=["a", "b"], interrupt=True,
        acknowledge_ownership=False,
    )


def test_recover_forwards_single_terminal_ownership_ack(client):
    with patch(
        "cli_agent_orchestrator.services.provider_rebind_service.recover_provider_reauth",
        new=AsyncMock(return_value={"results": []}),
    ) as recover:
        response = client.post("/sessions/cao-test/recover", json={
            "reason": "provider-reauth", "terminal_ids": ["a"],
            "acknowledge_ownership": True,
        })
    assert response.status_code == 200
    recover.assert_awaited_once_with(
        "cao-test", provider="codex", terminal_ids=["a"], interrupt=False,
        acknowledge_ownership=True,
    )


def test_recover_rejects_fleet_ownership_ack(client):
    response = client.post("/sessions/cao-test/recover", json={
        "reason": "provider-reauth", "acknowledge_ownership": True,
    })
    assert response.status_code == 400


def test_epoch_route_rejects_reauth_only_fields(client):
    for field, value in (
        ("terminal_ids", ["a"]), ("interrupt", True),
        ("acknowledge_ownership", True),
    ):
        response = client.post(
            "/sessions/cao-test/recover", json={"reason": "epoch", field: value},
        )
        assert response.status_code == 400, field
        assert "epoch recovery rejects" in response.json()["detail"]


def test_provider_reauth_route_rejects_epoch_base_names(client):
    response = client.post("/sessions/cao-test/recover", json={
        "reason": "provider-reauth", "base_names": ["codex"],
    })
    assert response.status_code == 400
    assert response.json()["detail"] == "provider-reauth rejects base_names"


def test_content_recovery_routes_scrub_options_and_defaults_nudge_on(client):
    recovered = {
        "results": [
            {
                "terminal_id": "ok",
                "status": "rebound",
                "decontamination": {"incident_path": "/tmp/incident.json"},
            },
            {"terminal_id": "bad", "status": "resume_failed"},
        ]
    }
    with (
        patch(
            "cli_agent_orchestrator.services.provider_rebind_service.recover_provider_reauth",
            new=AsyncMock(return_value=recovered),
        ) as recover,
        patch(
            "cli_agent_orchestrator.api.main._send_recovery_nudge",
            new=AsyncMock(return_value={"status": "sent", "nudge_message_id": 17}),
        ) as send,
        patch("cli_agent_orchestrator.services.wpd1_decontam.update_incident_nudge") as audit,
    ):
        response = client.post(
            "/sessions/cao-test/recover",
            json={"reason": "content-flag", "terminal_ids": ["ok", "bad"], "show": True},
        )
    assert response.status_code == 200
    recover.assert_awaited_once_with(
        "cao-test",
        provider="codex",
        terminal_ids=["ok", "bad"],
        interrupt=False,
        acknowledge_ownership=False,
        reason="content-flag",
        content_options={"show": True, "force": False},
    )
    rows = response.json()["results"]
    assert rows[0]["nudge"] == {"status": "sent", "nudge_message_id": 17}
    assert rows[1]["nudge"] == {"status": "not_attempted"}
    send.assert_awaited_once()
    audit.assert_called_once()


def test_content_recovery_explicit_no_nudge_is_structured_skip(client):
    recovered = {
        "results": [{
            "terminal_id": "ok", "status": "rebound",
            "decontamination": {"incident_path": "/tmp/incident.json"},
        }]
    }
    with (
        patch(
            "cli_agent_orchestrator.services.provider_rebind_service.recover_provider_reauth",
            new=AsyncMock(return_value=recovered),
        ),
        patch("cli_agent_orchestrator.api.main._send_recovery_nudge", new=AsyncMock()) as send,
        patch("cli_agent_orchestrator.services.wpd1_decontam.update_incident_nudge") as audit,
    ):
        response = client.post(
            "/sessions/cao-test/recover",
            json={"reason": "content-flag", "nudge": False},
        )
    assert response.json()["results"][0]["nudge"] == {
        "status": "skipped", "skip_reason": "no-nudge-flag"
    }
    send.assert_not_awaited()
    assert audit.call_args.kwargs["status"] == "skipped"


def test_noncontent_explicit_nudge_has_no_incident_record_side_effect(client):
    recovered = {"results": [{"terminal_id": "ok", "status": "rebound"}]}
    with (
        patch(
            "cli_agent_orchestrator.services.provider_rebind_service.recover_provider_reauth",
            new=AsyncMock(return_value=recovered),
        ),
        patch(
            "cli_agent_orchestrator.api.main._send_recovery_nudge",
            new=AsyncMock(return_value={"status": "sent", "nudge_message_id": 9}),
        ),
        patch("cli_agent_orchestrator.services.wpd1_decontam.update_incident_nudge") as audit,
    ):
        response = client.post(
            "/sessions/cao-test/recover",
            json={"reason": "provider-reauth", "nudge": True},
        )
    assert response.json()["results"][0]["nudge"]["status"] == "sent"
    audit.assert_not_called()


def test_content_nudge_sender_uses_current_caller_mailbox_incarnation(client):
    recovered = {"results": [{"terminal_id": "ok", "status": "rebound"}]}
    inbox = MagicMock(id=31, receiver_id="ok")
    backend = MagicMock()
    backend.session_exists.return_value = True
    with (
        patch(
            "cli_agent_orchestrator.services.provider_rebind_service.recover_provider_reauth",
            new=AsyncMock(return_value=recovered),
        ),
        patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={
                "caller_mailbox_id": "mb_owner",
                "tmux_session": "cao-test",
                "tmux_window": "ok",
            },
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_current_mailbox_terminal",
            return_value="caller-current",
        ),
        patch("cli_agent_orchestrator.api.main.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.api.main.require_input_allowed"),
        patch("cli_agent_orchestrator.api.main.create_inbox_message", return_value=inbox) as create,
        patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
    ):
        response = client.post("/sessions/cao-test/recover", json={"reason": "content-flag"})
    assert response.json()["results"][0]["nudge"] == {
        "status": "sent", "nudge_message_id": 31
    }
    assert create.call_args.args[:2] == ("caller-current", "ok")


def test_content_nudge_unresolvable_caller_is_skipped_with_reminder_state(client):
    recovered = {"results": [{"terminal_id": "ok", "status": "rebound"}]}
    with (
        patch(
            "cli_agent_orchestrator.services.provider_rebind_service.recover_provider_reauth",
            new=AsyncMock(return_value=recovered),
        ),
        patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={"caller_mailbox_id": None},
        ),
    ):
        response = client.post("/sessions/cao-test/recover", json={"reason": "content-flag"})
    assert response.json()["results"][0]["nudge"] == {
        "status": "skipped", "skip_reason": "caller-unresolvable"
    }


def test_content_nudge_waiting_dialog_is_held_by_ordinary_delivery_engine(
    client, monkeypatch, wpd1_nudge_db
):
    response, row, backend, _provider = _run_ordinary_nudge(
        client, monkeypatch, status=TerminalStatus.WAITING_USER_ANSWER
    )
    assert response.json()["results"][0]["status"] == "rebound"
    assert response.json()["results"][0]["nudge"]["status"] == "sent"
    assert row.status is MessageStatus.PENDING
    backend.send_keys.assert_not_called()


def test_content_nudge_nonempty_composer_defers_in_ordinary_delivery_engine(
    client, monkeypatch, wpd1_nudge_db
):
    response, row, backend, provider = _run_ordinary_nudge(
        client,
        monkeypatch,
        status=TerminalStatus.IDLE,
        composer_state="nonempty",
    )
    assert response.json()["results"][0]["status"] == "rebound"
    assert response.json()["results"][0]["nudge"]["status"] == "sent"
    assert row.status is MessageStatus.PENDING
    provider.read_composer_draft_state.assert_called()
    backend.send_keys.assert_not_called()


def test_content_nudge_send_failure_does_not_revoke_recovery_success(
    client, monkeypatch, wpd1_nudge_db
):
    response, row, backend, _provider = _run_ordinary_nudge(
        client,
        monkeypatch,
        status=TerminalStatus.IDLE,
        send_error=RuntimeError("injection failed"),
    )
    result = response.json()["results"][0]
    assert result["status"] == "rebound"
    assert result["nudge"] == {"status": "failed"}
    assert row.status is MessageStatus.FAILED
    backend.send_keys.assert_called_once()


def test_content_nudge_persists_fixed_neutral_body_before_any_terminal_input(
    client, monkeypatch, wpd1_nudge_db
):
    response, row, backend, _provider = _run_ordinary_nudge(
        client, monkeypatch, status=TerminalStatus.IDLE
    )
    assert response.json()["results"][0]["nudge"]["status"] == "sent"
    assert row.message == RECOVERY_NUDGE_MESSAGE
    assert "incident" not in row.message.lower()
    assert "sha256" not in row.message.lower()
    backend.send_keys.assert_called_once()


def test_content_recovery_rejects_non_codex_and_noncontent_scrub_flags(client):
    assert client.post(
        "/sessions/cao-test/recover",
        json={"reason": "content-flag", "provider": "grok_cli"},
    ).status_code == 400
    assert client.post(
        "/sessions/cao-test/recover",
        json={"reason": "provider-reauth", "force": True},
    ).status_code == 400
