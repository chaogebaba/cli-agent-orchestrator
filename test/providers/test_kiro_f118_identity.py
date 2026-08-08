"""Unit tests for F118: Loud Identity Guarantee for kiro_cli.

Tests cover:
- AC2: install_service injects ${VAR} env into all MCP entries
- AC3: kiro_cli no longer writes per-terminal agent JSON
- AC6: missing base agent JSON raises loud error
- AC7: post-launch kiro_default detection raises loud error
- AC8: provider routing mismatch raises loud error
- Literal ${...} passthrough pinning (empirical gate finding: unset var → literal)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.services.install_service import (
    _inject_kiro_identity_env,
    _inject_kiro_mcp_timeout,
)


# =============================================================================
# AC2: install_service injects ${VAR} env into all MCP entries
# =============================================================================


class TestInstallServiceInjectsVarEnv:
    """F118 AC2: base kiro JSON carries ${VAR} env on EVERY MCP entry."""

    def test_inject_identity_env_adds_vars_to_cao_server(self):
        """cao-mcp-server entry gets all three ${VAR} identity refs."""
        mcp_servers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "/usr/local/bin/cao-mcp-server",
                "args": [],
            }
        }
        result = _inject_kiro_identity_env(mcp_servers)
        env = result["cao-mcp-server"]["env"]
        assert env["CAO_TERMINAL_ID"] == "${CAO_TERMINAL_ID}"
        assert env["CAO_INSTANCE_ID"] == "${CAO_INSTANCE_ID}"
        assert env["CAO_ENDPOINT"] == "${CAO_ENDPOINT}"

    def test_inject_identity_env_adds_vars_to_all_entries(self):
        """D3: inject into EVERY entry, not just cao-mcp-server."""
        mcp_servers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "/usr/local/bin/cao-mcp-server",
                "args": [],
            },
            "some-other-server": {
                "type": "stdio",
                "command": "/usr/local/bin/other-server",
                "args": [],
            },
        }
        result = _inject_kiro_identity_env(mcp_servers)
        for name in mcp_servers:
            assert result[name]["env"]["CAO_TERMINAL_ID"] == "${CAO_TERMINAL_ID}"
            assert result[name]["env"]["CAO_INSTANCE_ID"] == "${CAO_INSTANCE_ID}"
            assert result[name]["env"]["CAO_ENDPOINT"] == "${CAO_ENDPOINT}"

    def test_inject_preserves_existing_env(self):
        """Existing env keys are preserved; only missing identity vars added."""
        mcp_servers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "/usr/local/bin/cao-mcp-server",
                "args": [],
                "env": {"CUSTOM_VAR": "keep_me"},
            }
        }
        result = _inject_kiro_identity_env(mcp_servers)
        env = result["cao-mcp-server"]["env"]
        assert env["CUSTOM_VAR"] == "keep_me"
        assert env["CAO_TERMINAL_ID"] == "${CAO_TERMINAL_ID}"

    def test_inject_does_not_override_explicit_values(self):
        """setdefault semantics: explicit value wins over ${VAR}."""
        mcp_servers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "/usr/local/bin/cao-mcp-server",
                "args": [],
                "env": {"CAO_TERMINAL_ID": "explicit_tid"},
            }
        }
        result = _inject_kiro_identity_env(mcp_servers)
        assert result["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "explicit_tid"

    def test_inject_none_passthrough(self):
        """None input returns None."""
        assert _inject_kiro_identity_env(None) is None

    def test_inject_empty_passthrough(self):
        """Empty dict returns empty dict."""
        assert _inject_kiro_identity_env({}) == {}

    def test_inject_non_dict_entry_passthrough(self):
        """Non-dict entries pass through unchanged."""
        mcp_servers = {"broken": "not_a_dict"}
        result = _inject_kiro_identity_env(mcp_servers)
        assert result["broken"] == "not_a_dict"


# =============================================================================
# AC3: kiro_cli no longer writes per-terminal agent JSON
# =============================================================================


class TestKiroCliNoPerTerminalFiles:
    """F118 AC3: initialize uses --agent <profile_name>, no per-terminal file."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.kiro_cli.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.kiro_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.kiro_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.kiro_cli.get_backend")
    async def test_initialize_uses_base_agent_name(
        self,
        mock_backend,
        mock_wait_status,
        mock_wait_shell,
        mock_load_profile,
        tmp_path,
    ):
        """Verify the tmux send-keys command contains --agent developer (not cao-<tid>)."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "developer.json").write_text(
            json.dumps({"name": "developer", "mcpServers": {}}), encoding="utf-8"
        )

        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load_profile.side_effect = FileNotFoundError("no profile")
        mock_backend.return_value.capture_viewport.return_value = ""

        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")

        with patch(
            "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", agents_dir
        ):
            await provider.initialize()

        send_keys_call = mock_backend.return_value.send_keys.call_args_list[0]
        command_sent = send_keys_call[0][2]
        assert "--agent developer" in command_sent
        assert "cao-ab12cd34" not in command_sent

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.kiro_cli.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.kiro_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.kiro_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.kiro_cli.get_backend")
    async def test_initialize_writes_no_per_terminal_json(
        self,
        mock_backend,
        mock_wait_status,
        mock_wait_shell,
        mock_load_profile,
        tmp_path,
    ):
        """No <tid>.kiro-agent.json created during initialize."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "developer.json").write_text(
            json.dumps({"name": "developer", "mcpServers": {}}), encoding="utf-8"
        )

        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_load_profile.side_effect = FileNotFoundError("no profile")
        mock_backend.return_value.capture_viewport.return_value = ""

        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")

        with patch(
            "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", agents_dir
        ):
            await provider.initialize()

        # No per-terminal file should exist
        per_terminal = agents_dir / "ab12cd34.kiro-agent.json"
        assert not per_terminal.exists()


