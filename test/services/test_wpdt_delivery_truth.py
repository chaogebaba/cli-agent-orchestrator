"""WPDT — wp-delivery-truth acceptance criteria tests.

AC1/AC2/AC3: the WebSocket doorbell plane — deleted by WP-ARCH 3c K3b; see the
     note at the foot of this file.
AC4: F152 producer — fresh pane has cc_team_inbox_path; self-heal fills missing.
AC5: Supervisor targets never receive rung2 composer injection (code path
     unreachable for role=supervisor).
AC6: F136/F276 regression tests (ordering + full drain).
AC7/AC8: doctrine arming + flag-flip rollback for that same deleted plane; see
     the note at the foot of this file.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# AC4: F152 producer — cc_team_inbox_path at creation + self-heal
# ---------------------------------------------------------------------------


class TestAC4F152Producer:
    """AC4: fresh pane has cc_team_inbox_path; self-heal fills missing."""

    def test_derive_cc_team_inbox_path_returns_valid_path(self):
        """_derive_cc_team_inbox_path builds ~/.claude/projects/{key}/team-lead.json."""
        from cli_agent_orchestrator.services.teammate_push_service import (
            _derive_cc_team_inbox_path,
        )

        result = _derive_cc_team_inbox_path("/home/user/project")
        assert result is not None
        assert "team-lead.json" in str(result)
        assert ".claude/projects/" in str(result)
        # The cwd_key replaces non-alphanumeric with -
        assert "-home-user-project" in str(result)

    def test_resolve_inbox_path_self_heal(self):
        """_resolve_inbox_path derives path when metadata is missing."""
        from cli_agent_orchestrator.services.teammate_push_service import _resolve_inbox_path

        mock_metadata = {
            "metadata": {},
            "provider": "claude_code",
            "working_directory": "/tmp/test_project",
        }
        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
                return_value=mock_metadata,
            ),
            patch(
                "cli_agent_orchestrator.clients.database.update_terminal_metadata",
            ) as mock_update,
        ):
            result = _resolve_inbox_path("test_terminal")
            assert result is not None
            assert "team-lead.json" in str(result)

    def test_resolve_inbox_path_returns_existing(self):
        """_resolve_inbox_path returns the stored path when present."""
        from cli_agent_orchestrator.services.teammate_push_service import _resolve_inbox_path

        mock_metadata = {
            "metadata": {"cc_team_inbox_path": "/home/u/.claude/inbox.json"},
            "provider": "claude_code",
            "working_directory": "/tmp/test",
        }
        with patch(
            "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
            return_value=mock_metadata,
        ):
            result = _resolve_inbox_path("test_terminal")
            assert result == Path("/home/u/.claude/inbox.json")


# ---------------------------------------------------------------------------
# AC5: Supervisor targets never receive rung2 composer injection
# ---------------------------------------------------------------------------


class TestAC5SupervisorNudgeExemption:
    """AC5: supervisor role targets are exempt from rung2 AND escalation rung2."""

    def test_attempt_rung2_supervisor_role_exempt(self):
        """attempt_rung2 returns supervisor_role_exempt for supervisor targets."""
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung2,
        )

        target = DeliveryTarget(
            terminal_id="sup_term",
            tmux_session="cao-session",
            tmux_window="sup_window",
            cc_inbox_path=None,
            liveness="presumed_live",
        )

        with patch(
            "cli_agent_orchestrator.services.delivery_service._is_supervisor_role_target",
            return_value=True,
        ):
            result = attempt_rung2(target, 100)
            assert result.delivered is False
            assert result.reason == "supervisor_role_exempt"

    def test_escalate_skips_rung2_for_supervisor(self):
        """_escalate does not call attempt_rung2 for supervisor targets."""
        from cli_agent_orchestrator.services.delivery_service import _escalate

        # Mock all dependencies
        mock_obl = MagicMock()
        mock_obl.inbox_row_id = 1
        mock_obl.mailbox_id = "mb1"
        mock_obl.attempts = 5
        mock_obl.accepted_at = None

        mock_target = MagicMock()
        mock_target.terminal_id = "sup_term"
        mock_target.tmux_session = "cao-session"
        mock_target.tmux_window = "sup_window"
        mock_target.cc_inbox_path = None
        mock_target.liveness = "presumed_live"

        mock_db = MagicMock()
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)

        with (
            patch(
                "cli_agent_orchestrator.services.delivery_service.resolve_supervisor_target",
                return_value=mock_target,
            ),
            patch(
                "cli_agent_orchestrator.services.delivery_service._is_supervisor_role_target",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.delivery_service.emit_trace_or_collapse",
            ),
            patch(
                "cli_agent_orchestrator.services.delivery_service._fire_escalation_display_message",
            ) as mock_display,
            patch(
                "cli_agent_orchestrator.services.delivery_service.attempt_rung2",
            ) as mock_rung2,
        ):
            _escalate(mock_db, mock_obl, now, 200.0)

            # rung2 should NOT be called for supervisor targets
            mock_rung2.assert_not_called()
            # display-message floor SHOULD fire
            mock_display.assert_called_once()
            # Obligation should be ESCALATED
            assert mock_obl.state == "ESCALATED"
            assert mock_obl.terminal_reason == "supervisor_role_exempt"


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
# seat's single
# carrier is the server-side delivery tick (``app/delivery/tick.py`` ->
# ``services/queue_carrier.NativeSeatCarrier``). The root repo still carries the
# now-dead ``doctrine/sections/shared/ws-arming.md``,
# ``doctrine/hooks/ws-arming-check.sh`` and
# ``.kiro/hooks/wpdt-ws-arming-reminder.json`` — root-repo cleanup, not this suite.
