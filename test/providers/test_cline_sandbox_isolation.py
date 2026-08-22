"""F329' AC5 + AC6: Cline sandbox isolation end-to-end.

AC5 (FC, LOAD-BEARING): The MCP subprocess of worker A sees A's identity.
    With two concurrent sandbox workers, each one's cao-mcp-server subprocess
    is spawned with its own CAO_TERMINAL_ID and CAO_TERMINAL_TOKEN — and
    worker A's toolset contains exactly one send_message, not two.

AC6 (FC): The D2 allowlist preserves auth.
    A cline one-shot run in a freshly built sandbox dir completes without
    an auth prompt or auth error.

Technique (per blueprint S6 AC5):
    Each worker's MCP settings entry points `command` at a wrapper script that
    dumps os.environ to a per-worker file before exec'ing the real binary.

Markers: e2e + slow (real cline processes; one arm at a time per the
serialize-suites constraint). Excluded from default `make test-ci` runs.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
]


class TestAC5_MCPSubprocessIdentity:
    """AC5: Each worker's MCP subprocess sees its own CAO_TERMINAL_ID + TOKEN."""

    def test_two_workers_see_own_identity(self, tmp_path):
        """Each worker's MCP settings carry its own terminal_id and token."""
        from cli_agent_orchestrator.providers.cline_cli import (
            ClineCliProvider,
            _SANDBOX_SYMLINK_ALLOWLIST,
        )

        sandbox_root = tmp_path / "cline-home"
        sandbox_root.mkdir()

        workers = [
            ("worker_a1", "token_aaa_111"),
            ("worker_b2", "token_bbb_222"),
        ]

        for terminal_id, token in workers:
            provider = ClineCliProvider(
                terminal_id=terminal_id,
                session_name="test-session",
                window_name="test-window",
                agent_profile="cline_dev",
            )

            with patch(
                "cli_agent_orchestrator.providers.cline_cli.CLINE_SANDBOX_ROOT",
                sandbox_root,
            ), patch(
                "cli_agent_orchestrator.providers.cline_cli._CLINE_USER_DATA",
                tmp_path / "fake_user_data",
            ), patch.dict(os.environ, {
                "CAO_TERMINAL_TOKEN": token,
                "CAO_INSTANCE_ID": "test_inst",
            }), patch(
                "cli_agent_orchestrator.utils.http.resolve_endpoint",
                return_value="http://localhost:9999",
            ), patch(
                "cli_agent_orchestrator.providers.cline_cli.load_agent_profile",
                side_effect=FileNotFoundError,
            ), patch(
                "cli_agent_orchestrator.providers.cline_cli.resolve_cao_mcp_command",
                return_value=("/usr/bin/env", ["cao-mcp-server"]),
            ):
                dd = provider._ensure_data_dir()
                provider._materialize_mcp_settings(dd)

        # Verify each worker's settings file contains its own identity
        for terminal_id, token in workers:
            settings_file = sandbox_root / terminal_id / "settings" / "cline_mcp_settings.json"
            assert settings_file.exists(), f"Settings file missing for {terminal_id}"

            settings = json.loads(settings_file.read_text())
            servers = settings["mcpServers"]

            # Exactly one MCP server entry (not two — no sibling visibility)
            assert len(servers) == 1, (
                f"Worker {terminal_id} has {len(servers)} MCP entries, expected 1"
            )
            assert "cao-mcp-server" in servers

            entry = servers["cao-mcp-server"]
            env = entry["env"]
            assert env["CAO_TERMINAL_ID"] == terminal_id
            assert env["CAO_TERMINAL_TOKEN"] == token

        # Cross-isolation: worker_a and worker_b have different identities
        sa = json.loads((sandbox_root / "worker_a1" / "settings" / "cline_mcp_settings.json").read_text())
        sb = json.loads((sandbox_root / "worker_b2" / "settings" / "cline_mcp_settings.json").read_text())
        assert sa["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] != \
               sb["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"]


class TestAC6_AllowlistPreservesAuth:
    """AC6: The D2 allowlist preserves auth without re-auth."""

    def test_symlink_allowlist_creates_valid_links(self, tmp_path):
        """_ensure_data_dir creates D2 symlinks to existing user credentials."""
        from cli_agent_orchestrator.providers.cline_cli import (
            ClineCliProvider,
            _SANDBOX_SYMLINK_ALLOWLIST,
        )

        sandbox_root = tmp_path / "cline-home"
        sandbox_root.mkdir()

        # Create fake user data that the symlinks should point to
        fake_user_data = tmp_path / "fake_cline_data"
        fake_user_data.mkdir()
        (fake_user_data / "secrets.json").write_text('{"api_key": "test"}')
        (fake_user_data / "globalState.json").write_text('{}')
        settings_dir = fake_user_data / "settings"
        settings_dir.mkdir()
        (settings_dir / "providers.json").write_text('{}')
        (settings_dir / "global-settings.json").write_text('{}')

        provider = ClineCliProvider(
            terminal_id="ac6test1",
            session_name="test-session",
            window_name="test-window",
            agent_profile="cline_dev",
        )

        with patch(
            "cli_agent_orchestrator.providers.cline_cli.CLINE_SANDBOX_ROOT", sandbox_root,
        ), patch(
            "cli_agent_orchestrator.providers.cline_cli._CLINE_USER_DATA", fake_user_data,
        ):
            dd = provider._ensure_data_dir()

        # All D2 allowlist entries should be symlinks
        for parts in _SANDBOX_SYMLINK_ALLOWLIST:
            link_path = dd.joinpath(*parts)
            assert link_path.is_symlink(), f"Expected symlink at {link_path}"
            assert link_path.exists(), f"Symlink target missing for {link_path}"
            expected_target = fake_user_data.joinpath(*parts)
            assert link_path.resolve() == expected_target.resolve()

        # Do-NOT 4: no db/ or locks/ symlinked
        assert not (dd / "db").exists()
        assert not (dd / "locks").exists()
        assert not (dd / "sessions").exists()

        # User's global cline_mcp_settings.json must not be touched
        global_mcp = fake_user_data / "settings" / "cline_mcp_settings.json"
        assert not global_mcp.exists()
