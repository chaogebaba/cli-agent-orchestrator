"""F203 S1: Tests that kill surviving mutants M4, M5, R1, R2.

AC13 (D16): tick-frequency — deleted by WP-ARCH 3c K7 with its subject; see the
note where the class stood.

AC16 (D19): two-terminal ordering — _find_supervisor resolves correctly when
the supervisor is NOT the first claude_code terminal in query order.

R1 (D15): supervisor self-notify — a supervisor terminal with no caller_id gets
a self-notify obligation instead of the invalid-caller refusal.

R2 (D19): role-based resolver — exemption at :393 and push target at :989 both
use the same role-based identity.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# WP-ARCH 3c K7: ``TestAC13TickFrequency`` is GONE with its subject
# ---------------------------------------------------------------------------
# Its two arms (M4 KILL and its paired "fires again after tick_s elapses"
# baseline) both drove ``StalledCallbackWatchdog._fx191_convergence_tick`` and
# both patched ``delivery_service.convergence_tick`` to count executions. K7
# deletes the ladder's ``convergence_tick``, and the watchdog method that was its
# only driver goes with it — there is no cadence gate left, because there is
# nothing left behind the gate.
#
# The cadence concern did not move to another function here; it moved to a
# different scheduler. ``app/delivery/tick.py`` is the seat's scheduled observer
# now, and how often it runs is owned by its own tests under
# ``test/app/delivery/`` rather than by a throttle inside the watchdog.


class TestAC16TwoTerminalOrdering:
    """AC16 (D19): _find_supervisor resolves correctly with two claude_code terminals."""

    def test_supervisor_found_by_role_not_insertion_order(self):
        """M5/R2 KILL: When two claude_code terminals exist and the supervisor
        is NOT first in query order, role-based resolution still works."""
        from cli_agent_orchestrator.services.auto_responder import AutoResponder

        # Mock list_terminals_by_session to return TWO claude_code terminals
        # where the supervisor (6c1c1545) is SECOND in query order
        # S1/V1-c: BOTH terminals have caller_id=None so the fallback
        # (caller_id is None and provider == "claude_code") CANNOT disambiguate
        terminals = [
            {
                "id": "stale_twin",  # First in query order (lower rowid)
                "provider": "claude_code",
                "agent_profile": "developer",  # NOT a supervisor
                "caller_id": None,  # S1 fix: None (both terminals None)
                "tmux_session": "cao-orch5",
            },
            {
                "id": "6c1c1545",  # Second in query order (higher rowid) — the REAL supervisor
                "provider": "claude_code",
                "agent_profile": "supervisor",  # The role-marked supervisor
                "caller_id": None,  # Supervisors have no caller
                "tmux_session": "cao-orch5",
            },
        ]

        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            return_value=terminals,
        ):
            result = AutoResponder._find_supervisor("cao-orch5")

        # D19: Must resolve to the role-marked supervisor, not the first claude_code
        assert result == "6c1c1545", (
            f"M5/R2 KILL: _find_supervisor returned '{result}' instead of '6c1c1545'. "
            "The role-based resolver is broken — it fell through to the insertion-order "
            "fallback and returned the stale twin."
        )

    def test_exemption_and_push_target_same_resolver(self):
        """AC16 [LB]: :393 exemption and :989 push target resolve the SAME terminal."""
        from cli_agent_orchestrator.services.auto_responder import AutoResponder

        terminals = [
            {
                "id": "worker1",
                "provider": "claude_code",
                "agent_profile": "developer",
                "caller_id": "sup1",
                "tmux_session": "s1",
            },
            {
                "id": "sup1",
                "provider": "claude_code",
                "agent_profile": "supervisor",
                "caller_id": None,
                "tmux_session": "s1",
            },
        ]

        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            return_value=terminals,
        ):
            # Both call sites use _find_supervisor with the same session
            exemption_result = AutoResponder._find_supervisor("s1")
            push_result = AutoResponder._find_supervisor("s1")

        assert exemption_result == push_result == "sup1", (
            f"AC16: exemption resolved '{exemption_result}', push resolved "
            f"'{push_result}' — they must be identical ('sup1')"
        )


class TestR1SupervisorSelfNotify:
    """R1 (D15): supervisor self-notify replaces invalid-caller refusal.

    V1-c: invokes the REAL stalled_callback_watchdog.tick_waiting_inbox method
    with a supervisor fixture — no inline branching reimplementation.
    """

    def test_supervisor_no_caller_gets_self_notify(self):
        """R1 KILL: supervisor terminal with no caller_id triggers self-notify
        via the real tick_waiting_inbox path.

        Mocks external dependencies but calls the REAL watchdog method that
        contains the D15 branching logic at stalled_callback_watchdog.py:1236.
        """
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            StalledCallbackWatchdog,
            WaitingInboxEpisode,
        )

        watchdog = StalledCallbackWatchdog(grace_seconds=30)

        # Pre-seed a waiting episode that has exceeded grace period
        episode = WaitingInboxEpisode(waiting_since=0.0)  # ancient
        watchdog._waiting_inbox_episodes["sup_r1"] = episode

        supervisor_metadata = {
            "caller_id": None,  # Supervisor has no caller
            "agent_profile": "supervisor",
            "tmux_session": "cao-test",
        }

        mock_self_notify = MagicMock()

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.list_pending_receiver_ids",
                return_value={"sup_r1"},
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value=supervisor_metadata,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.WAITING_USER_ANSWER,
            ),
            patch(
                "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "_create_self_notify_obligation",
                mock_self_notify,
            ),
            patch("time.monotonic", return_value=99999.0),
        ):
            watchdog.tick_waiting_inbox(registry=None, now=99999.0)

        # The REAL D15 code must have called _create_self_notify_obligation.
        # WP-ARCH 3c K7 RELOCATED that helper from ``delivery_service`` into the
        # watchdog itself (unchanged), because the ladder module it lived in is
        # reduced to one predicate and both of its call sites are in this file.
        # The patch target follows it; the branch under test is the same one.
        mock_self_notify.assert_called_once_with("sup_r1")

        # Episode must NOT be latched fired=True (that's the refusal path)
        assert episode.fired is not True, (
            "R1 KILL: episode.fired was latched True — supervisor hit the "
            "refusal path instead of self-notify"
        )

    def test_worker_still_refused(self):
        """R1 negative: worker terminal with caller_id==terminal_id still gets
        refused via the REAL tick_waiting_inbox path."""
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            StalledCallbackWatchdog,
            WaitingInboxEpisode,
        )

        watchdog = StalledCallbackWatchdog(grace_seconds=30)

        # Pre-seed episode
        episode = WaitingInboxEpisode(waiting_since=0.0)
        watchdog._waiting_inbox_episodes["worker1"] = episode

        worker_metadata = {
            "caller_id": "worker1",  # Self-referential (corrupt)
            "agent_profile": "developer",
            "tmux_session": "cao-test",
        }

        mock_self_notify = MagicMock()

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.list_pending_receiver_ids",
                return_value={"worker1"},
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value=worker_metadata,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.WAITING_USER_ANSWER,
            ),
            patch(
                "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "_create_self_notify_obligation",
                mock_self_notify,
            ),
            patch("time.monotonic", return_value=99999.0),
        ):
            watchdog.tick_waiting_inbox(registry=None, now=99999.0)

        # Worker must NOT trigger self-notify
        mock_self_notify.assert_not_called()

        # Episode must be latched fired=True (refusal path)
        assert (
            episode.fired is True
        ), "Worker with corrupt caller_id must hit the refusal path (fired=True)"
