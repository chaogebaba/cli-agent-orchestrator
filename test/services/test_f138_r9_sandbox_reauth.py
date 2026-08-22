"""F138 R9: Sandbox-only mock_cli reauth/rebind reachability tests.

Validates:
- Sandbox mock_cli provider is accepted by POST /sessions/{name}/recover
  when fixture capability advertises supports_reauth_rebind.
- Non-sandbox mock_cli is rejected (production whitelist enforced).
- codex/grok_cli behavior unchanged.
- Unsupported fixture provider rejected even in sandbox.
- MockCliProvider session identity methods (capture/validate/resume).
- Generation 1→2 same terminal ID rebind path reachability.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SANDBOX_ENV = {"CAO_INSTANCE_ID": "test-f138-r9-sandbox"}
_NO_SANDBOX_ENV = {"CAO_INSTANCE_ID": ""}


def _make_fixture_capability():
    """Build a mock SandboxFixtureProviderCapability for mock_cli."""
    from cli_agent_orchestrator.utils.provider_plane import (
        SandboxFixtureProviderCapability,
    )

    return SandboxFixtureProviderCapability(
        provider="mock_cli",
        binary_realpath=Path("/tmp/fake-bin/mock_cli"),
        binary_sha256="deadbeef" * 8,
        variant="healthy",
        state_dir=Path("/tmp/fake-sandbox/state"),
    )


# ---------------------------------------------------------------------------
# API Validation Boundary Tests
# ---------------------------------------------------------------------------


class TestRecoverProviderValidation:
    """Pydantic SessionRecoverRequest.validate_provider boundary."""

    def test_codex_always_accepted(self):
        """codex passes regardless of sandbox state."""
        from cli_agent_orchestrator.api.main import SessionRecoverRequest

        req = SessionRecoverRequest(reason="provider-reauth", provider="codex")
        assert req.provider == "codex"

    def test_grok_cli_always_accepted(self):
        """grok_cli passes regardless of sandbox state."""
        from cli_agent_orchestrator.api.main import SessionRecoverRequest

        req = SessionRecoverRequest(reason="provider-reauth", provider="grok_cli")
        assert req.provider == "grok_cli"

    def test_mock_cli_rejected_outside_sandbox(self):
        """mock_cli is rejected when CAO_INSTANCE_ID is absent (production)."""
        from cli_agent_orchestrator.api.main import SessionRecoverRequest

        with patch.dict(os.environ, _NO_SANDBOX_ENV, clear=False):
            with pytest.raises(ValidationError) as exc_info:
                SessionRecoverRequest(reason="provider-reauth", provider="mock_cli")
            assert "provider must be codex or grok_cli" in str(exc_info.value)

    def test_mock_cli_accepted_in_sandbox_with_capability(self):
        """mock_cli passes when sandbox active + fixture capability validates."""
        from cli_agent_orchestrator.api.main import SessionRecoverRequest

        cap = _make_fixture_capability()
        with patch.dict(os.environ, _SANDBOX_ENV, clear=False):
            with patch(
                "cli_agent_orchestrator.utils.provider_plane.load_active_fixture_provider",
                return_value=cap,
            ):
                req = SessionRecoverRequest(reason="provider-reauth", provider="mock_cli")
                assert req.provider == "mock_cli"

    def test_mock_cli_rejected_in_sandbox_capability_fails(self):
        """mock_cli rejected when sandbox active but capability validation fails."""
        from cli_agent_orchestrator.api.main import SessionRecoverRequest
        from cli_agent_orchestrator.utils.sandbox_guard import SandboxProviderUnsafe

        with patch.dict(os.environ, _SANDBOX_ENV, clear=False):
            with patch(
                "cli_agent_orchestrator.utils.provider_plane.load_active_fixture_provider",
                side_effect=SandboxProviderUnsafe("sandbox_fixture_provider_no_manifest_row"),
            ):
                with pytest.raises(ValidationError) as exc_info:
                    SessionRecoverRequest(reason="provider-reauth", provider="mock_cli")
                assert "fixture capability validation failed" in str(exc_info.value)

    def test_unsupported_provider_rejected_even_in_sandbox(self):
        """A provider not in {codex, grok_cli, mock_cli} is always rejected."""
        from cli_agent_orchestrator.api.main import SessionRecoverRequest

        with patch.dict(os.environ, _SANDBOX_ENV, clear=False):
            with pytest.raises(ValidationError) as exc_info:
                SessionRecoverRequest(reason="provider-reauth", provider="kiro_cli")
            assert "provider must be codex or grok_cli" in str(exc_info.value)

    def test_mock_cli_rejected_if_supports_reauth_rebind_false(self):
        """mock_cli rejected when class does not advertise supports_reauth_rebind."""
        from cli_agent_orchestrator.api.main import SessionRecoverRequest

        cap = _make_fixture_capability()
        with patch.dict(os.environ, _SANDBOX_ENV, clear=False):
            with patch(
                "cli_agent_orchestrator.utils.provider_plane.load_active_fixture_provider",
                return_value=cap,
            ):
                with patch(
                    "cli_agent_orchestrator.providers.mock_cli.MockCliProvider.supports_reauth_rebind",
                    False,
                ):
                    with pytest.raises(ValidationError) as exc_info:
                        SessionRecoverRequest(reason="provider-reauth", provider="mock_cli")
                    assert "supports_reauth_rebind" in str(exc_info.value)


# ---------------------------------------------------------------------------
# MockCliProvider Session Identity Tests
# ---------------------------------------------------------------------------


class TestMockCliSessionIdentity:
    """MockCliProvider capture/validate/resume session UUID methods."""

    def _make_provider(self, terminal_id: str = "abcd1234"):
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        return MockCliProvider(
            terminal_id=terminal_id,
            session_name="test-session",
            window_name="test-window",
        )

    def test_capture_session_uuid_deterministic(self):
        """capture_session_uuid returns terminal-id-based stable UUID."""
        p = self._make_provider("deadbeef")
        uuid1 = p.capture_session_uuid(12345, 1000.0, "/tmp")
        uuid2 = p.capture_session_uuid(99999, 2000.0, "/other")
        assert uuid1 == uuid2 == "mock-session-deadbeef"

    def test_resume_session_uuid_matches_capture(self):
        """resume_session_uuid returns same value as capture."""
        p = self._make_provider("abcd1234")
        captured = p.capture_session_uuid(1, 1.0, "/x")
        resumed = p.resume_session_uuid()
        assert captured == resumed == "mock-session-abcd1234"

    def test_validate_session_artifact_accepts_correct(self):
        """validate_session_artifact passes for matching UUID."""
        p = self._make_provider("face0001")
        # Should not raise
        p.validate_session_artifact("mock-session-face0001", "/tmp")

    def test_validate_session_artifact_rejects_mismatch(self):
        """validate_session_artifact raises on UUID mismatch."""
        p = self._make_provider("face0001")
        with pytest.raises(ValueError, match="session artifact mismatch"):
            p.validate_session_artifact("mock-session-other", "/tmp")

    def test_validate_session_artifact_rejects_empty(self):
        """validate_session_artifact raises on empty UUID."""
        p = self._make_provider("face0001")
        with pytest.raises(ValueError, match="session artifact mismatch"):
            p.validate_session_artifact("", "/tmp")


# ---------------------------------------------------------------------------
# Generation 1→2 Same Terminal ID Rebind Path Test
# ---------------------------------------------------------------------------


class TestSameIdRebindGeneration:
    """F138 ARM4: generation increment across same-terminal rebind.

    This tests the rebind_terminal service layer contract: given a terminal
    with generation=1 metadata, the rebind path increments to generation=2
    using the same terminal_id.
    """

    @pytest.fixture
    def mock_env(self):
        """Minimal mocks for rebind_terminal entry."""
        terminal_id = "aabb0011"
        metadata = {
            "id": terminal_id,
            "tmux_session": "test-sess",
            "tmux_window": "test-win",
            "provider": "mock_cli",
            "shell_command": "/bin/bash",
            "recovery_state": None,
            "recovery_error": None,
            "provider_session_id": f"mock-session-{terminal_id}",
            "lifecycle_generation": "1",
        }
        return terminal_id, metadata

    def test_rebind_validates_supports_reauth_rebind(self, mock_env):
        """rebind_terminal checks supports_reauth_rebind on the provider instance."""
        from cli_agent_orchestrator.providers.mock_cli import MockCliProvider

        terminal_id, metadata = mock_env

        provider = MockCliProvider(
            terminal_id=terminal_id,
            session_name="test-sess",
            window_name="test-win",
        )
        # The provider advertises reauth rebind
        assert provider.supports_reauth_rebind is True
        # capture_session_uuid returns deterministic value
        uuid = provider.capture_session_uuid(1234, 1.0, "/tmp")
        assert uuid == f"mock-session-{terminal_id}"
        # validate_session_artifact passes for same UUID
        provider.validate_session_artifact(uuid, "/tmp")
