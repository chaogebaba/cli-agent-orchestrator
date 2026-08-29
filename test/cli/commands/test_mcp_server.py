"""Tests for mcp-server command."""

import os
import subprocess
import sys
from importlib.metadata import version
from unittest.mock import patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.mcp_server import mcp_server

_WARNING_NEEDLE = "CAO_MCP_APPS_ENABLED is set but no IdP is configured"


def test_mcp_server_command() -> None:
    """Test that mcp-server command calls the lazily-imported server main."""
    runner = CliRunner()

    # The server main is imported lazily inside the command (issue #428), so it
    # is patched at its source module rather than as a command-module attribute.
    with patch("cli_agent_orchestrator.mcp_server.server.main") as mock_run:
        result = runner.invoke(mcp_server)

        assert result.exit_code == 0
        assert "Starting CAO MCP server..." in result.output
        mock_run.assert_called_once()


def _run(args: list[str], extra_env: dict[str, str]) -> "subprocess.CompletedProcess[str]":
    """Run a fresh interpreter (no import cache warmed by the test process) and
    return the completed process. A subprocess is required because the
    MCP-server surface registers at module import — once any in-process test
    imports ``mcp_server.server`` the module is cached and the import-time
    warning cannot re-fire, so warning counts are only observable in a clean
    process."""

    env = dict(os.environ)
    env.update(extra_env)
    # Force no IdP so the "enabled but no IdP" branch is the one under test.
    env.pop("AUTH0_DOMAIN", None)
    env.pop("CAO_AUTH_JWKS_URI", None)
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_version_emits_no_apps_warning_when_enabled() -> None:
    """issue #428: `cao --version` with apps.enabled=true must NOT import the
    MCP-server surface, so the "no IdP" posture warning fires ZERO times on a
    trivial CLI path."""

    proc = _run(
        ["-m", "cli_agent_orchestrator.cli.main", "--version"],
        {"CAO_MCP_APPS_ENABLED": "true"},
    )

    assert proc.returncode == 0, proc.stderr
    assert version("cli-agent-orchestrator") in proc.stdout
    assert proc.stderr.count(_WARNING_NEEDLE) == 0, proc.stderr


def test_server_surface_mount_emits_exactly_one_apps_warning() -> None:
    """issue #428: mounting the MCP-server surface (importing
    ``mcp_server.server``, as the cao-mcp-server entrypoint does) with
    apps.enabled=true and no IdP fires the posture warning EXACTLY once per
    process."""

    proc = _run(
        ["-c", "import cli_agent_orchestrator.mcp_server.server"],
        {"CAO_MCP_APPS_ENABLED": "true"},
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count(_WARNING_NEEDLE) == 1, proc.stderr
