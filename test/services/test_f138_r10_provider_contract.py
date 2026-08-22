"""F138-R10: Provider contract tests for shell_baseline capture and reauth identity.

Verifies:
1. MockCliProvider.initialize() captures shell_baseline from the backend for all
   successfully-initialized variants (healthy, process-less, startup-delay).
2. Missing backend baseline fails for the correct reason (shell_baseline_unavailable
   when the identity-persist path is reached).
3. Spawn-then-fault behavior: faults before identity-persist, so missing baseline
   does not cause shell_baseline_unavailable.
4. Stable session identity: capture/resume/validate contract on MockCliProvider.
5. Mutant witnesses: removing shell_baseline assignment causes test failure;
   disabling supports_reauth_rebind causes path-not-traversed detection.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch, MagicMock

import pytest

from cli_agent_orchestrator.clients.database import (
    ProcessIncarnationModel,
    SessionLocal,
    init_db,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_BINARY = REPO / "test" / "providers" / "fixtures" / "bin" / "mock_cli"


@pytest.fixture(autouse=True)
def setup_db():
    """Ensure DB is initialized for every test."""
    init_db()
    yield


@pytest.fixture
def db_session():
    return SessionLocal


# ==============================================================================
# 1) shell_baseline capture — successful initialization sets it from backend
# ==============================================================================


class TestShellBaselineCapture:
    """Verify MockCliProvider captures shell_baseline during initialize() for
    all successfully-initialized variants."""

    def _make_provider(self, terminal_id="ab12cd34"):
        """Create a MockCliProvider instance with test params."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        return MockCliProvider(
            terminal_id=terminal_id,
            session_name="test-sess",
            window_name="test-win",
        )

    @pytest.mark.asyncio
    async def test_healthy_captures_shell_baseline(self, tmp_path, monkeypatch):
        """Healthy variant: initialize sets shell_baseline from get_pane_current_command."""
        provider = self._make_provider()

        # Mock wait_for_shell to succeed
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
            AsyncMock(return_value=True),
        )
        # Mock wait_until_status to succeed
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
            AsyncMock(return_value=True),
        )

        backend = Mock()
        backend.get_pane_current_command.return_value = "bash"
        backend.send_keys.return_value = None
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.get_backend", lambda: backend
        )
        # No fixture capability (legacy CI mode)
        monkeypatch.delenv("CAO_INSTANCE_ID", raising=False)

        await provider.initialize()

        assert provider.shell_baseline == "bash"
        assert provider._initialized is True

    @pytest.mark.asyncio
    async def test_processless_captures_shell_baseline(self, tmp_path, monkeypatch):
        """Process-less variant: initialize sets shell_baseline before early return."""
        from cli_agent_orchestrator.utils.provider_plane import (
            SandboxFixtureProviderCapability,
        )

        provider = self._make_provider()

        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
            AsyncMock(return_value=True),
        )

        backend = Mock()
        backend.get_pane_current_command.return_value = "zsh"
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.get_backend", lambda: backend
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        _cap = SandboxFixtureProviderCapability(
            provider="mock_cli",
            binary_realpath=FIXTURE_BINARY.resolve(),
            binary_sha256=hashlib.sha256(FIXTURE_BINARY.resolve().read_bytes()).hexdigest(),
            variant="process-less",
            state_dir=state_dir,
        )

        monkeypatch.setenv("CAO_INSTANCE_ID", "test001")
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.MockCliProvider._load_fixture_capability",
            lambda self: _cap,
        )

        await provider.initialize()

        assert provider.shell_baseline == "zsh"
        assert provider._initialized is True
        assert provider.has_process_child is False

    @pytest.mark.asyncio
    async def test_startup_delay_captures_shell_baseline(self, tmp_path, monkeypatch):
        """Startup-delay (healthy + env var) still captures shell_baseline."""
        provider = self._make_provider()

        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
            AsyncMock(return_value=True),
        )

        backend = Mock()
        backend.get_pane_current_command.return_value = "fish"
        backend.send_keys.return_value = None
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.get_backend", lambda: backend
        )
        monkeypatch.delenv("CAO_INSTANCE_ID", raising=False)
        # Set startup delay
        monkeypatch.setenv("CAO_MOCK_CLI_STARTUP_DELAY_MS", "10")

        await provider.initialize()

        assert provider.shell_baseline == "fish"
        assert provider._initialized is True

    @pytest.mark.asyncio
    async def test_backend_returns_none_no_baseline_set(self, tmp_path, monkeypatch):
        """If get_pane_current_command returns None, shell_baseline stays unset."""
        provider = self._make_provider()

        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
            AsyncMock(return_value=True),
        )

        backend = Mock()
        backend.get_pane_current_command.return_value = None
        backend.send_keys.return_value = None
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.get_backend", lambda: backend
        )
        monkeypatch.delenv("CAO_INSTANCE_ID", raising=False)

        await provider.initialize()

        assert not hasattr(provider, "shell_baseline") or provider.shell_baseline is None
        assert provider._initialized is True

    @pytest.mark.asyncio
    async def test_backend_raises_no_baseline_set(self, tmp_path, monkeypatch):
        """If get_pane_current_command raises, shell_baseline stays unset."""
        provider = self._make_provider()

        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.wait_until_status",
            AsyncMock(return_value=True),
        )

        backend = Mock()
        backend.get_pane_current_command.side_effect = RuntimeError("tmux not found")
        backend.send_keys.return_value = None
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.get_backend", lambda: backend
        )
        monkeypatch.delenv("CAO_INSTANCE_ID", raising=False)

        await provider.initialize()

        assert not hasattr(provider, "shell_baseline") or provider.shell_baseline is None
        assert provider._initialized is True


