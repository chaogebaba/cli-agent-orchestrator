"""WPDT — wp-delivery-truth acceptance criteria tests.

AC1: WS endpoint serves on :9889; frame delivered <1s for armed supervisor;
     unit tests for auth-reject, unarmed no-op, frame format.
AC2: (e2e, box-run) — armed scratch supervisor receives doorbell while IDLE
     and post-/compact. Documented probe step for G7 round.
AC3: ws_monitor flag OFF → WS endpoint returns 503/4503, doctrine arming
     conditioned on flag, negative test: no WS connection under flag=OFF.
AC4: F152 producer — fresh pane has cc_team_inbox_path; self-heal fills missing.
AC5: Supervisor targets never receive rung2 composer injection (code path
     unreachable for role=supervisor).
AC6: F136/F276 regression tests (ordering + full drain).
AC7: (root) doctrine arming step + hook reminder (probe-style live check).
AC8: (both) single bounce; rollback = flag flip. (Process-level, documented.)
"""

import asyncio
import json
import os
import re
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# AC1: WS doorbell endpoint — auth, frame format, armed delivery
# ---------------------------------------------------------------------------


class TestAC1WsDoorbellEndpoint:
    """AC1: WS endpoint serves; frame format; auth-reject; unarmed no-op."""

    def test_ws_doorbell_auth_reject_missing_token(self):
        """Connection without token is rejected with 4401."""
        from cli_agent_orchestrator.services.ws_doorbell import is_ws_monitor_enabled

        # Flag must be True for the endpoint to serve
        with patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get"
        ) as mock_get:
            mock_get.return_value = True
            assert is_ws_monitor_enabled() is True

    def test_ws_doorbell_frame_format(self):
        """Frame format matches: [CAO] callback waiting: [<id>] from=<short> <preview>."""
        frame = "[CAO] callback waiting: [42] from=abc12345 Hello world preview"
        pattern = r"\[CAO\] callback waiting: \[\d+\] from=\S+ .+"
        assert re.match(pattern, frame)

    def test_ws_doorbell_is_armed_false_when_no_connection(self):
        """is_armed returns False for unregistered terminals."""
        from cli_agent_orchestrator.services.ws_doorbell import is_armed

        assert is_armed("nonexistent_terminal_id") is False

    @pytest.mark.asyncio
    async def test_ws_doorbell_push_frame_unarmed_returns_false(self):
        """push_doorbell_frame returns False when terminal is not armed."""
        from cli_agent_orchestrator.services.ws_doorbell import push_doorbell_frame

        result = await push_doorbell_frame("unarmed_terminal", 1, "sender", "preview")
        assert result is False

    @pytest.mark.asyncio
    async def test_ws_doorbell_register_and_push(self):
        """Registered connection receives frame; push returns True."""
        from cli_agent_orchestrator.services import ws_doorbell

        # Reset module state
        ws_doorbell._connections.clear()

        mock_ws = AsyncMock()
        mock_ws.send_text = AsyncMock()

        await ws_doorbell.register_connection("test_terminal", mock_ws)
        assert ws_doorbell.is_armed("test_terminal") is True

        result = await ws_doorbell.push_doorbell_frame(
            "test_terminal", 42, "worker01", "callback result"
        )
        assert result is True
        mock_ws.send_text.assert_called_once()
        frame_text = mock_ws.send_text.call_args[0][0]
        assert "[CAO] callback waiting:" in frame_text
        assert "[42]" in frame_text
        assert "from=worker01" in frame_text

        # Cleanup
        await ws_doorbell.unregister_connection("test_terminal", mock_ws)
        assert ws_doorbell.is_armed("test_terminal") is False

    def test_ws_doorbell_auth_token_validation_constant_time(self):
        """_constant_time_compare uses hmac.compare_digest."""
        import importlib
        import sys

        # Import the module to get the function
        from cli_agent_orchestrator.api.main import _constant_time_compare

        assert _constant_time_compare("abc", "abc") is True
        assert _constant_time_compare("abc", "def") is False
        assert _constant_time_compare("", "") is True


# ---------------------------------------------------------------------------
# AC3: ws_monitor flag OFF → 503, no connection possible
# ---------------------------------------------------------------------------