# =============================================================================
# AC6: Missing base agent JSON raises loud error
# =============================================================================


class TestKiroMissingBaseFailsLoud:
    """F118 AC6: _assert_kiro_base_agent raises on missing base."""

    def test_missing_base_raises(self, tmp_path):
        """Guard raises RuntimeError naming the missing file."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        # No developer.json — base is missing

        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")

        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", agents_dir
            ),
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.load_agent_profile",
                side_effect=FileNotFoundError("no profile"),
            ),
            pytest.raises(RuntimeError, match="kiro base agent JSON missing"),
        ):
            provider._assert_kiro_identity_guard()

    def test_existing_base_passes(self, tmp_path):
        """Guard passes when base JSON exists."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "developer.json").write_text("{}", encoding="utf-8")

        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")

        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", agents_dir
            ),
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.load_agent_profile",
                side_effect=FileNotFoundError("no profile"),
            ),
        ):
            # Should not raise
            provider._assert_kiro_identity_guard()


# =============================================================================
# AC7: Post-launch kiro_default assertion
# =============================================================================


class TestKiroPostlaunchDefaultAssert:
    """F118 AC7: post-launch identity assertion catches kiro_default."""

    def test_kiro_default_status_bar_raises(self):
        """Pane showing 'kiro_default ·' triggers RuntimeError."""
        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")
        mock_backend = MagicMock()
        mock_backend.capture_viewport.return_value = (
            "kiro_default · claude-sonnet-4-20250514 · ◔ 98%\n"
            "Ask a question or describe a task ↵"
        )

        with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=mock_backend):
            with pytest.raises(RuntimeError, match="kiro_default"):
                provider._assert_postlaunch_identity()

    def test_agent_not_found_banner_raises(self):
        """Pane containing 'agent "..." not found' triggers RuntimeError."""
        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")
        mock_backend = MagicMock()
        mock_backend.capture_viewport.return_value = (
            'agent "developer" not found, using "kiro_default"\n'
        )

        with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=mock_backend):
            with pytest.raises(RuntimeError, match="kiro_default"):
                provider._assert_postlaunch_identity()

    def test_correct_agent_passes(self):
        """Pane showing expected agent does not raise."""
        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")
        mock_backend = MagicMock()
        mock_backend.capture_viewport.return_value = (
            "developer · claude-sonnet-4-20250514 · ◔ 98%\n"
            "Ask a question or describe a task ↵"
        )

        with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=mock_backend):
            # Should not raise
            provider._assert_postlaunch_identity()

    def test_empty_pane_passes(self):
        """Empty pane content does not raise (timeout catches real failures)."""
        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")
        mock_backend = MagicMock()
        mock_backend.capture_viewport.return_value = ""

        with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=mock_backend):
            provider._assert_postlaunch_identity()


# =============================================================================
# AC8: Provider routing mismatch rejected
# =============================================================================


