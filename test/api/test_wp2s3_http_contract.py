"""WP2S3 HTTP conflict and start bootstrap contracts."""

import threading
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.services.provider_session_lease import (
    acquire_provider_session_lease,
    release_provider_session_lease,
)
from cli_agent_orchestrator.services.session_lifecycle_lease import (
    acquire_session_lifecycle_shared, release_session_lifecycle_lease,
)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("delete", "/terminals/abcd1234"),
        ("delete", "/sessions/cao-race"),
        ("post", "/sessions/cao-race/close"),
    ],
)
def test_provisional_owner_race_reaches_service_and_has_zero_teardown_effects(
    client, monkeypatch, method, path,
):
    """All public teardown surfaces hit the real guard before destructive work."""
    from cli_agent_orchestrator.services import session_close_service, session_service
    from cli_agent_orchestrator.services import terminal_service

    row = {
        "id": "abcd1234", "tmux_session": "cao-race", "tmux_window": "w",
        "provider": "codex", "agent_profile": "dev",
        "provider_session_id": "provisional-uuid",
    }
    backend = MagicMock()
    fifo = MagicMock()
    db_delete = MagicMock()
    plugin_dispatch = MagicMock()
    cleanup = MagicMock()
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _id: row)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "fifo_manager", fifo)
    monkeypatch.setattr(terminal_service, "delete_terminal_and_warm_intent", db_delete)
    monkeypatch.setattr(terminal_service, "dispatch_plugin_event", plugin_dispatch)
    monkeypatch.setattr(terminal_service.provider_manager, "cleanup_provider", cleanup)
    monkeypatch.setattr(session_service, "list_terminals_by_session", lambda _s: [row])
    monkeypatch.setattr(session_service, "finalize_session", MagicMock())
    monkeypatch.setattr(session_close_service, "list_terminals_by_session", lambda _s: [row])
    monkeypatch.setattr(session_close_service, "list_ready_provider_sessions_for_session", lambda _s: [])
    monkeypatch.setattr(session_close_service, "list_warm_intents", lambda _s: [])
    monkeypatch.setattr(session_close_service, "get_ready_provider_session_by_source_terminal", lambda _t: None)
    monkeypatch.setattr(session_close_service, "load_agent_profile", lambda _p: None)

    lease = acquire_provider_session_lease("provisional-uuid")
    assert lease is not None
    try:
        response = getattr(client, method)(path)
    finally:
        release_provider_session_lease(lease)

    assert response.status_code == 409
    assert response.json()["detail"] == "resume_in_progress"
    backend.assert_not_called()
    fifo.stop_reader.assert_not_called()
    db_delete.assert_not_called()
    cleanup.assert_not_called()
    plugin_dispatch.assert_not_called()


@pytest.mark.parametrize("path", ["/sessions/cao-race", "/sessions/cao-race/close"])
@pytest.mark.parametrize("provisional_first", [True, False])
def test_session_teardown_preflights_all_rows_before_deleting_any(
    client, monkeypatch, path, provisional_first,
):
    from cli_agent_orchestrator.services import session_close_service, session_service
    from cli_agent_orchestrator.services import terminal_service

    normal = {
        "id": "a-normal", "tmux_session": "cao-race", "tmux_window": "a",
        "provider": "codex", "agent_profile": "dev", "provider_session_id": None,
    }
    provisional = {
        "id": "z-provisional", "tmux_session": "cao-race", "tmux_window": "z",
        "provider": "codex", "agent_profile": "dev",
        "provider_session_id": "session-race-uuid",
    }
    rows = [provisional, normal] if provisional_first else [normal, provisional]
    by_id = {row["id"]: row for row in rows}
    deleted = MagicMock()
    backend = MagicMock()
    fifo = MagicMock()
    plugin = MagicMock()
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", by_id.get)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "fifo_manager", fifo)
    monkeypatch.setattr(terminal_service, "delete_terminal_and_warm_intent", deleted)
    monkeypatch.setattr(terminal_service, "dispatch_plugin_event", plugin)
    monkeypatch.setattr(session_service, "list_terminals_by_session", lambda _s: rows)
    monkeypatch.setattr(session_service, "finalize_session", MagicMock())
    monkeypatch.setattr(session_close_service, "list_terminals_by_session", lambda _s: rows)
    monkeypatch.setattr(session_close_service, "list_ready_provider_sessions_for_session", lambda _s: [])
    monkeypatch.setattr(session_close_service, "list_warm_intents", lambda _s: [])
    monkeypatch.setattr(session_close_service, "get_ready_provider_session_by_source_terminal", lambda _t: None)
    monkeypatch.setattr(session_close_service, "load_agent_profile", lambda _p: None)

    lease = acquire_provider_session_lease("session-race-uuid")
    assert lease is not None
    try:
        response = client.post(path) if path.endswith("/close") else client.delete(path)
    finally:
        release_provider_session_lease(lease)

    assert response.status_code == 409
    assert response.json()["detail"] == "resume_in_progress"
    deleted.assert_not_called()  # a-normal survives regardless of row ordering
    backend.assert_not_called()
    fifo.stop_reader.assert_not_called()
    plugin.assert_not_called()


