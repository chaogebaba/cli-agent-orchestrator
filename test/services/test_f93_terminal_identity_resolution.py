"""F93 acceptance tests for boot-time terminal identity reconciliation."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.backends.base import PaneIdentityReadResult
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.services import terminal_service


def _row(terminal_id: str, window: str, *, session: str = "cao-session") -> dict[str, str]:
    return {
        "id": terminal_id,
        "tmux_session": session,
        "tmux_window": window,
        "init_state": "ready",
    }


def _identity_backend() -> MagicMock:
    backend = MagicMock()
    backend.supports_identity_readback = True
    return backend


@pytest.fixture
def purge_effects(monkeypatch):
    deleted: list[str] = []
    updated: list[tuple[str, str]] = []

    def delete(terminal_id: str, *, preserve_warm_intent: bool) -> dict[str, bool]:
        assert preserve_warm_intent is False
        deleted.append(terminal_id)
        return {"terminal_deleted": True, "intent_deleted": False}

    def update(terminal_id: str, window: str) -> bool:
        updated.append((terminal_id, window))
        return True

    settlement = SimpleNamespace(busy_aborted=False)
    monkeypatch.setattr(terminal_service, "delete_terminal_and_warm_intent", delete)
    monkeypatch.setattr(
        terminal_service,
        "settle_pending_orphan_messages",
        lambda *, receiver_ids: settlement,
    )
    monkeypatch.setattr(terminal_service, "update_terminal_tmux_window", update)
    return SimpleNamespace(deleted=deleted, updated=updated)


def test_ac1_live_window_never_purges(monkeypatch, purge_effects) -> None:
    backend = _identity_backend()
    backend.window_liveness.return_value = "live"
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [_row("a", "old")])

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []
    backend.get_session_windows.assert_not_called()


def test_ac2_error_window_never_purges(monkeypatch, purge_effects) -> None:
    backend = _identity_backend()
    backend.window_liveness.return_value = "error"
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [_row("a", "old")])

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []
    backend.get_session_windows.assert_not_called()


def test_ac3_rename_self_heals_and_routes_subsequent_input(monkeypatch, purge_effects) -> None:
    metadata = _row("terminal-a", "old-name")
    backend = _identity_backend()
    backend.window_liveness.return_value = "gone"
    backend.enumerate_windows.return_value = ("ok", [{"name": "new-name", "index": "1"}])
    backend.read_pane_identity.return_value = PaneIdentityReadResult(identity="terminal-a")

    def update(terminal_id: str, window: str) -> bool:
        assert terminal_id == "terminal-a"
        metadata["tmux_window"] = window
        purge_effects.updated.append((terminal_id, window))
        return True

    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [metadata])
    monkeypatch.setattr(terminal_service, "update_terminal_tmux_window", update)

    assert terminal_service.purge_stale_terminal_records() == 0
    assert metadata["tmux_window"] == "new-name"
    assert purge_effects.deleted == []

    provider_manager = MagicMock()
    provider_manager.get_provider.return_value = None
    status_monitor = MagicMock()
    status_monitor.begin_dispatch.return_value = MagicMock()
    monkeypatch.setattr(terminal_service, "provider_manager", provider_manager)
    monkeypatch.setattr(terminal_service, "status_monitor", status_monitor)
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _terminal_id: metadata)
    monkeypatch.setattr(
        terminal_service, "inject_memory_context", lambda message, _terminal_id: message
    )
    monkeypatch.setattr(
        terminal_service, "preserve_draft_before_send", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(terminal_service, "update_last_active", lambda _terminal_id: True)

    assert terminal_service.send_input("terminal-a", "payload", expect_callback=False) is True
    backend.send_keys.assert_called_once_with(
        "cao-session",
        "new-name",
        "payload",
        enter_count=1,
        force_bracketed_paste=True,
        submit_delay=0.3,
    )


def test_ac4_genuinely_dead_window_purges_under_clean_scan(monkeypatch, purge_effects) -> None:
    backend = _identity_backend()
    backend.window_liveness.return_value = "gone"
    backend.enumerate_windows.return_value = ("ok", [{"name": "ordinary", "index": "1"}])
    backend.read_pane_identity.return_value = PaneIdentityReadResult(reason="missing_env")
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [_row("dead", "old")])

    assert terminal_service.purge_stale_terminal_records() == 1
    assert purge_effects.deleted == ["dead"]


def test_ac4b_unreadable_window_defers_then_clean_scan_purges(
    monkeypatch, purge_effects, caplog
) -> None:
    backend = _identity_backend()
    backend.window_liveness.return_value = "gone"
    backend.enumerate_windows.return_value = (
        "ok",
        [
            {"name": "split", "index": "1"},
            {"name": "clean", "index": "2"},
        ],
    )

    def first_read(_session: str, window: str) -> PaneIdentityReadResult:
        if window == "split":
            return PaneIdentityReadResult(reason="pane_cardinality")
        return PaneIdentityReadResult(reason="missing_env")

    backend.read_pane_identity.side_effect = first_read
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [_row("dead", "old")])
    caplog.set_level(logging.WARNING)

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []
    assert "purge_inconclusive terminal=dead" in caplog.text

    backend.enumerate_windows.return_value = ("ok", [{"name": "clean", "index": "2"}])
    backend.read_pane_identity.side_effect = None
    backend.read_pane_identity.return_value = PaneIdentityReadResult(reason="missing_env")

    assert terminal_service.purge_stale_terminal_records() == 1
    assert purge_effects.deleted == ["dead"]


def test_ac5_herdr_inherits_error_liveness_and_never_auto_purges(
    monkeypatch, purge_effects
) -> None:
    backend = HerdrBackend()
    assert backend.window_liveness("cao-session", "old") == "error"
    assert backend.get_session_windows("cao-session") == []
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [_row("herdr", "old")])

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []


@pytest.mark.parametrize("reason", ["read_error", "pane_cardinality", "incarnation_changed"])
def test_ac11_unreadable_identity_reason_is_never_absence(
    monkeypatch, purge_effects, caplog, reason: str
) -> None:
    backend = _identity_backend()
    backend.window_liveness.return_value = "gone"
    backend.enumerate_windows.return_value = ("ok", [{"name": "renamed", "index": "1"}])
    backend.read_pane_identity.return_value = PaneIdentityReadResult(reason=reason)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [_row("worker", "old")])
    caplog.set_level(logging.WARNING)

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []
    assert "purge_inconclusive terminal=worker" in caplog.text


def test_ac12_conflicting_name_declines_then_reconciles_after_conflict_clears(
    monkeypatch, purge_effects, caplog
) -> None:
    row_a = _row("terminal-a", "stale-name")
    row_b = _row("terminal-b", "claimed-name")
    rows = [row_a, row_b]
    backend = _identity_backend()
    backend.window_liveness.side_effect = lambda _session, window: (
        "gone" if window == "stale-name" else "live"
    )
    backend.enumerate_windows.return_value = ("ok", [{"name": "claimed-name", "index": "1"}])
    backend.read_pane_identity.return_value = PaneIdentityReadResult(identity="terminal-a")
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: rows)
    update = MagicMock(return_value=False)
    monkeypatch.setattr(terminal_service, "update_terminal_tmux_window", update)
    caplog.set_level(logging.WARNING)

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []
    assert row_a["tmux_window"] == "stale-name"
    assert row_b["tmux_window"] == "claimed-name"
    assert "purge_rename_conflict terminal=terminal-a window=claimed-name" in caplog.text

    rows.remove(row_b)

    def reconcile(_terminal_id: str, window: str) -> bool:
        row_a["tmux_window"] = window
        return True

    update.side_effect = reconcile
    update.return_value = True
    assert terminal_service.purge_stale_terminal_records() == 0
    assert row_a["tmux_window"] == "claimed-name"


def test_ac13_one_row_raise_skips_it_and_processes_remaining_rows(
    monkeypatch, purge_effects, caplog
) -> None:
    rows = [_row("raising", "bad"), _row("live", "live"), _row("dead", "dead")]
    backend = _identity_backend()
    backend.window_liveness.side_effect = lambda _session, window: (
        "live" if window == "live" else "gone"
    )

    call_count = {"n": 0}

    def enum_windows(_session: str):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("inventory failed")
        return ("ok", [{"name": "clean", "index": "1"}])

    backend.enumerate_windows.side_effect = enum_windows
    backend.read_pane_identity.return_value = PaneIdentityReadResult(reason="missing_env")
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: rows)
    caplog.set_level(logging.ERROR)

    assert terminal_service.purge_stale_terminal_records() == 1
    assert purge_effects.deleted == ["dead"]
    assert "purge_row_failed terminal=raising" in caplog.text


def test_ac13b_database_listing_failure_escapes_per_row_guard(monkeypatch) -> None:
    def fail_listing():
        raise RuntimeError("terminal listing failed")

    monkeypatch.setattr(terminal_service, "db_list_all_terminals", fail_listing)

    with pytest.raises(RuntimeError, match="terminal listing failed"):
        terminal_service.purge_stale_terminal_records()


def test_ac14_no_identity_readback_skips_gone_row_without_scanning(
    monkeypatch, purge_effects
) -> None:
    class GoneWithoutIdentityReadback:
        supports_identity_readback = False

        def __init__(self) -> None:
            self.scan_calls = 0
            self.read_calls = 0

        def window_liveness(self, _session: str, _window: str) -> str:
            return "gone"

        def enumerate_windows(self, _session: str):
            self.scan_calls += 1
            return ("ok", [])

        def get_session_windows(self, _session: str):
            self.scan_calls += 1
            return []

        def read_pane_identity(self, _session: str, _window: str):
            self.read_calls += 1
            return PaneIdentityReadResult(reason="missing_env")

    backend = GoneWithoutIdentityReadback()
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [_row("stub", "old")])

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []
    assert backend.scan_calls == 0
    assert backend.read_calls == 0


def test_ac15_duplicate_identity_is_ambiguous_before_first_match_pick(
    monkeypatch, purge_effects, caplog
) -> None:
    backend = _identity_backend()
    backend.window_liveness.return_value = "gone"
    backend.enumerate_windows.return_value = (
        "ok",
        [
            {"name": "clone-one", "index": "1"},
            {"name": "clone-two", "index": "2"},
        ],
    )
    backend.read_pane_identity.return_value = PaneIdentityReadResult(identity="terminal-a")
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service, "db_list_all_terminals", lambda: [_row("terminal-a", "old")]
    )
    update = MagicMock()
    monkeypatch.setattr(terminal_service, "update_terminal_tmux_window", update)
    caplog.set_level(logging.WARNING)

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []
    update.assert_not_called()
    assert "purge_identity_ambiguous terminal=terminal-a" in caplog.text
    assert "clone-one" in caplog.text
    assert "clone-two" in caplog.text


# --- §1.8 amendment: enumerate_windows subprocess-only discrimination ---


def test_ac_a1_enumeration_error_preserves_row(monkeypatch, purge_effects, caplog) -> None:
    """enumerate_windows returns (\"error\", None) with session alive + stored window renamed
    → row PRESERVED, purge_inconclusive logged."""
    backend = _identity_backend()
    backend.window_liveness.return_value = "gone"
    backend.enumerate_windows.return_value = ("error", None)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [_row("alive", "old")])
    caplog.set_level(logging.WARNING)

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []
    assert "purge_inconclusive terminal=alive" in caplog.text
    assert "enumeration_failed=True" in caplog.text


def test_ac_a2_session_absent_deletes_row(monkeypatch, purge_effects) -> None:
    """Session ABSENT (subprocess stderr \"can't find session\") → (\"ok\", []) → row DELETED."""
    backend = _identity_backend()
    backend.window_liveness.return_value = "gone"
    backend.enumerate_windows.return_value = ("ok", [])
    # No windows → no identity matches, no unreadable → hits delete path
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service, "db_list_all_terminals", lambda: [_row("dead", "old")])

    assert terminal_service.purge_stale_terminal_records() == 1
    assert purge_effects.deleted == ["dead"]


def test_ac_a3_ok_windows_with_identity_match_reconciles(monkeypatch, purge_effects) -> None:
    """(\"ok\", windows) with identity match → rename reconciles (unchanged behavior)."""
    backend = _identity_backend()
    backend.window_liveness.return_value = "gone"
    backend.enumerate_windows.return_value = ("ok", [{"name": "new-win"}])
    backend.read_pane_identity.return_value = PaneIdentityReadResult(identity="terminal-x")
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service, "db_list_all_terminals", lambda: [_row("terminal-x", "old-win")]
    )

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []
    assert purge_effects.updated == [("terminal-x", "new-win")]


def test_ac_a5_unclassifiable_failure_preserves_row(monkeypatch, purge_effects, caplog) -> None:
    """Unclassifiable failure (permission denied, timeout) → (\"error\", None) → row PRESERVED."""
    backend = _identity_backend()
    backend.window_liveness.return_value = "gone"
    backend.enumerate_windows.return_value = ("error", None)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service, "db_list_all_terminals", lambda: [_row("perm-denied", "win")]
    )
    caplog.set_level(logging.WARNING)

    assert terminal_service.purge_stale_terminal_records() == 0
    assert purge_effects.deleted == []
    assert "purge_inconclusive terminal=perm-denied" in caplog.text
    assert "enumeration_failed=True" in caplog.text
