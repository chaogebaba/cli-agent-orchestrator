"""F542 (#398): reconcile terminal rows whose whole tmux_session is gone.

The tmux server does not survive a host reboot; the SQLite terminal rows do.
Without reconciliation the pipe-pane liveness watchdog re-arms a reader per
surviving row and probes ``get_history`` forever, logging
``Failed to get history from <session>:<window>`` every few seconds.

``reconcile_dead_session_terminals`` runs the cleanup/terminated path ONCE per
row whose session is absent, and must never touch a row whose session exists.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.services import terminal_service


def _row(terminal_id: str, session: str, *, window: str | None = None) -> dict[str, str]:
    return {
        "id": terminal_id,
        "tmux_session": session,
        "tmux_window": window or terminal_id,
        "init_state": "ready",
    }


@pytest.fixture
def reconcile_effects(monkeypatch):
    """Capture cleanup/delete/settle effects without touching a real DB."""
    deleted: list[str] = []
    cleaned: list[str] = []
    settled: list[str] = []

    def delete(terminal_id: str, *, preserve_warm_intent: bool) -> dict[str, bool]:
        assert preserve_warm_intent is False
        deleted.append(terminal_id)
        return {"terminal_deleted": True, "intent_deleted": False}

    def settle(*, receiver_ids: list[str]) -> SimpleNamespace:
        settled.extend(receiver_ids)
        return SimpleNamespace(busy_aborted=False)

    provider_manager = MagicMock()
    provider_manager.cleanup_provider.side_effect = lambda tid: cleaned.append(tid)

    monkeypatch.setattr(terminal_service, "delete_terminal_and_warm_intent", delete)
    monkeypatch.setattr(terminal_service, "settle_pending_orphan_messages", settle)
    monkeypatch.setattr(terminal_service, "provider_manager", provider_manager)
    return SimpleNamespace(deleted=deleted, cleaned=cleaned, settled=settled)


def _tmux_backend(session_exists) -> MagicMock:
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.session_exists.side_effect = session_exists
    return backend


def test_dead_session_row_reconciled_once(monkeypatch, reconcile_effects) -> None:
    """A row whose tmux_session is gone is reconciled exactly once."""
    backend = _tmux_backend(lambda name: False)  # session absent
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service, "db_list_all_terminals", lambda: [_row("dead", "cao-orch3")]
    )

    result = terminal_service.reconcile_dead_session_terminals()

    assert result == {"reconciled": 1, "skipped_session_live": 0}
    assert reconcile_effects.deleted == ["dead"]
    assert reconcile_effects.cleaned == ["dead"]
    assert reconcile_effects.settled == ["dead"]


def test_second_call_is_a_noop(monkeypatch, reconcile_effects) -> None:
    """Idempotent: once the row is deleted a second sweep sees nothing (no repeat)."""
    rows = [_row("dead", "cao-orch3")]
    backend = _tmux_backend(lambda name: False)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)

    # First sweep deletes; model the delete by draining the row list.
    def delete(terminal_id: str, *, preserve_warm_intent: bool) -> dict[str, bool]:
        reconcile_effects.deleted.append(terminal_id)
        rows.clear()
        return {"terminal_deleted": True, "intent_deleted": False}

    monkeypatch.setattr(terminal_service, "delete_terminal_and_warm_intent", delete)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: list(rows))

    first = terminal_service.reconcile_dead_session_terminals()
    second = terminal_service.reconcile_dead_session_terminals()

    assert first == {"reconciled": 1, "skipped_session_live": 0}
    assert second == {"reconciled": 0, "skipped_session_live": 0}
    assert reconcile_effects.deleted == ["dead"]  # deleted once, never twice


def test_live_session_row_never_touched(monkeypatch, reconcile_effects) -> None:
    """A row whose tmux_session still exists is left completely alone."""
    backend = _tmux_backend(lambda name: True)  # session present
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service, "db_list_all_terminals", lambda: [_row("alive", "cao-live")]
    )

    result = terminal_service.reconcile_dead_session_terminals()

    assert result == {"reconciled": 0, "skipped_session_live": 1}
    assert reconcile_effects.deleted == []
    assert reconcile_effects.cleaned == []


def test_session_exists_probed_once_per_session(monkeypatch, reconcile_effects) -> None:
    """A supervisor + N workers of one dead session cost exactly one probe."""
    backend = _tmux_backend(lambda name: False)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service,
        "db_list_all_terminals",
        lambda: [
            _row("sup", "cao-orch3"),
            _row("w1", "cao-orch3"),
            _row("w2", "cao-orch3"),
        ],
    )

    result = terminal_service.reconcile_dead_session_terminals()

    assert result == {"reconciled": 3, "skipped_session_live": 0}
    assert sorted(reconcile_effects.deleted) == ["sup", "w1", "w2"]
    # One distinct session -> exactly one existence probe despite three rows.
    assert backend.session_exists.call_count == 1


def test_mixed_sessions_only_dead_reconciled(monkeypatch, reconcile_effects) -> None:
    """Rows of a dead session are reconciled; rows of a live session are not."""

    def session_exists(name: str) -> bool:
        return name == "cao-live"

    backend = _tmux_backend(session_exists)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service,
        "db_list_all_terminals",
        lambda: [
            _row("dead-a", "cao-dead"),
            _row("alive-a", "cao-live"),
            _row("dead-b", "cao-dead"),
        ],
    )

    result = terminal_service.reconcile_dead_session_terminals()

    assert result == {"reconciled": 2, "skipped_session_live": 1}
    assert sorted(reconcile_effects.deleted) == ["dead-a", "dead-b"]


def test_session_exists_error_leaves_rows_intact(monkeypatch, reconcile_effects) -> None:
    """An unclassifiable backend failure is NOT proof of absence — never reconcile."""
    backend = _tmux_backend(lambda name: (_ for _ in ()).throw(RuntimeError("tmux flaked")))
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service, "db_list_all_terminals", lambda: [_row("maybe", "cao-orch3")]
    )

    result = terminal_service.reconcile_dead_session_terminals()

    assert result == {"reconciled": 0, "skipped_session_live": 1}
    assert reconcile_effects.deleted == []


def test_herdr_backend_is_skipped(monkeypatch, reconcile_effects) -> None:
    """Event-inbox backends (herdr) have no tmux session to reconcile."""
    backend = MagicMock()
    backend.supports_event_inbox.return_value = True
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [_row("x", "cao-orch3")])

    result = terminal_service.reconcile_dead_session_terminals()

    assert result == {"reconciled": 0, "skipped_session_live": 0}
    backend.session_exists.assert_not_called()
    assert reconcile_effects.deleted == []
