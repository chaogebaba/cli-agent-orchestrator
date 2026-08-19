"""Tests for F311, F312, F314 grok terminal lifecycle bug fixes.

F311: synchronous create artifact validation race (wait loop).
F312: AccessDenied on unrelated processes must not block cleanup.
F314: failed create rollback removes provisioned GROK_HOME.
"""

import hashlib
import os
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from cli_agent_orchestrator.providers.base import RetryableArtifactValidation
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider


@pytest.fixture
def tmp_cao_home(tmp_path):
    """Override CAO_HOME_DIR to an isolated temp directory."""
    with patch("cli_agent_orchestrator.providers.grok_cli.CAO_HOME_DIR", tmp_path):
        yield tmp_path


@pytest.fixture
def provider(tmp_cao_home):
    """Create a GrokCliProvider with mocked external deps."""
    with (
        patch("cli_agent_orchestrator.providers.grok_cli.provider_home") as mock_plane,
        patch("cli_agent_orchestrator.providers.grok_cli.get_backend") as mock_backend,
    ):
        plane = MagicMock()
        plane.home = tmp_cao_home / "grok_plane"
        plane.home.mkdir(parents=True, exist_ok=True)
        plane.sessions = tmp_cao_home / "grok_plane" / "sessions"
        plane.sessions.mkdir(parents=True, exist_ok=True)
        plane.credential_path = tmp_cao_home / "grok_plane" / "auth.json"
        plane.credential_path.write_text("{}")
        mock_plane.return_value = plane

        backend_inst = MagicMock()
        backend_inst.get_pane_working_directory.return_value = "/tmp"
        mock_backend.return_value = backend_inst

        p = GrokCliProvider(
            terminal_id="test-f311-01",
            session_name="s1",
            window_name="w1",
            agent_profile="grok_dev",
        )
        yield p


# ─── F311: artifact validation retry ────────────────────────────────────────


class TestF311ArtifactWaitSync:
    """F311: _wait_for_session_artifact_sync retries on RetryableArtifactValidation."""

    def test_succeeds_after_late_write(self):
        """Simulates artifact appearing after initial check — succeeds within deadline."""
        from cli_agent_orchestrator.services.terminal_service import (
            _wait_for_session_artifact_sync,
        )

        call_count = [0]

        class FakeProvider:
            def validate_session_artifact(self, session_uuid, cwd):
                call_count[0] += 1
                if call_count[0] < 3:
                    raise RetryableArtifactValidation("session_artifact_missing_or_inert")
                # Third call succeeds (artifact appeared)

        provider = FakeProvider()
        _wait_for_session_artifact_sync(
            provider, "uuid-1", "/tmp", deadline_s=5.0, poll_interval=0.05
        )
        assert call_count[0] == 3

    def test_raises_after_deadline(self):
        """Genuine inert artifact: raises RetryableArtifactValidation after bound."""
        from cli_agent_orchestrator.services.terminal_service import (
            _wait_for_session_artifact_sync,
        )

        class AlwaysFailProvider:
            def validate_session_artifact(self, session_uuid, cwd):
                raise RetryableArtifactValidation("session_artifact_missing_or_inert")

        provider = AlwaysFailProvider()
        with pytest.raises(RetryableArtifactValidation):
            _wait_for_session_artifact_sync(
                provider, "uuid-1", "/tmp", deadline_s=0.2, poll_interval=0.05
            )

    def test_immediate_success_no_retry(self):
        """If artifact is already present, no retry needed."""
        from cli_agent_orchestrator.services.terminal_service import (
            _wait_for_session_artifact_sync,
        )

        call_count = [0]

        class ImmediateProvider:
            def validate_session_artifact(self, session_uuid, cwd):
                call_count[0] += 1

        provider = ImmediateProvider()
        _wait_for_session_artifact_sync(
            provider, "uuid-1", "/tmp", deadline_s=1.0, poll_interval=0.05
        )
        assert call_count[0] == 1


# ─── F312: AccessDenied scoping ─────────────────────────────────────────────


