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
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.terminal_service import (
    _deferred_tasks_by_terminal,
    _deferred_tasks_lock,
    _quiesce_cascade_subtree_pre_plan,
    delete_terminal,
    has_deferred_init,
    quiesce_deferred_terminal_sync,
    quiesce_session_teardown_set_sync,
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

        monkeypatch.setattr(
            terminal_service, "quiesce_deferred_terminal_sync", mock_quiesce_terminal
        )
        monkeypatch.setattr(terminal_service, "list_terminals_by_session", lambda _s: terminals)
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {"rootroot": root, "aaaaaaaa": term_a, "bbbbbbbb": term_b}.get(tid),
        )

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
        monkeypatch.setattr(
            terminal_service,
            "_delete_terminal_under_lease",
            lambda tid, token, **kw: {"terminal_deleted": True},
        )
        monkeypatch.setattr(
            terminal_service,
            "status_monitor",
            MagicMock(
                get_boundary_observation=MagicMock(
                    return_value=MagicMock(status=MagicMock(value="idle"))
                )
            ),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
            lambda tid: MagicMock(terminal_id=tid),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease", lambda _t: None
        )
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

        monkeypatch.setattr(
            terminal_service, "quiesce_deferred_terminal_sync", mock_quiesce_terminal
        )
        monkeypatch.setattr(terminal_service, "list_terminals_by_session", lambda _s: terminals)
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {"parentaa": parent, "childaaa": child}.get(tid),
        )

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
        monkeypatch.setattr(
            terminal_service,
            "_delete_terminal_under_lease",
            lambda tid, token, **kw: {"terminal_deleted": True},
        )
        monkeypatch.setattr(
            terminal_service,
            "status_monitor",
            MagicMock(
                get_boundary_observation=MagicMock(
                    return_value=MagicMock(status=MagicMock(value="idle"))
                )
            ),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
            lambda tid: MagicMock(terminal_id=tid),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease", lambda _t: None
        )
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

        monkeypatch.setattr(
            terminal_service, "quiesce_deferred_terminal_sync", lambda tid, **kw: None
        )
        monkeypatch.setattr(terminal_service, "list_terminals_by_session", lambda _s: terminals)
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {"parentaa": parent, "childaaa": child}.get(tid),
        )

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

        def mock_acquire(session_name, terminal_id):
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
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_terminal_exclusive",
            mock_acquire,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_terminal_exclusive",
            lambda _l: None,
        )
        monkeypatch.setattr(terminal_service, "has_deferred_init", lambda tid: False)
        monkeypatch.setattr(
            terminal_service,
            "_delete_terminal_under_lease",
            lambda tid, token, **kw: {"terminal_deleted": True},
        )
        monkeypatch.setattr(
            terminal_service,
            "status_monitor",
            MagicMock(
                get_boundary_observation=MagicMock(
                    return_value=MagicMock(status=MagicMock(value="idle"))
                )
            ),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
            lambda tid: MagicMock(terminal_id=tid),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease", lambda _t: None
        )
        monkeypatch.setattr(terminal_service, "get_backend", lambda: MagicMock())

        delete_terminal("parentaa")

        assert events == ["quiesce", "lease_acquired"]

    def test_ac7_session_paths_route_through_the_teardown_set(self):
        """AC7 (#27): the session-wide variant is GONE; session paths route here.

        ``quiesce_deferred_session_sync`` scanned the in-memory deferred registry
        by ``record.session_name`` — a key that is not the teardown key and that
        any per-terminal caller could pick up by mistake (the F167 incident).
        It is deleted; the session-shaped entry point is
        ``quiesce_session_teardown_set_sync``, which quiesces the same terminal
        list the teardown is about to delete.
        """
        import inspect

        assert callable(quiesce_session_teardown_set_sync)
        assert not hasattr(terminal_service, "quiesce_deferred_session_sync")

        # Verify delete_terminal does NOT call any session-shaped quiesce (AC1)
        source = inspect.getsource(delete_terminal)
        assert "quiesce_deferred_session_sync" not in source
        assert "quiesce_session_teardown_set_sync" not in source

        # Both session-teardown callers use the routed entry point.
        from cli_agent_orchestrator.services import session_close_service, session_service

        for module, func in (
            (session_close_service, session_close_service.close_session),
            (session_service, session_service.delete_session),
        ):
            body = inspect.getsource(func)
            assert "quiesce_session_teardown_set_sync" in body
            assert "quiesce_deferred_session_sync" not in body

    def test_ac7_quiesce_set_is_the_db_teardown_set_not_the_record_session_name(self, monkeypatch):
        """#27: a deferred record with NO session_name is still quiesced.

        ``schedule_deferred_init`` stores ``snapshot.get("tmux_session")``, which
        is ``None`` when the metadata read came back empty. The deleted
        session-wide variant filtered on that field, so such a record survived a
        session close and its init task ran on into a teardown that was deleting
        its row. Revert-sensitive: restoring the by-session-name scan quiesces
        nothing here.
        """
        quiesced: list[str] = []
        monkeypatch.setattr(
            terminal_service,
            "quiesce_deferred_terminal_sync",
            lambda tid, **_kw: quiesced.append(tid),
        )
        monkeypatch.setattr(
            terminal_service,
            "list_terminals_by_session",
            lambda session: [_fake_terminal("orphaned", session=session)],
        )
        with _deferred_tasks_lock:
            _deferred_tasks_by_terminal["orphaned"] = SimpleNamespace(
                task=MagicMock(), loop=MagicMock(), generation="g", session_name=None
            )
        try:
            terminal_service.quiesce_session_teardown_set_sync("test-sess")
        finally:
            with _deferred_tasks_lock:
                _deferred_tasks_by_terminal.pop("orphaned", None)

        assert quiesced == ["orphaned"]

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

        monkeypatch.setattr(
            terminal_service, "quiesce_deferred_terminal_sync", mock_quiesce_terminal
        )
        monkeypatch.setattr(terminal_service, "list_terminals_by_session", mock_list_terminals)
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {
                "parentaa": parent,
                "latechld": late_child,
            }.get(tid),
        )

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
        monkeypatch.setattr(
            terminal_service,
            "_delete_terminal_under_lease",
            lambda tid, token, **kw: {"terminal_deleted": True},
        )
        monkeypatch.setattr(
            terminal_service,
            "status_monitor",
            MagicMock(
                get_boundary_observation=MagicMock(
                    return_value=MagicMock(status=MagicMock(value="idle"))
                )
            ),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
            lambda tid: MagicMock(terminal_id=tid),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease", lambda _t: None
        )
        monkeypatch.setattr(terminal_service, "get_backend", lambda: MagicMock())

        delete_terminal("parentaa")

        # The late child MUST have been quiesced by the re-plan round
        assert "latechld" in quiesced_ids, (
            f"Late child not quiesced. quiesced_ids={quiesced_ids}. "
            "AC4: re-plan round must catch children appearing after pre-plan."
        )