# ==============================================================================
# 2) Missing baseline → shell_baseline_unavailable at identity-persist
# ==============================================================================


class TestMissingBaselineFailure:
    """When shell_baseline is not set and supports_reauth_rebind=True,
    _prepare_provider_runtime_identity raises shell_baseline_unavailable."""

    def test_prepare_identity_raises_without_baseline(self, monkeypatch):
        """Direct call: provider with supports_reauth_rebind=True but no
        shell_baseline → RuntimeError("shell_baseline_unavailable")."""
        from cli_agent_orchestrator.services.terminal_service import (
            _prepare_provider_runtime_identity,
        )

        provider = Mock()
        provider.supports_reauth_rebind = True
        provider.shell_baseline = None  # explicitly unset
        provider.allocated_session_uuid = None
        provider.resume_session_uuid.return_value = "mock-session-test"

        terminal_id = "deadbeef"
        # Mock get_terminal_metadata
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda tid: {"tmux_session": "s", "tmux_window": "w", "shell_command": None},
        )
        # Mock backend
        mock_backend = Mock()
        mock_backend.get_pane_working_directory.return_value = "/tmp"
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_backend",
            lambda: mock_backend,
        )
        # Mock pane_pid/pane_launch_epoch
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            lambda *a, **k: 999,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_launch_epoch",
            lambda *a, **k: 1.0,
        )

        with pytest.raises(RuntimeError, match="shell_baseline_unavailable"):
            _prepare_provider_runtime_identity(
                provider, terminal_id, settlement_form="first_time"
            )

    def test_prepare_identity_succeeds_with_baseline(self, monkeypatch):
        """With shell_baseline set, _prepare_provider_runtime_identity returns
        a _PreparedRuntimeIdentity (not None)."""
        from cli_agent_orchestrator.services.terminal_service import (
            _prepare_provider_runtime_identity,
        )

        provider = Mock()
        provider.supports_reauth_rebind = True
        provider.shell_baseline = "bash"
        provider.allocated_session_uuid = None
        provider.resume_session_uuid.return_value = "mock-session-ok"

        terminal_id = "cafebabe"
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda tid: {"tmux_session": "s", "tmux_window": "w", "shell_command": None},
        )
        mock_backend = Mock()
        mock_backend.get_pane_working_directory.return_value = "/tmp"
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_backend",
            lambda: mock_backend,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            lambda *a, **k: 999,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_launch_epoch",
            lambda *a, **k: 1.0,
        )

        result = _prepare_provider_runtime_identity(
            provider, terminal_id, settlement_form="first_time"
        )
        assert result is not None
        assert result.session_uuid == "mock-session-ok"
        assert result.shell == "bash"


