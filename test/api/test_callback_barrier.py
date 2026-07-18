"""Authenticated callback-barrier HTTP surfaces."""

from unittest.mock import patch


def test_status_uses_typed_id_selector(client):
    with patch(
        "cli_agent_orchestrator.api.main.callback_barrier_status",
        return_value={"id": 7, "label": "123", "state": "OPEN", "members": []},
    ) as status:
        response = client.get("/barriers/status", params={"barrier_id": 7})
    assert response.status_code == 200
    assert response.json()["label"] == "123"
    status.assert_called_once_with(barrier_id=7, barrier_label=None, owner_id=None)


def test_status_label_is_not_guessed_as_numeric(client):
    with patch(
        "cli_agent_orchestrator.api.main.callback_barrier_status",
        return_value={"id": 8, "label": "123", "state": "OPEN", "members": []},
    ) as status:
        response = client.get(
            "/barriers/status", params={"barrier_label": "123", "owner": "aaaaaaaa"}
        )
    assert response.status_code == 200
    status.assert_called_once_with(barrier_id=None, barrier_label="123", owner_id="aaaaaaaa")


def test_cancel_releases_and_wakes_each_receiver(client):
    with (
        patch(
            "cli_agent_orchestrator.api.main.cancel_callback_barrier",
            return_value={
                "id": 9,
                "state": "CANCELLED",
                "released": 2,
                "receiver_ids": ["aaaaaaaa", "bbbbbbbb"],
            },
        ),
        patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending") as deliver,
    ):
        response = client.post("/barriers/cancel", params={"barrier_id": 9})
    assert response.status_code == 200
    assert response.json()["released"] == 2
    assert [call.args[0] for call in deliver.call_args_list] == ["aaaaaaaa", "bbbbbbbb"]


def test_selector_domain_error_is_typed_400(client):
    response = client.get("/barriers/status")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "barrier_selector_requires_exactly_one"
