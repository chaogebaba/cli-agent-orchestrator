"""F203 S1: Tests that kill surviving mutants M4, M5, R1, R2.

AC13 (D16): tick-frequency — deleted by WP-ARCH 3c K7 with its subject; see the
note where the class stood.

AC16 (D19): two-terminal ordering — _find_supervisor resolves correctly when
the supervisor is NOT the first claude_code terminal in query order.

R1 (D15): supervisor self-notify — deleted by WP-ARCH 3c K4 with its subject;
see the note where the class stood.

R2 (D19): role-based resolver — exemption at :393 and push target at :989 both
use the same role-based identity.
"""

from __future__ import annotations

from unittest.mock import patch

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


# ---------------------------------------------------------------------------
# WP-ARCH 3c K4: ``TestR1SupervisorSelfNotify`` is GONE with its subject
# ---------------------------------------------------------------------------
# Both arms drove the REAL ``StalledCallbackWatchdog.tick_waiting_inbox`` — that
# was the point of them, V1-c having rejected an inline reimplementation of the
# D15 branch. The positive arm seeded a ``WaitingInboxEpisode`` past grace and
# asserted the supervisor branch called ``_create_self_notify_obligation`` and
# left ``episode.fired`` unlatched; the negative arm asserted a worker whose
# ``caller_id`` equals its own id still took the refusal path and latched
# ``fired=True``.
#
# K4 deletes all three of those things together: the tick, the episode type it
# kept its state in, and the obligation helper — which lost both of its callers
# here and, as its own docstring predicted, went with them. There is no
# self-notify branch left to take and no refusal path to be preferred over, so
# the R1 distinction the arms drew has no code to draw it about. Nothing of it
# survives elsewhere: the waiting-inbox alert is not reimplemented under another
# name, it is withdrawn, and a supervisor whose inbox goes unread is now the
# delivery queue's problem rather than the watchdog's.
#
# What this file still owns is the resolver those arms shared with the rest of
# F203: ``AutoResponder._find_supervisor`` resolving by ROLE rather than by query
# order (M5/R2 above). That is untouched by K4 and still kills its mutants.
# ---------------------------------------------------------------------------
