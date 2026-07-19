from unittest.mock import AsyncMock, MagicMock, patch


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


def test_content_recovery_rejects_non_codex_and_noncontent_scrub_flags(client):
    assert client.post(
        "/sessions/cao-test/recover",
        json={"reason": "content-flag", "provider": "grok_cli"},
    ).status_code == 400
    assert client.post(
        "/sessions/cao-test/recover",
        json={"reason": "provider-reauth", "force": True},
    ).status_code == 400
