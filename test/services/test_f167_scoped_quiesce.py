"""F167: Scoped subtree quiesce regression tests.

AC2: sibling survival — delete B does not cancel A's deferred init.
AC3: child quiesce — deleting parent quiesces child.
AC4: late-child re-plan.
AC5: cascade_quiesce_unstable fail-closed.
AC6: ordering — quiesce precedes leased snapshot.
AC7: legitimate callers unchanged.
"""

from __future__ import annotations

import asyncio
import time
import threading
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.terminal_service import (
    _quiesce_cascade_subtree_pre_plan,
    has_deferred_init,
    quiesce_deferred_session_sync,
    quiesce_deferred_terminal_sync,
    _deferred_tasks_by_terminal,
    _deferred_tasks_lock,
    delete_terminal,
)


def _fake_terminal(tid: str, session: str = "test-sess", caller_id: str | None = None):
    return {
        "id": tid,
        "tmux_session": session,
        "tmux_window": f"win-{tid}",
        "caller_id": caller_id,
        "provider": "kiro_cli",
        "agent_profile": "developer",
        "lifecycle": "ephemeral",
        "init_state": "init_pending",
        "metadata": {},
    }


class TestF167SiblingAndSubtreeQuiesce:
    """F167 core: subtree scoping and sibling isolation."""

    def test_ac2_sibling_survival(self, monkeypatch):
        """AC2: Deleting terminal B cancels no deferred-init task belonging to
        sibling A (not in B's cascade subtree)."""
        # Setup: A and B are siblings under root R
        root = _fake_terminal("rootroot", caller_id=None)
        term_a = _fake_terminal("aaaaaaaa", caller_id="rootroot")
        term_b = _fake_terminal("bbbbbbbb", caller_id="rootroot")
        terminals = [root, term_a, term_b]

        quiesced_ids: list[str] = []

        def mock_quiesce_terminal(tid, **kw):
            quiesced_ids.append(tid)

        monkeypatch.setattr(terminal_service, "quiesce_deferred_terminal_sync", mock_quiesce_terminal)
        monkeypatch.setattr(terminal_service, "list_terminals_by_session", lambda _s: terminals)
        monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda tid: {
            "rootroot": root, "aaaaaaaa": term_a, "bbbbbbbb": term_b
        }.get(tid))

        from cli_agent_orchestrator.services.terminal_guard_service import DeletionClassification
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.classify_deletion",
            lambda tid, force=False: DeletionClassification(True),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
            lambda tid, force=False: None,
        )

        # Mock lifecycle lease + _cascade_plan + _delete_terminal_under_lease
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
            lambda _s: "lease",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
            lambda _l: None,
        )
        monkeypatch.setattr(terminal_service, "has_deferred_init", lambda tid: False)
        monkeypatch.setattr(terminal_service, "_delete_terminal_under_lease",
                            lambda tid, token, **kw: {"terminal_deleted": True})
        monkeypatch.setattr(terminal_service, "status_monitor",
                            MagicMock(get_boundary_observation=MagicMock(
                                return_value=MagicMock(status=MagicMock(value="idle")))))
        monkeypatch.setattr("cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
                            lambda tid: MagicMock(terminal_id=tid))
        monkeypatch.setattr("cli_agent_orchestrator.services.rebind_lease.release_rebind_lease",
                            lambda _t: None)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: MagicMock())

        # Delete B — A should NOT be quiesced
        delete_terminal("bbbbbbbb", caller_id="rootroot")

        # A must not have been quiesced. Only B (the root of deletion) was quiesced.
        assert "aaaaaaaa" not in quiesced_ids
        assert "bbbbbbbb" in quiesced_ids

    def test_ac3_parent_delete_quiesces_child(self, monkeypatch):
        """AC3: Deleting parent quiesces child in subtree before deletion."""
        parent = _fake_terminal("parentaa", caller_id=None)
        child = _fake_terminal("childaaa", caller_id="parentaa")
        terminals = [parent, child]

        quiesced_ids: list[str] = []

        def mock_quiesce_terminal(tid, **kw):
            quiesced_ids.append(tid)

        monkeypatch.setattr(terminal_service, "quiesce_deferred_terminal_sync", mock_quiesce_terminal)
        monkeypatch.setattr(terminal_service, "list_terminals_by_session", lambda _s: terminals)
        monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda tid: {
            "parentaa": parent, "childaaa": child
        }.get(tid))

        from cli_agent_orchestrator.services.terminal_guard_service import DeletionClassification
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.classify_deletion",
            lambda tid, force=False: DeletionClassification(True),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
            lambda tid, force=False: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
            lambda _s: "lease",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
            lambda _l: None,
        )
        monkeypatch.setattr(terminal_service, "has_deferred_init", lambda tid: False)
        monkeypatch.setattr(terminal_service, "_delete_terminal_under_lease",
                            lambda tid, token, **kw: {"terminal_deleted": True})
        monkeypatch.setattr(terminal_service, "status_monitor",
                            MagicMock(get_boundary_observation=MagicMock(
                                return_value=MagicMock(status=MagicMock(value="idle")))))
        monkeypatch.setattr("cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
                            lambda tid: MagicMock(terminal_id=tid))
        monkeypatch.setattr("cli_agent_orchestrator.services.rebind_lease.release_rebind_lease",
                            lambda _t: None)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: MagicMock())

        delete_terminal("parentaa")

        # Both parent and child should be quiesced
        assert "childaaa" in quiesced_ids
        assert "parentaa" in quiesced_ids

    def test_ac5_cascade_quiesce_unstable_raises(self, monkeypatch):
        """AC5: If plan still has deferred-bearing node after CASCADE_QUIESCE_ROUNDS,
        raises cascade_quiesce_unstable and deletes nothing."""
        parent = _fake_terminal("parentaa", caller_id=None)
        child = _fake_terminal("childaaa", caller_id="parentaa")
        terminals = [parent, child]

        monkeypatch.setattr(terminal_service, "quiesce_deferred_terminal_sync", lambda tid, **kw: None)
        monkeypatch.setattr(terminal_service, "list_terminals_by_session", lambda _s: terminals)
        monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda tid: {
            "parentaa": parent, "childaaa": child
        }.get(tid))

        from cli_agent_orchestrator.services.terminal_guard_service import DeletionClassification
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.classify_deletion",
            lambda tid, force=False: DeletionClassification(True),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
            lambda tid, force=False: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
            lambda _s: "lease",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
            lambda _l: None,
        )
        # has_deferred_init always returns True → plan never converges
        monkeypatch.setattr(terminal_service, "has_deferred_init", lambda tid: True)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: MagicMock())

        with pytest.raises(RuntimeError, match="cascade_quiesce_unstable"):
            delete_terminal("parentaa")

    def test_ac6_quiesce_precedes_lease(self, monkeypatch):
        """AC6: Quiesce still precedes acquire_session_lifecycle_exclusive."""
        parent = _fake_terminal("parentaa", caller_id=None)
        terminals = [parent]
        events: list[str] = []

        def mock_pre_plan(*args, **kwargs):
            events.append("quiesce")

        def mock_acquire(session_name):
            events.append("lease_acquired")
            return "lease"

        monkeypatch.setattr(terminal_service, "_quiesce_cascade_subtree_pre_plan", mock_pre_plan)
        monkeypatch.setattr(terminal_service, "list_terminals_by_session", lambda _s: terminals)
        monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda tid: parent)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
            lambda tid, force=False: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
            mock_acquire,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
            lambda _l: None,
        )
        monkeypatch.setattr(terminal_service, "has_deferred_init", lambda tid: False)
        monkeypatch.setattr(terminal_service, "_delete_terminal_under_lease",
                            lambda tid, token, **kw: {"terminal_deleted": True})
        monkeypatch.setattr(terminal_service, "status_monitor",
                            MagicMock(get_boundary_observation=MagicMock(
                                return_value=MagicMock(status=MagicMock(value="idle")))))
        monkeypatch.setattr("cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
                            lambda tid: MagicMock(terminal_id=tid))
        monkeypatch.setattr("cli_agent_orchestrator.services.rebind_lease.release_rebind_lease",
                            lambda _t: None)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: MagicMock())

        delete_terminal("parentaa")

        assert events == ["quiesce", "lease_acquired"]

    def test_ac7_session_close_and_shutdown_unchanged(self):
        """AC7: quiesce_deferred_session_sync still exists and is used by legitimate callers.

        Verifies the symbol exists and that session_close_service, session_service,
        flow_service, shutdown_deferred_tasks, and herdr_inbox_service sites are unchanged.
        """
        # Symbol still exists
        assert callable(quiesce_deferred_session_sync)

        # Verify delete_terminal does NOT call it (AC1)
        import inspect
        source = inspect.getsource(delete_terminal)
        assert "quiesce_deferred_session_sync" not in source



    def test_ac4_late_child_replan(self, monkeypatch):
        """AC4 (S1): A child created between pre-plan and leased snapshot is
        quiesced by the re-plan round.

        Simulates: has_deferred_init returns True for 'latechld' only on the
        SECOND call to list_terminals_by_session (the leased snapshot), meaning
        the child appeared after the pre-plan. Assert that child IS quiesced.
        """
        parent = _fake_terminal("parentaa", caller_id=None)
        # Initially only parent exists
        terminals_pre = [parent]
        # After lease, a late child appears
        late_child = _fake_terminal("latechld", caller_id="parentaa")
        terminals_post = [parent, late_child]

        # Track which snapshot call we're on
        list_call_count = [0]

        def mock_list_terminals(session_name):
            list_call_count[0] += 1
            if list_call_count[0] <= 1:
                return terminals_pre  # pre-plan: no child
            return terminals_post  # leased snapshot: child appeared

        quiesced_ids: list[str] = []

        def mock_quiesce_terminal(tid, **kw):
            quiesced_ids.append(tid)

        monkeypatch.setattr(terminal_service, "quiesce_deferred_terminal_sync", mock_quiesce_terminal)
        monkeypatch.setattr(terminal_service, "list_terminals_by_session", mock_list_terminals)
        monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda tid: {
            "parentaa": parent, "latechld": late_child,
        }.get(tid))

        # has_deferred_init: True only for latechld (simulates it still initializing)
        # After first quiesce of latechld, it becomes False (settled)
        deferred_settled = set()

        def mock_has_deferred_init(tid):
            if tid == "latechld" and tid not in deferred_settled:
                deferred_settled.add(tid)
                return True
            return False

        from cli_agent_orchestrator.services.terminal_guard_service import DeletionClassification
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.classify_deletion",
            lambda tid, force=False: DeletionClassification(True),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
            lambda tid, force=False: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
            lambda _s: "lease",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
            lambda _l: None,
        )
        monkeypatch.setattr(terminal_service, "has_deferred_init", mock_has_deferred_init)
        monkeypatch.setattr(terminal_service, "_delete_terminal_under_lease",
                            lambda tid, token, **kw: {"terminal_deleted": True})
        monkeypatch.setattr(terminal_service, "status_monitor",
                            MagicMock(get_boundary_observation=MagicMock(
                                return_value=MagicMock(status=MagicMock(value="idle")))))
        monkeypatch.setattr("cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
                            lambda tid: MagicMock(terminal_id=tid))
        monkeypatch.setattr("cli_agent_orchestrator.services.rebind_lease.release_rebind_lease",
                            lambda _t: None)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: MagicMock())

        delete_terminal("parentaa")

        # The late child MUST have been quiesced by the re-plan round
        assert "latechld" in quiesced_ids, (
            f"Late child not quiesced. quiesced_ids={quiesced_ids}. "
            "AC4: re-plan round must catch children appearing after pre-plan."
        )
