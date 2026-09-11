"""WPDT — wp-delivery-truth acceptance criteria tests.

AC1/AC2/AC3: the WebSocket doorbell plane — deleted by WP-ARCH 3c K3b; see the
     note at the foot of this file.
AC4: F152 producer — fresh pane has cc_team_inbox_path; self-heal fills missing.
     The derivation survived WP-ARCH 3c K2 in ``services/native_delivery_health``;
     the arms follow it there.
AC5: the obligation ladder's supervisor exemption — deleted by WP-ARCH 3c K7; see
     the note at the foot of this file.
AC6: F136/F276 regression tests (ordering + full drain).
AC7/AC8: doctrine arming + flag-flip rollback for that same deleted plane; see
     the note at the foot of this file.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# AC4: F152 producer — cc_team_inbox_path at creation + self-heal
# ---------------------------------------------------------------------------


class TestAC4F152Producer:
    """AC4: fresh pane has cc_team_inbox_path; self-heal fills missing.

    WP-ARCH 3c K2 deleted ``teammate_push_service``, but NOT this pair. The
    socket-path derivation and its F152 self-heal were the NATIVE half of that
    file and moved unchanged to ``services/native_delivery_health`` (losing only
    their leading underscores, since they are now the module's public surface).
    The subject is the same address derivation, so the arms move with it rather
    than being retired.
    """

    def test_derive_cc_team_inbox_path_returns_valid_path(self):
        """derive_cc_team_inbox_path builds ~/.claude/projects/{key}/team-lead.json."""
        from cli_agent_orchestrator.services.native_delivery_health import (
            derive_cc_team_inbox_path,
        )

        result = derive_cc_team_inbox_path("/home/user/project")
        assert result is not None
        assert "team-lead.json" in str(result)
        assert ".claude/projects/" in str(result)
        # The cwd_key replaces non-alphanumeric with -
        assert "-home-user-project" in str(result)

    def test_resolve_inbox_path_self_heal(self):
        """resolve_inbox_path derives path when metadata is missing."""
        from cli_agent_orchestrator.services.native_delivery_health import resolve_inbox_path

        mock_metadata = {
            "metadata": {},
            "provider": "claude_code",
            "working_directory": "/data/claude-scratch/worker-scratch/test_project",
        }
        with (
            patch(
                "cli_agent_orchestrator.services.native_delivery_health.get_terminal_metadata",
                return_value=mock_metadata,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.update_terminal_metadata",
            ),
        ):
            result = resolve_inbox_path("test_terminal")
            assert result is not None
            assert "team-lead.json" in str(result)

    def test_resolve_inbox_path_returns_existing(self):
        """resolve_inbox_path returns the stored path when present."""
        from cli_agent_orchestrator.services.native_delivery_health import resolve_inbox_path

        mock_metadata = {
            "metadata": {"cc_team_inbox_path": "/home/u/.claude/inbox.json"},
            "provider": "claude_code",
            "working_directory": "/data/claude-scratch/worker-scratch/test",
        }
        with patch(
            "cli_agent_orchestrator.services.native_delivery_health.get_terminal_metadata",
            return_value=mock_metadata,
        ):
            result = resolve_inbox_path("test_terminal")
            assert result == Path("/home/u/.claude/inbox.json")


# ---------------------------------------------------------------------------
# WP-ARCH 3c K7: AC5's two arms are GONE with their subject
# ---------------------------------------------------------------------------
# ``TestAC5SupervisorNudgeExemption`` pinned an exemption INSIDE the obligation
# ladder: that ``attempt_rung2`` refused a supervisor target with
# ``supervisor_role_exempt``, and that ``_escalate`` therefore skipped rung 2 and
# fell through to the display-message floor. K7 deletes the ladder whole —
# ``attempt_rung1``/``attempt_rung2``, ``_escalate``, ``convergence_tick``,
# ``DeliveryTarget`` and ``resolve_supervisor_target`` — leaving
# ``delivery_service`` with only ``is_target_confirmed_dead``. There is no rung to
# be exempt from and no escalation to skip it.
#
# The concern the exemption served — that nothing may type into the seat's input
# box — is NOT retired with it, and is asserted more strongly than these arms did.
# K8 removes the seat's reachability of the paste seam outright rather than
# guarding it with a role check: ``InboxService.deliver_pending``'s supervisor
# branch returns before ``prepare_input`` can be reached at all, which
# ``test_p3b_seat_carrier_positions`` pins behaviourally in every switch position.
# An unreachable seam needs no exemption, so re-pointing these arms at the new
# code would mean asserting a predicate that no longer decides anything.


# ---------------------------------------------------------------------------
# AC6: F136/F276 regression tests (ordering + full drain)
# ---------------------------------------------------------------------------


class TestAC6DeliveryCorrectness:
    """AC6: F136 full drain + F276 FIFO ordering."""

    def test_deliver_pending_default_drains_all(self):
        """deliver_pending default num_messages=0 means drain all eligible."""
        import inspect

        from cli_agent_orchestrator.services.inbox_service import InboxService

        sig = inspect.signature(InboxService.deliver_pending)
        num_messages_param = sig.parameters["num_messages"]
        assert (
            num_messages_param.default == 0
        ), f"F136: deliver_pending default should be 0 (drain all), got {num_messages_param.default}"

    def test_get_pending_messages_ordered_by_id_asc(self):
        """F276: get_pending_messages orders by id ASC for strict FIFO."""
        import inspect

        from cli_agent_orchestrator.clients.database import get_pending_messages

        source = inspect.getsource(get_pending_messages)
        # Verify the ORDER BY uses id.asc() as primary sort
        assert (
            "order_by(InboxModel.id.asc())" in source
        ), "F276: get_pending_messages must order by id ASC for FIFO"

    def test_get_pending_messages_default_limit_100(self):
        """F136: get_pending_messages default limit increased from 1 to 100."""
        import inspect

        from cli_agent_orchestrator.clients.database import get_pending_messages

        sig = inspect.signature(get_pending_messages)
        limit_param = sig.parameters["limit"]
        assert (
            limit_param.default == 100
        ), f"F136: get_pending_messages default limit should be 100, got {limit_param.default}"


# ---------------------------------------------------------------------------
# AC5+W5: F337 auth line + F216 EINVAL short-circuit
# ---------------------------------------------------------------------------


class TestAC5W5NativeTierParking:
    """W5: F337 auth line + F216 EINVAL short-circuit."""

    def test_write_to_socket_empty_path_returns_socket_path_empty(self):
        """F216: empty socket path short-circuits to socket_path_empty."""
        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket

        result = write_to_socket("", "payload")
        assert result == "socket_path_empty"

    def test_write_to_socket_none_path_returns_socket_path_empty(self):
        """F216: None socket path short-circuits."""
        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket

        result = write_to_socket(None, "payload")
        assert result == "socket_path_empty"

    def test_write_to_socket_accepts_auth_token(self):
        """F337: write_to_socket accepts optional auth_token parameter."""
        import inspect

        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket

        sig = inspect.signature(write_to_socket)
        assert "auth_token" in sig.parameters
        auth_param = sig.parameters["auth_token"]
        assert auth_param.default is None

    def test_write_to_socket_einval_mapped(self):
        """F216: EINVAL (errno 22) maps to 'socket_einval'."""
        import errno
        import socket as socket_mod

        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.connect.side_effect = OSError(errno.EINVAL, "Invalid argument")

            result = write_to_socket("/tmp/nonexistent.sock", "payload")
            assert result == "socket_einval"


# ---------------------------------------------------------------------------
# WP-ARCH 3c K3b: AC1/AC2/AC3/AC7/AC8 removed with their subject
# ---------------------------------------------------------------------------
# Those arms pinned the WebSocket doorbell plane (``services/ws_doorbell.py``),
# its ``supervisor.wake.ws_monitor`` dark-ship gate, and the root-repo doctrine
# that told the seat to arm the socket. All three are deleted, the flag included
# (``test_f747_native_default.test_ws_monitor_is_not_a_setting_any_more``): the
# seat's single carrier is the server-side delivery tick
# (``app/delivery/tick.py`` -> ``services/queue_carrier.NativeSeatCarrier``).
#
# The root-repo arming doctrine those arms read — ``ws-arming.md``,
# ``ws-arming-check.sh`` and the kiro reminder hook — has since been deleted by
# the root lane, so nothing is outstanding there either.