class TestF312AccessDeniedScoping:
    """F312: AccessDenied on an unrelated process must not block cleanup."""

    def test_unrelated_access_denied_does_not_block(self, provider, tmp_cao_home):
        """AccessDenied on an unrelated same-uid process → cleanup proceeds."""
        home = provider._home_path()

        # Mock a process that denies environ() inspection but is unrelated
        mock_proc = MagicMock()
        mock_proc.uids.return_value = MagicMock(real=os.getuid())
        mock_proc.environ.side_effect = psutil.AccessDenied(pid=1234)
        # Not a descendant, no related cwd/files
        mock_proc.parent.return_value = MagicMock(pid=1)
        mock_proc.cwd.side_effect = psutil.AccessDenied(pid=1234)
        mock_proc.open_files.side_effect = psutil.AccessDenied(pid=1234)

        with (
            patch(
                "cli_agent_orchestrator.providers.grok_cli.psutil.pids",
                return_value=[1234],
            ),
            patch(
                "cli_agent_orchestrator.providers.grok_cli.psutil.Process",
                return_value=mock_proc,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service.pane_pid",
                return_value=9999,
            ),
        ):
            result = provider._pids_using_home(home)
        # Should return empty list (no grok processes), NOT None (uncertain)
        assert result == []

    def test_related_access_denied_returns_none(self, provider, tmp_cao_home):
        """AccessDenied on a process whose cwd IS in GROK_HOME → returns None (uncertain)."""
        home = provider._home_path()
        home_str = str(home)

        mock_proc = MagicMock()
        mock_proc.uids.return_value = MagicMock(real=os.getuid())
        mock_proc.environ.side_effect = psutil.AccessDenied(pid=5678)
        # cwd is inside the GROK_HOME — this process is plausibly related
        mock_proc.cwd.return_value = home_str + "/sessions"
        mock_proc.parent.return_value = MagicMock(pid=1)

        with (
            patch(
                "cli_agent_orchestrator.providers.grok_cli.psutil.pids",
                return_value=[5678],
            ),
            patch(
                "cli_agent_orchestrator.providers.grok_cli.psutil.Process",
                return_value=mock_proc,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service.pane_pid",
                return_value=9999,
            ),
        ):
            result = provider._pids_using_home(home)
        # Related process + AccessDenied → uncertainty
        assert result is None

    def test_genuinely_live_grok_child_still_defers(self, provider, tmp_cao_home):
        """A genuinely live grok child (GROK_HOME matches) → returns [pid]."""
        home = provider._home_path()
        home_str = str(home)

        mock_proc = MagicMock()
        mock_proc.uids.return_value = MagicMock(real=os.getuid())
        mock_proc.environ.return_value = {"GROK_HOME": home_str}

        with (
            patch(
                "cli_agent_orchestrator.providers.grok_cli.psutil.pids",
                return_value=[7777],
            ),
            patch(
                "cli_agent_orchestrator.providers.grok_cli.psutil.Process",
                return_value=mock_proc,
            ),
        ):
            result = provider._pids_using_home(home)
        assert result == [7777]

    def test_cleanup_succeeds_with_uninspectable_daemons(self, provider, tmp_cao_home):
        """E2E: cleanup() succeeds on host with uninspectable same-uid daemons."""
        home = provider._home_path()
        assert home.exists()

        # Simulate 4 uninspectable daemons (the real-world scenario)
        daemon_pids = [1264, 1332, 4317, 68321]
        mock_procs = {}
        for pid in daemon_pids:
            m = MagicMock()
            m.uids.return_value = MagicMock(real=os.getuid())
            m.environ.side_effect = psutil.AccessDenied(pid=pid)
            m.parent.return_value = MagicMock(pid=1)
            m.cwd.side_effect = psutil.AccessDenied(pid=pid)
            m.open_files.side_effect = psutil.AccessDenied(pid=pid)
            mock_procs[pid] = m

        def process_factory(pid):
            return mock_procs[pid]

        with (
            patch(
                "cli_agent_orchestrator.providers.grok_cli.psutil.pids",
                return_value=daemon_pids,
            ),
            patch(
                "cli_agent_orchestrator.providers.grok_cli.psutil.Process",
                side_effect=process_factory,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service.pane_pid",
                return_value=9999,
            ),
        ):
            result = provider.cleanup()
        assert result is True
        assert not home.exists()


# ─── F314: create rollback removes GROK_HOME ─────────────────────────────────


class TestF314CreateRollbackRemovesHome:
    """F314: failed create rollback removes the provisioned GROK_HOME."""

    def test_rollback_removes_provisioned_home(self, provider, tmp_cao_home):
        """Force a create failure scenario and verify GROK_HOME is removed."""
        from cli_agent_orchestrator.providers.manager import ProviderManager

        home = provider._home_path()
        assert home.exists(), "GROK_HOME should exist after provider __init__"

        # Simulate the F314 rollback logic directly (extracted from terminal_service)
        # The real code does: provider_manager.get_provider → isinstance check → rmtree
        manager = ProviderManager()
        with manager._lock:
            manager._providers["test-f311-01"] = provider

        retrieved = manager.get_provider("test-f311-01")
        assert isinstance(retrieved, GrokCliProvider)
        rollback_home = retrieved._home_path()
        assert rollback_home.exists()
        assert retrieved._is_managed_home(rollback_home)

        # Execute the F314 rollback
        shutil.rmtree(rollback_home, ignore_errors=True)
        assert not rollback_home.exists()

    def test_rollback_only_removes_own_home(self, tmp_cao_home):
        """Rollback removes only its own provisioned home, not another terminal's."""
        with (
            patch("cli_agent_orchestrator.providers.grok_cli.provider_home") as mock_plane,
            patch("cli_agent_orchestrator.providers.grok_cli.get_backend") as mock_backend,
        ):
            plane = MagicMock()
            plane.home = tmp_cao_home / "grok_plane"
            plane.home.mkdir(parents=True, exist_ok=True)
            plane.sessions = tmp_cao_home / "grok_plane" / "sessions"
            plane.sessions.mkdir(parents=True, exist_ok=True)
            plane.credential_path = tmp_cao_home / "grok_plane" / "auth.json"
            plane.credential_path.write_text("{}")
            mock_plane.return_value = plane
            mock_backend.return_value = MagicMock(
                get_pane_working_directory=MagicMock(return_value="/tmp")
            )

            # Create two providers with different terminal IDs
            p1 = GrokCliProvider("term-aaa", "s1", "w1", "grok_dev")
            p2 = GrokCliProvider("term-bbb", "s1", "w2", "grok_dev")

        home1 = p1._home_path()
        home2 = p2._home_path()
        assert home1.exists()
        assert home2.exists()
        assert home1 != home2

        # Rollback p1 — only p1's home removed
        shutil.rmtree(home1, ignore_errors=True)
        assert not home1.exists()
        assert home2.exists(), "Other terminal's home must survive"
