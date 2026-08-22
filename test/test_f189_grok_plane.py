"""Test for F189: grok sessions routed through the provider plane.

Verifies:
1. fork_context_service reads grok sessions via provider_home, not Path.home().
2. provider_plane supports 'grok_cli' plane.
3. The g7b guard scan covers .grok literals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.utils.provider_plane import ProviderHome


class TestF189GrokPlaneRouting:
    """fork_context_service grok reads must go through the provider plane."""

    def test_grok_artifact_mismatch_uses_plane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_grok_artifact_mismatch must read from provider_home, not Path.home()."""
        from cli_agent_orchestrator.services import fork_context_service

        fake_grok_home = tmp_path / "grok-plane"
        sessions = fake_grok_home / "sessions"
        sessions.mkdir(parents=True)

        plane = ProviderHome(
            provider="grok_cli",
            classification="production",
            home=fake_grok_home,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.provider_home",
            lambda p: plane if p == "grok_cli" else None,
        )

        # No artifacts present — should return None (no mismatch detectable)
        result = fork_context_service._grok_artifact_mismatch("fake-uuid", "/some/cwd")
        assert result is None

    def test_validate_base_source_grok_compatibility_uses_plane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """validate_base_source in compatibility mode reads grok through the plane."""
        from cli_agent_orchestrator.services.fork_context_service import (
            ForkContextError,
            validate_base_source,
        )

        fake_grok_home = tmp_path / "grok-plane"
        sessions = fake_grok_home / "sessions"
        sessions.mkdir(parents=True)

        plane = ProviderHome(
            provider="grok_cli",
            classification="production",
            home=fake_grok_home,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.provider_home",
            lambda p: plane if p == "grok_cli" else None,
        )

        # Session doesn't exist — should raise
        with pytest.raises(ForkContextError, match="session_file_missing"):
            validate_base_source(
                mode="compatibility",
                provider="grok_cli",
                session_uuid="test-uuid",
                cwd="/test/cwd",
            )

    def test_grok_plane_in_provider_plane_module(self) -> None:
        """provider_home supports 'grok_cli' in production mode."""
        from cli_agent_orchestrator.utils.provider_plane import provider_home

        # In non-sandbox mode (no CAO_INSTANCE_ID), returns production home
        plane = provider_home("grok_cli")
        assert plane.home == Path.home() / ".grok"
        assert plane.provider == "grok_cli"

    def test_guard_catches_grok_literals(self) -> None:
        """The g7b guard scan includes .grok in its literal check."""
        from test.test_g7b_sandbox import (
            test_native_home_guard_and_every_roster_consumer_is_injected,
        )

        # If this doesn't raise, the guard is correctly configured
        test_native_home_guard_and_every_roster_consumer_is_injected()

    def test_no_native_grok_path_in_fork_context_service(self) -> None:
        """Source code must not contain Path.home()/.grok after routing fix."""
        import inspect

        from cli_agent_orchestrator.services import fork_context_service

        source = inspect.getsource(fork_context_service)
        # The fix routes through _resolved_grok_sessions / provider_home
        assert 'Path.home() / ".grok"' not in source
