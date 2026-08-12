"""F139 r5 D15/D16 — launch_health_failure_confirmed tests.

Kill tests for the three-site coordination:
  1. BaseProvider class attr (default False)
  2. MockCliProvider empty-shell sets True after baseline confirmation
  3. _provider_child_alive consumes the flag before procfs/tmux checks

Mutant targets:
  M1 — remove consumer check in _provider_child_alive
  M2 — remove/move state set in mock_cli empty-shell path
  M3 — set True for all variants (not just empty-shell)
  M4 — default True in BaseProvider (should break real providers)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.mock_cli import MockCliProvider


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_provider(terminal_id: str = "ab12cd34") -> MockCliProvider:
    return MockCliProvider(
        terminal_id=terminal_id,
        session_name="test-session",
        window_name="test-window",
    )


def _fake_capability(variant: str, state_dir: Path | None = None):
    return SimpleNamespace(
        provider="mock_cli",
        binary_realpath=Path("/fake/bin/mock_cli"),
        binary_sha256="a" * 64,
        variant=variant,
        state_dir=state_dir or Path("/tmp/fake-state"),
    )


async def _run_provider_child_alive(provider, terminal_id="ab12cd34"):
    """Call the production _provider_child_alive with controlled mocks."""
    from cli_agent_orchestrator.services.terminal_service import _provider_child_alive

    # Mock the fork_context_service to prevent real procfs access
    with patch(
        "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
        return_value={
            "tmux_session": "test-session",
            "tmux_window": "test-window",
        },
    ):
        return await _provider_child_alive(terminal_id, provider)


async def _initialize_empty_shell_provider() -> MockCliProvider:
    """Run initialize() for empty-shell variant and return the provider."""
    provider = _make_provider()
    cap = _fake_capability("empty-shell")
    provider.shell_baseline = "zsh"

    async def mock_wait_for_shell(*a, **kw):
        return True

    mock_backend = MagicMock()
    mock_backend.send_keys = MagicMock()
    mock_backend.get_pane_current_command = MagicMock(return_value="zsh")

    with (
        patch("cli_agent_orchestrator.providers.mock_cli.wait_for_shell", mock_wait_for_shell),
        patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=mock_backend),
        patch.object(provider, "_load_fixture_capability", return_value=cap),
    ):
        result = await provider.initialize()

    assert result is True
    return provider


# ─── Core integration: production deferred-init path ──────────────────────────


class TestLaunchHealthFailureConfirmedIntegration:
    """Production-path integration: empty-shell initialize → _provider_child_alive → False."""

    @pytest.mark.asyncio
    async def test_empty_shell_provider_child_alive_returns_false(self):
        """After empty-shell initialize, _provider_child_alive must return False
        WITHOUT needing procfs or tmux — the flag short-circuits."""
        from cli_agent_orchestrator.services.terminal_service import _provider_child_alive

        provider = await _initialize_empty_shell_provider()

        # Verify flag was set
        assert provider.launch_health_failure_confirmed is True

        # Mock procfs to show ALIVE descendants — the flag must still win
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w"},
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._procfs_available",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service.pane_pid",
                return_value=12345,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._PROC_ROOT",
                new=MagicMock(**{"__truediv__": lambda s, x: MagicMock(**{"__truediv__": lambda s2, x2: MagicMock(exists=MagicMock(return_value=True))})}),
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._descendants",
                return_value=[12345, 12346, 12347],
            ),
        ):
            result = await _provider_child_alive("ab12cd34", provider)

        assert result is False

    @pytest.mark.asyncio
    async def test_deterministic_10_run_loop(self):
        """10-run deterministic integration: every run must yield False."""
        from cli_agent_orchestrator.services.terminal_service import _provider_child_alive

        results = []
        for _ in range(10):
            provider = await _initialize_empty_shell_provider()
            with (
                patch(
                    "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                    return_value={"tmux_session": "s", "tmux_window": "w"},
                ),
                patch(
                    "cli_agent_orchestrator.services.fork_context_service._procfs_available",
                    return_value=True,
                ),
                patch(
                    "cli_agent_orchestrator.services.fork_context_service.pane_pid",
                    return_value=12345,
                ),
                patch(
                    "cli_agent_orchestrator.services.fork_context_service._PROC_ROOT",
                    new=MagicMock(**{"__truediv__": lambda s, x: MagicMock(**{"__truediv__": lambda s2, x2: MagicMock(exists=MagicMock(return_value=True))})}),
                ),
                patch(
                    "cli_agent_orchestrator.services.fork_context_service._descendants",
                    return_value=[12345, 12346],
                ),
            ):
                result = await _provider_child_alive("ab12cd34", provider)
            results.append(result)

        assert results == [False] * 10, (
            f"Non-deterministic: expected all False, got {results}"
        )


# ─── M1: remove consumer check → must fail ───────────────────────────────────


class TestM1ConsumerCheck:
    """M1: if _provider_child_alive doesn't check launch_health_failure_confirmed,
    it falls through to procfs/tmux which may return True (false-alive)."""

    @pytest.mark.asyncio
    async def test_flag_true_returns_false_despite_alive_procfs(self):
        """Provider with flag=True must get False from _provider_child_alive
        even when procfs shows descendants (false-alive evidence).
        Killing the consumer check would let procfs evidence return True."""
        from cli_agent_orchestrator.services.terminal_service import _provider_child_alive

        provider = _make_provider()
        provider.launch_health_failure_confirmed = True

        # Mock everything to simulate a "live" procfs tree that would return True
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w"},
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._procfs_available",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service.pane_pid",
                return_value=12345,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._PROC_ROOT",
                new=MagicMock(**{"__truediv__": lambda s, x: MagicMock(**{"__truediv__": lambda s2, x2: MagicMock(exists=MagicMock(return_value=True))})}),
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._descendants",
                return_value=[12345, 12346, 12347],  # 3 descendants = alive
            ),
        ):
            result = await _provider_child_alive("ab12cd34", provider)

        assert result is False, (
            "M1 kill: _provider_child_alive must return False when "
            "launch_health_failure_confirmed=True, even with live procfs descendants"
        )

    @pytest.mark.asyncio
    async def test_flag_false_does_not_short_circuit(self):
        """Provider with flag=False must NOT short-circuit — falls through to procfs."""
        from cli_agent_orchestrator.services.terminal_service import _provider_child_alive

        provider = _make_provider()
        provider.launch_health_failure_confirmed = False

        # With procfs mocked unavailable, should return None (inconclusive)
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w"},
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._procfs_available",
                return_value=False,
            ),
        ):
            result = await _provider_child_alive("ab12cd34", provider)

        assert result is None, (
            "flag=False must not short-circuit; procfs unavailable → None"
        )


# ─── M2: remove/move state set after initialize return ───────────────────────


class TestM2StateSet:
    """M2: if empty-shell doesn't set the flag, _provider_child_alive races on procfs."""

    @pytest.mark.asyncio
    async def test_empty_shell_initialize_sets_flag(self):
        """After empty-shell initialize with baseline match, flag must be True."""
        provider = await _initialize_empty_shell_provider()
        assert provider.launch_health_failure_confirmed is True

    @pytest.mark.asyncio
    async def test_empty_shell_no_baseline_does_not_set_flag(self):
        """Without shell_baseline, flag stays False (no baseline confirmation)."""
        provider = _make_provider()
        cap = _fake_capability("empty-shell")
        # Deliberately don't set shell_baseline

        async def mock_wait_for_shell(*a, **kw):
            return True

        mock_backend = MagicMock()
        mock_backend.send_keys = MagicMock()
        mock_backend.get_pane_current_command = MagicMock(return_value=None)

        with (
            patch("cli_agent_orchestrator.providers.mock_cli.wait_for_shell", mock_wait_for_shell),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=mock_backend),
            patch.object(provider, "_load_fixture_capability", return_value=cap),
        ):
            await provider.initialize()

        assert provider.launch_health_failure_confirmed is False

    @pytest.mark.asyncio
    async def test_flag_not_set_before_initialize(self):
        """Fresh provider instance must have flag=False."""
        provider = _make_provider()
        assert provider.launch_health_failure_confirmed is False