class TestProviderRoutingMismatchRejected:
    """F118 AC8: profile declaring different provider is rejected."""

    def test_claude_code_profile_on_kiro_raises(self, tmp_path):
        """Profile with provider='claude_code' routed to kiro_cli raises."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "design_reviewer.json").write_text("{}", encoding="utf-8")

        mock_profile = MagicMock()
        mock_profile.provider = "claude_code"

        provider = KiroCliProvider(
            "ab12cd34", "test-session", "window-0", "design_reviewer"
        )

        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", agents_dir
            ),
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.load_agent_profile",
                return_value=mock_profile,
            ),
            pytest.raises(RuntimeError, match="routing mismatch"),
        ):
            provider._assert_kiro_identity_guard()

    def test_kiro_cli_profile_passes(self, tmp_path):
        """Profile with provider='kiro_cli' passes."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "developer.json").write_text("{}", encoding="utf-8")

        mock_profile = MagicMock()
        mock_profile.provider = "kiro_cli"

        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")

        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", agents_dir
            ),
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.load_agent_profile",
                return_value=mock_profile,
            ),
        ):
            # Should not raise
            provider._assert_kiro_identity_guard()

    def test_no_provider_field_passes(self, tmp_path):
        """Profile with provider=None passes (no explicit routing)."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "developer.json").write_text("{}", encoding="utf-8")

        mock_profile = MagicMock()
        mock_profile.provider = None

        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")

        with (
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", agents_dir
            ),
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.load_agent_profile",
                return_value=mock_profile,
            ),
        ):
            provider._assert_kiro_identity_guard()


# =============================================================================
# AC6 cleanup: cleanup is now a no-op (no per-terminal files to remove)
# =============================================================================


class TestKiroCleanupNoop:
    """F118 AC6: cleanup writes nothing after A-path deletion."""

    def test_cleanup_resets_initialized_only(self, tmp_path):
        """cleanup() sets _initialized=False and does nothing else to disk."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        provider = KiroCliProvider("ab12cd34", "test-session", "window-0", "developer")
        provider._initialized = True

        with patch(
            "cli_agent_orchestrator.providers.kiro_cli.KIRO_AGENTS_DIR", agents_dir
        ):
            provider.cleanup()

        assert provider._initialized is False
        # No files created or modified
        assert list(agents_dir.iterdir()) == []


# =============================================================================
# Literal ${...} passthrough pinning (empirical gate finding)
# =============================================================================


class TestLiteralVarPassthrough:
    """Empirical gate: unset var expands to literal ${VAR}, not empty string.

    The gate finding (kiro_reviewer-7987) confirmed that when an env var is UNSET,
    kiro-cli passes through the literal '${CAO_TERMINAL_ID}' string (not empty).
    This is the expected behavior that the install_service relies on for safe
    degradation — if something goes wrong, the MCP server sees the literal ref
    rather than an empty string.
    """

    def test_injected_values_are_literal_dollar_brace(self):
        """The injected values are literal '${VAR}' strings (passthrough refs)."""
        mcp_servers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "/usr/local/bin/cao-mcp-server",
                "args": [],
            }
        }
        result = _inject_kiro_identity_env(mcp_servers)
        env = result["cao-mcp-server"]["env"]

        # Verify they are literal ${...} strings — not empty, not resolved
        assert env["CAO_TERMINAL_ID"] == "${CAO_TERMINAL_ID}"
        assert env["CAO_TERMINAL_ID"].startswith("${")
        assert env["CAO_TERMINAL_ID"].endswith("}")

        # They must NOT be empty (the point of F118: on expansion failure,
        # kiro passes literal, not empty — so the MCP server can detect the
        # misconfiguration rather than silently receiving "")
        assert len(env["CAO_TERMINAL_ID"]) > 2

    def test_full_chain_produces_literal_refs_in_json(self, tmp_path):
        """End-to-end: _inject_kiro_identity_env → _inject_kiro_mcp_timeout
        chain produces JSON with literal ${...} refs."""
        mcp_servers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "/usr/local/bin/cao-mcp-server",
                "args": [],
            }
        }
        # Match the install_service call order
        result = _inject_kiro_identity_env(_inject_kiro_mcp_timeout(mcp_servers))
        serialized = json.dumps(result)

        # Verify literal ${...} survives JSON serialization
        assert "${CAO_TERMINAL_ID}" in serialized
        assert "${CAO_INSTANCE_ID}" in serialized
        assert "${CAO_ENDPOINT}" in serialized
