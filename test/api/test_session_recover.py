from unittest.mock import AsyncMock, patch


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