# ─── M3: set true for all variants → healthy/post-send must fail ──────────────


class TestM3VariantSpecificity:
    """M3: only empty-shell sets the flag; healthy/post-send-death must remain False."""

    @pytest.mark.asyncio
    async def test_healthy_does_not_set_flag(self):
        """Healthy variant must NOT set launch_health_failure_confirmed."""
        provider = _make_provider()
        cap = _fake_capability("healthy")

        async def mock_wait_for_shell(*a, **kw):
            return True

        async def mock_wait_until_status(*a, **kw):
            return True

        mock_backend = MagicMock()
        mock_backend.send_keys = MagicMock()

        with (
            patch("cli_agent_orchestrator.providers.mock_cli.wait_for_shell", mock_wait_for_shell),
            patch("cli_agent_orchestrator.providers.mock_cli.wait_until_status", mock_wait_until_status),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=mock_backend),
            patch.object(provider, "_load_fixture_capability", return_value=cap),
        ):
            await provider.initialize()

        assert provider.launch_health_failure_confirmed is False, (
            "M3 kill: healthy variant must NOT set flag"
        )

    @pytest.mark.asyncio
    async def test_post_send_death_does_not_set_flag(self):
        """Post-send-death variant must NOT set launch_health_failure_confirmed."""
        provider = _make_provider()
        cap = _fake_capability("post-send-death")

        async def mock_wait_for_shell(*a, **kw):
            return True

        async def mock_wait_until_status(*a, **kw):
            return True

        mock_backend = MagicMock()
        mock_backend.send_keys = MagicMock()

        with (
            patch("cli_agent_orchestrator.providers.mock_cli.wait_for_shell", mock_wait_for_shell),
            patch("cli_agent_orchestrator.providers.mock_cli.wait_until_status", mock_wait_until_status),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=mock_backend),
            patch.object(provider, "_load_fixture_capability", return_value=cap),
        ):
            await provider.initialize()

        assert provider.launch_health_failure_confirmed is False, (
            "M3 kill: post-send-death variant must NOT set flag"
        )

    @pytest.mark.asyncio
    async def test_process_less_does_not_set_flag(self):
        """Process-less variant must NOT set launch_health_failure_confirmed."""
        provider = _make_provider()
        cap = _fake_capability("process-less")

        async def mock_wait_for_shell(*a, **kw):
            return True

        mock_backend = MagicMock()

        with (
            patch("cli_agent_orchestrator.providers.mock_cli.wait_for_shell", mock_wait_for_shell),
            patch("cli_agent_orchestrator.providers.mock_cli.get_backend", return_value=mock_backend),
            patch.object(provider, "_load_fixture_capability", return_value=cap),
        ):
            await provider.initialize()

        assert provider.launch_health_failure_confirmed is False