@pytest.mark.parametrize("path", ["/sessions/cao-race", "/sessions/cao-race/close"])
@pytest.mark.parametrize("barrier", ["before_first_delete", "after_first_delete"])
def test_session_lifecycle_contention_keeps_every_row_untouched(
    client, monkeypatch, path, barrier,
):
    """EXCLUSIVE remains held at both in-sweep barriers on both HTTP surfaces."""
    from cli_agent_orchestrator.services import session_close_service, session_service
    from cli_agent_orchestrator.services import terminal_service

    durable = {
        "a-normal": {"id": "a-normal", "tmux_session": "cao-race", "agent_profile": "dev"},
        "z-racing": {"id": "z-racing", "tmux_session": "cao-race", "agent_profile": "dev"},
    }
    original = {key: dict(value) for key, value in durable.items()}
    rows = list(durable.values())
    reached = threading.Event()
    attempted = threading.Event()
    result = {}
    acquired = []
    delete_calls = []
    backend = MagicMock()
    fifo = MagicMock()
    db_delete = MagicMock()
    plugin = MagicMock()
    monkeypatch.setattr(session_service, "list_terminals_by_session", lambda _s: rows)
    monkeypatch.setattr(session_service, "finalize_session", MagicMock())
    monkeypatch.setattr(session_close_service, "list_terminals_by_session", lambda _s: rows)
    monkeypatch.setattr(session_close_service, "list_ready_provider_sessions_for_session", lambda _s: [])
    monkeypatch.setattr(session_close_service, "list_warm_intents", lambda _s: [])
    monkeypatch.setattr(session_close_service, "get_ready_provider_session_by_source_terminal", lambda _t: None)
    monkeypatch.setattr(session_close_service, "load_agent_profile", lambda _p: None)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "fifo_manager", fifo)
    monkeypatch.setattr(terminal_service, "delete_terminal_and_warm_intent", db_delete)
    monkeypatch.setattr(terminal_service, "dispatch_plugin_event", plugin)

    def pause_and_abort():
        reached.set()
        assert attempted.wait(2)
        raise RuntimeError("resume_in_progress")

    def preflight(_rows):
        if barrier == "before_first_delete":
            pause_and_abort()

    def delete_under_lease(terminal_id, _token, **_kwargs):
        delete_calls.append(terminal_id)
        if barrier == "after_first_delete" and len(delete_calls) == 2:
            pause_and_abort()
        return {"terminal_deleted": True, "intent_deleted": False, "intent_error": None}

    monkeypatch.setattr(terminal_service, "preflight_session_teardown", preflight)
    monkeypatch.setattr(terminal_service, "_delete_terminal_under_lease", delete_under_lease)

    def request():
        result["response"] = (
            client.post(path) if path.endswith("/close") else client.delete(path)
        )

    request_thread = threading.Thread(target=request)
    request_thread.start()
    assert reached.wait(2)
    intent = acquire_session_lifecycle_shared("cao-race")
    acquired.append(intent)
    if intent is not None:  # early-release mutant: perform the forbidden association
        durable["z-racing"]["provider_session_id"] = "late-publication"
    attempted.set()
    request_thread.join(2)
    assert not request_thread.is_alive()
    if intent is not None:
        release_session_lifecycle_lease(intent)

    response = result["response"]
    assert response.status_code == 409
    assert response.json()["detail"] == "resume_in_progress"
    assert acquired == [None]
    assert durable == original
    backend.assert_not_called()
    fifo.stop_reader.assert_not_called()
    db_delete.assert_not_called()
    plugin.assert_not_called()


