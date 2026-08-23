"""F360 (issue #215) — failed session-create must not leave a ghost terminal.

Two layers:
1. ``session_service.create_session`` unwinds the terminal registration (DB row
   via ``delete_terminal`` + StatusMonitor state via ``unregister``) on ANY
   exception raised after ``create_terminal`` allocated and registered the id.
2. ``StatusMonitor._process_chunk`` drops a terminal id from its watch state
   with one warning after the third consecutive "not found in database" miss
   (``GHOST_DROP_MISSES``) from ``provider_manager.get_provider`` instead of
   erroring forever.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.services import session_service
from cli_agent_orchestrator.services.session_service import create_session
from cli_agent_orchestrator.services.status_monitor import GHOST_DROP_MISSES, StatusMonitor


def _terminal_double():
    return MagicMock(id="f3600001", session_name="cao-f360")


class TestCreateSessionUnwind:
    """A failed create leaves no terminal row / monitor entry (issue #215)."""

    @pytest.mark.asyncio
    async def test_post_create_failure_unwinds_registration(self):
        """Failure after create_terminal deregisters the terminal + monitor."""
        terminal = _terminal_double()
        with (
            patch.object(session_service, "create_terminal", AsyncMock(return_value=terminal)),
            patch.object(
                session_service,
                "load_agent_profile",
                MagicMock(return_value=MagicMock(role="worker")),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
                AsyncMock(return_value=None),
            ),
            patch.object(
                session_service,
                "dispatch_plugin_event",
                MagicMock(side_effect=RuntimeError("post_create_session blew up")),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.delete_terminal"
            ) as mock_delete,
            patch(
                "cli_agent_orchestrator.services.status_monitor.status_monitor"
            ) as mock_monitor,
        ):
            with pytest.raises(RuntimeError, match="post_create_session blew up"):
                await create_session(provider="kiro_cli", agent_profile="developer")

            # DB row + full teardown went through delete_terminal...
            mock_delete.assert_called_once_with("f3600001", None)
            # ...and the monitor watch state was removed.
            mock_monitor.unregister.assert_called_once_with("f3600001")

    @pytest.mark.asyncio
    async def test_successful_create_does_not_unwind(self):
        """Success path never deregisters the terminal."""
        terminal = _terminal_double()
        with (
            patch.object(session_service, "create_terminal", AsyncMock(return_value=terminal)),
            patch.object(
                session_service,
                "load_agent_profile",
                MagicMock(return_value=MagicMock(role="worker")),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
                AsyncMock(return_value=None),
            ),
            patch.object(session_service, "dispatch_plugin_event", MagicMock()),
            patch(
                "cli_agent_orchestrator.services.terminal_service.delete_terminal"
            ) as mock_delete,
            patch(
                "cli_agent_orchestrator.services.status_monitor.status_monitor"
            ) as mock_monitor,
        ):
            result = await create_session(provider="kiro_cli", agent_profile="developer")

        assert result is terminal
        mock_delete.assert_not_called()
        mock_monitor.unregister.assert_not_called()


    @pytest.mark.asyncio
    async def test_publication_failure_still_unwinds_monitor(self):
        """Supervisor publication failure also removes the monitor entry."""
        terminal = _terminal_double()
        with (
            patch.object(session_service, "create_terminal", AsyncMock(return_value=terminal)),
            patch.object(
                session_service,
                "load_agent_profile",
                MagicMock(return_value=MagicMock(role="supervisor")),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
                AsyncMock(return_value=None),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.claim_mailbox"
            ) as mock_claim,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.publish_supervisor_incarnation",
                MagicMock(side_effect=RuntimeError("provider check failed mid-create")),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.delete_terminal",
                MagicMock(return_value={"deleted": True}),
            ),
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata",
                MagicMock(return_value=None),
            ),
            patch(
                "cli_agent_orchestrator.services.status_monitor.status_monitor"
            ) as mock_monitor,
        ):
            mock_claim.return_value = MagicMock(session_name="cao-f360", role="supervisor")
            with pytest.raises(RuntimeError, match="provider check failed mid-create"):
                await create_session(provider="kiro_cli", agent_profile="code_supervisor")

            mock_monitor.unregister.assert_called_once_with("f3600001")


def _not_found(terminal_id):
    return ValueError(f"Terminal {terminal_id} not found in database")


class TestStatusMonitorGhostDrop:
    """Monitor drops a ghost id after GHOST_DROP_MISSES misses (issue #215)."""

    @pytest.mark.parametrize("misses", [1, GHOST_DROP_MISSES - 1])
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_first_misses_tolerated_not_dropped(self, mock_pm, misses):
        sm = StatusMonitor()
        mock_pm.get_provider.side_effect = _not_found("ghost0001")

        for i in range(misses):
            sm._process_chunk("ghost0001", f"chunk{i}")

        assert "ghost0001" not in sm._dropped_not_found
        assert sm._provider_not_found_count["ghost0001"] == misses

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_third_not_found_drops_ghost_with_one_warning(self, mock_pm, caplog):
        sm = StatusMonitor()
        tid = "ghost0002"
        mock_pm.get_provider.side_effect = _not_found(tid)
        caplog.set_level("WARNING", logger="cli_agent_orchestrator.services.status_monitor")

        sm._process_chunk(tid, "chunk1")
        with sm._lock:
            sm._buffers[tid] = "stale ghost buffer"
        sm._process_chunk(tid, "chunk2")  # 2 misses: still watching (commit
        # window under lock contention — F360 diff gate SHOULD finding).
        assert tid not in sm._dropped_not_found
        sm._process_chunk(tid, "chunk3")  # 3rd miss: drop.
        # Further chunks are ignored silently — no more errors, no more warnings.
        sm._process_chunk(tid, "chunk4")
        sm._process_chunk(tid, "chunk5")

        assert tid in sm._dropped_not_found
        with sm._lock:
            assert tid not in sm._buffers  # watch state cleared
        warnings = [r for r in caplog.records if "dropping ghost terminal" in r.message]
        assert len(warnings) == 1

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_unrelated_value_error_still_raises(self, mock_pm):
        sm = StatusMonitor()
        mock_pm.get_provider.side_effect = ValueError("some other provider error")

        with pytest.raises(ValueError, match="some other provider error"):
            sm._process_chunk("t1", "chunk")

    @patch("cli_agent_orchestrator.services.status_monitor.get_server_settings")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    def test_provider_recovery_resets_miss_counter(self, mock_pm, mock_settings):
        sm = StatusMonitor()
        tid = "ghost0003"
        mock_settings.return_value = {"state_buffer_max": 4096}
        provider = MagicMock()
        provider.supports_screen_detection = False
        mock_pm.get_provider.side_effect = [_not_found(tid), provider]

        with patch.object(sm, "_schedule_raw_detection"):
            sm._process_chunk(tid, "chunk1")  # miss
            sm._process_chunk(tid, "chunk2")  # provider appears (creation finished)

        assert tid not in sm._dropped_not_found
        assert tid not in sm._provider_not_found_count