# ==============================================================================
# 3) Spawn-then-fault faults before identity-persist
# ==============================================================================


class TestSpawnThenFaultContract:
    """spawn-then-fault raises during initialize() BEFORE identity-persist,
    so missing shell_baseline does not cause shell_baseline_unavailable."""

    @pytest.mark.asyncio
    async def test_spawn_then_fault_raises_before_return(self, tmp_path, monkeypatch):
        """spawn-then-fault always raises RuntimeError during initialize()."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider
        from cli_agent_orchestrator.utils.provider_plane import (
            SandboxFixtureProviderCapability,
        )

        provider = MockCliProvider(
            terminal_id="ab12cd34",
            session_name="test-sess",
            window_name="test-win",
        )

        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.wait_for_shell",
            AsyncMock(return_value=True),
        )

        backend = Mock()
        # Even if baseline capture fails, the fault should fire first
        backend.get_pane_current_command.return_value = None
        backend.send_keys.return_value = None
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.get_backend", lambda: backend
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        _cap = SandboxFixtureProviderCapability(
            provider="mock_cli",
            binary_realpath=FIXTURE_BINARY.resolve(),
            binary_sha256=hashlib.sha256(FIXTURE_BINARY.resolve().read_bytes()).hexdigest(),
            variant="spawn-then-fault",
            state_dir=state_dir,
        )

        monkeypatch.setenv("CAO_INSTANCE_ID", "test001")
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.mock_cli.MockCliProvider._load_fixture_capability",
            lambda self: _cap,
        )

        with pytest.raises(RuntimeError, match="spawn-then-fault"):
            await provider.initialize()

        # Provider must NOT have _initialized set
        assert not getattr(provider, "_initialized", False)


# ==============================================================================
# 4) Stable session identity contract
# ==============================================================================


class TestSessionIdentityContract:
    """MockCliProvider session identity methods produce stable, deterministic IDs."""

    def test_capture_deterministic(self):
        """Same terminal_id → same capture_session_uuid, always."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        p = MockCliProvider("ab12cd34", "s", "w")
        uuid1 = p.capture_session_uuid(123, 1.0, "/tmp")
        uuid2 = p.capture_session_uuid(456, 2.0, "/other")
        assert uuid1 == uuid2 == "mock-session-ab12cd34"

    def test_resume_matches_capture(self):
        """resume_session_uuid matches capture for same provider instance."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        p = MockCliProvider("deadbeef", "s", "w")
        captured = p.capture_session_uuid(1, 1.0, "/x")
        resumed = p.resume_session_uuid()
        assert captured == resumed

    def test_validate_accepts_correct(self):
        """validate_session_artifact passes for matching UUID."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        p = MockCliProvider("cafebabe", "s", "w")
        uuid_val = p.capture_session_uuid(1, 1.0, "/x")
        # Should not raise
        p.validate_session_artifact(uuid_val, "/x")

    def test_validate_rejects_mismatch(self):
        """validate_session_artifact raises for non-matching UUID."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        p = MockCliProvider("cafebabe", "s", "w")
        with pytest.raises(ValueError, match="artifact mismatch"):
            p.validate_session_artifact("wrong-uuid", "/x")

    def test_supports_reauth_rebind_true(self):
        """MockCliProvider advertises supports_reauth_rebind=True."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        assert MockCliProvider.supports_reauth_rebind is True
        p = MockCliProvider("ab12cd34", "s", "w")
        assert p.supports_reauth_rebind is True