# ─── M4: default true in BaseProvider → real providers fail ───────────────────


class TestM4DefaultFalse:
    """M4: BaseProvider default must be False; otherwise all providers report dead."""

    def test_base_provider_default_is_false(self):
        """The class attribute on BaseProvider must be False."""
        assert BaseProvider.launch_health_failure_confirmed is False

    def test_fresh_mock_cli_instance_default_false(self):
        """A freshly constructed MockCliProvider has flag=False."""
        provider = _make_provider()
        assert provider.launch_health_failure_confirmed is False

    def test_real_provider_classes_default_false(self):
        """All concrete provider classes inherit False from BaseProvider."""
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
        from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

        assert ClaudeCodeProvider.launch_health_failure_confirmed is False
        assert KiroCliProvider.launch_health_failure_confirmed is False

    @pytest.mark.asyncio
    async def test_real_provider_with_default_false_passes_health_check(self):
        """A real provider (ClaudeCodeProvider) with flag=False must NOT
        short-circuit _provider_child_alive as False — it falls through."""
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        # Create a minimal real provider instance
        provider = ClaudeCodeProvider.__new__(ClaudeCodeProvider)
        provider.terminal_id = "deadbeef"
        provider.has_process_child = True
        provider.launch_health_failure_confirmed = False

        # With procfs unavailable, must return None (not False)
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                return_value={"tmux_session": "s", "tmux_window": "w"},
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._procfs_available",
                return_value=False,
            ),
        ):
            from cli_agent_orchestrator.services.terminal_service import _provider_child_alive
            result = await _provider_child_alive("deadbeef", provider)

        assert result is None, (
            "M4 kill: real provider with flag=False must degrade to procfs path, not False"
        )