@pytest.mark.parametrize("path", ["/sessions/cao-race", "/sessions/cao-race/close"])
def test_exclusive_precedes_authoritative_terminal_snapshot(client, monkeypatch, path):
    from cli_agent_orchestrator.services import session_close_service, session_service
    from cli_agent_orchestrator.services import session_lifecycle_lease, terminal_service

    events = []
    real_acquire = session_lifecycle_lease.acquire_session_lifecycle_exclusive
    def acquire(session_name):
        events.append("exclusive")
        return real_acquire(session_name)
    def enumerate_rows(_session_name):
        events.append("enumerate")
        return []
    monkeypatch.setattr(session_lifecycle_lease, "acquire_session_lifecycle_exclusive", acquire)
    monkeypatch.setattr(session_service, "list_terminals_by_session", enumerate_rows)
    monkeypatch.setattr(session_close_service, "list_terminals_by_session", enumerate_rows)
    monkeypatch.setattr(session_service, "finalize_session", MagicMock())
    monkeypatch.setattr(session_close_service, "list_ready_provider_sessions_for_session", lambda _s: [])
    monkeypatch.setattr(session_close_service, "list_warm_intents", lambda _s: [])
    monkeypatch.setattr(session_close_service, "delete_session_epoch", MagicMock())
    monkeypatch.setattr(terminal_service, "preflight_session_teardown", MagicMock())
    response = client.post(path) if path.endswith("/close") else client.delete(path)
    assert response.status_code == 200
    assert events[:2] == ["exclusive", "enumerate"]


def test_terminal_delete_resume_in_progress_is_409(client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as service:
        service.delete_terminal.side_effect = RuntimeError("resume_in_progress")
        response = client.delete("/terminals/abcd1234")
    assert response.status_code == 409
    assert response.json()["detail"] == "resume_in_progress"
    service.delete_terminal.assert_called_once_with("abcd1234", registry=ANY)


def test_session_delete_resume_in_progress_is_409(client):
    with patch("cli_agent_orchestrator.api.main.session_service") as service:
        service.delete_session.side_effect = RuntimeError("resume_in_progress")
        response = client.delete("/sessions/cao-race")
    assert response.status_code == 409
    assert response.json()["detail"] == "resume_in_progress"


def test_session_close_resume_in_progress_is_409(client):
    with patch(
        "cli_agent_orchestrator.services.session_close_service.close_session",
        side_effect=RuntimeError("resume_in_progress"),
    ):
        response = client.post("/sessions/cao-race/close")
    assert response.status_code == 409
    assert response.json()["detail"] == "resume_in_progress"


def test_start_seed_failure_is_closed_422_v1(client):
    with patch(
        "cli_agent_orchestrator.api.main.session_service.start_session",
        new=AsyncMock(side_effect=RuntimeError("seed_exec_failed")),
    ):
        response = client.post("/sessions/start", params={"agent_profile": "dev"})
    assert response.status_code == 422
    assert response.json() == {
        "schema_version": "cao.session-start/v1", "session": None,
        "supervisor_terminal": None,
        "bootstrap": {"mode": "seed_resume", "status": "seed_failed",
                      "error_code": "seed_exec_failed"},
        "manifest": None, "manifest_error": None,
    }