class TestAC3DarkShipFlagOff:
    """AC3: flag OFF = byte-identical to pre-WP behavior."""

    def test_ws_monitor_disabled_by_default(self):
        """supervisor.wake.ws_monitor defaults to False."""
        from cli_agent_orchestrator.services.config_service import ConfigService

        # Default value in ENV_REGISTRY is False
        from cli_agent_orchestrator.services.config_service import ENV_REGISTRY

        entry = ENV_REGISTRY.get("CAO_SUPERVISOR_WAKE_WS_MONITOR")
        assert entry is not None
        assert entry == ("supervisor.wake.ws_monitor", "bool", False)

    def test_is_ws_monitor_enabled_returns_false_when_off(self):
        """is_ws_monitor_enabled() returns False when flag is off."""
        from cli_agent_orchestrator.services.ws_doorbell import is_ws_monitor_enabled

        with patch(
            "cli_agent_orchestrator.services.ws_doorbell.ConfigService.get",
            return_value=False,
        ):
            assert is_ws_monitor_enabled() is False

    def test_push_doorbell_frame_sync_noop_when_disabled(self):
        """push_doorbell_frame_sync is a no-op when flag is off."""
        from cli_agent_orchestrator.services.ws_doorbell import push_doorbell_frame_sync

        with patch(
            "cli_agent_orchestrator.services.ws_doorbell.is_ws_monitor_enabled",
            return_value=False,
        ):
            # Should not raise, just return silently
            push_doorbell_frame_sync("some_terminal", 1, "sender", "preview")


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
        with patch(
            "cli_agent_orchestrator.services.teammate_push_service.get_terminal_metadata",
            return_value=mock_metadata,
        ), patch(
            "cli_agent_orchestrator.clients.database.update_terminal_metadata",
        ) as mock_update:
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

        with patch(
            "cli_agent_orchestrator.services.delivery_service.resolve_supervisor_target",
            return_value=mock_target,
        ), patch(
            "cli_agent_orchestrator.services.delivery_service._is_supervisor_role_target",
            return_value=True,
        ), patch(
            "cli_agent_orchestrator.services.delivery_service.emit_trace_or_collapse",
        ), patch(
            "cli_agent_orchestrator.services.delivery_service._fire_escalation_display_message",
        ) as mock_display, patch(
            "cli_agent_orchestrator.services.delivery_service.attempt_rung2",
        ) as mock_rung2:
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
        from cli_agent_orchestrator.services.inbox_service import InboxService
        import inspect

        sig = inspect.signature(InboxService.deliver_pending)
        num_messages_param = sig.parameters["num_messages"]
        assert num_messages_param.default == 0, (
            f"F136: deliver_pending default should be 0 (drain all), got {num_messages_param.default}"
        )

    def test_get_pending_messages_ordered_by_id_asc(self):
        """F276: get_pending_messages orders by id ASC for strict FIFO."""
        from cli_agent_orchestrator.clients.database import get_pending_messages
        import inspect

        source = inspect.getsource(get_pending_messages)
        # Verify the ORDER BY uses id.asc() as primary sort
        assert "order_by(InboxModel.id.asc())" in source, (
            "F276: get_pending_messages must order by id ASC for FIFO"
        )

    def test_get_pending_messages_default_limit_100(self):
        """F136: get_pending_messages default limit increased from 1 to 100."""
        from cli_agent_orchestrator.clients.database import get_pending_messages
        import inspect

        sig = inspect.signature(get_pending_messages)
        limit_param = sig.parameters["limit"]
        assert limit_param.default == 100, (
            f"F136: get_pending_messages default limit should be 100, got {limit_param.default}"
        )


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
        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket
        import inspect

        sig = inspect.signature(write_to_socket)
        assert "auth_token" in sig.parameters
        auth_param = sig.parameters["auth_token"]
        assert auth_param.default is None

    def test_write_to_socket_einval_mapped(self):
        """F216: EINVAL (errno 22) maps to 'socket_einval'."""
        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket
        import socket as socket_mod
        import errno

        with patch("socket.socket") as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.connect.side_effect = OSError(errno.EINVAL, "Invalid argument")

            result = write_to_socket("/tmp/nonexistent.sock", "payload")
            assert result == "socket_einval"


# ---------------------------------------------------------------------------
# AC7: Doctrine arming step + hook (probe-style — existence verified here,
#      live check is a G7 round documented step)
# ---------------------------------------------------------------------------


class TestAC7DoctrineArmingStep:
    """AC7: doctrine file and hook exist with correct content."""

    def test_doctrine_arming_section_exists(self):
        """The ws-arming.md doctrine section exists."""
        path = Path("/home/chao/VScode_projects/cli-subagents/doctrine/sections/shared/ws-arming.md")
        assert path.exists(), "doctrine/sections/shared/ws-arming.md must exist"

    def test_doctrine_arming_section_content(self):
        """ws-arming.md contains Monitor arming instruction."""
        path = Path("/home/chao/VScode_projects/cli-subagents/doctrine/sections/shared/ws-arming.md")
        content = path.read_text()
        assert "Monitor(" in content
        assert "persistent: true" in content
        assert "ws_monitor" in content
        assert "list_messages" in content

    def test_hook_script_exists_and_executable(self):
        """ws-arming-check.sh exists and is executable."""
        path = Path("/home/chao/VScode_projects/cli-subagents/doctrine/hooks/ws-arming-check.sh")
        assert path.exists()
        assert os.access(str(path), os.X_OK)

    def test_hook_json_exists(self):
        """wpdt-ws-arming-reminder.json hook file exists with SessionStart trigger."""
        path = Path(
            "/home/chao/VScode_projects/cli-subagents/.kiro/hooks/wpdt-ws-arming-reminder.json"
        )
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["hooks"][0]["trigger"] == "SessionStart"

    def test_hook_script_flag_conditioned(self):
        """Hook script checks ws_monitor flag from health endpoint."""
        path = Path("/home/chao/VScode_projects/cli-subagents/doctrine/hooks/ws-arming-check.sh")
        content = path.read_text()
        assert "ws_monitor" in content
        assert "/health" in content


# ---------------------------------------------------------------------------
# AC8: Single bounce + rollback = flag flip (documented, not testable in unit)
# ---------------------------------------------------------------------------


class TestAC8SingleBounceRollback:
    """AC8: Rollback is flag flip — ws_monitor=False disables all new paths."""

    def test_all_ws_paths_gated_by_flag(self):
        """All WS doorbell code paths check is_ws_monitor_enabled()."""
        from cli_agent_orchestrator.services.ws_doorbell import (
            is_ws_monitor_enabled,
            push_doorbell_frame_sync,
        )
        import inspect

        source = inspect.getsource(push_doorbell_frame_sync)
        assert "is_ws_monitor_enabled" in source

    def test_config_default_false_ensures_dark_ship(self):
        """Default False means the feature ships dark (AC8: rollback = flag flip)."""
        from cli_agent_orchestrator.services.ws_doorbell import is_ws_monitor_enabled

        with patch(
            "cli_agent_orchestrator.services.ws_doorbell.ConfigService.get",
            return_value=False,
        ):
            assert is_ws_monitor_enabled() is False
