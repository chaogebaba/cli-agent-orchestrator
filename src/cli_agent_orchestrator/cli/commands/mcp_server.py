"""MCP server command for CLI Agent Orchestrator CLI."""

from typing import Callable

import click


@click.command(name="mcp-server")
def mcp_server() -> None:
    """Start the CAO MCP server."""
    # Imported lazily inside the command so building the CLI command tree does
    # NOT import mcp_server.server. That module registers the MCP-server
    # surfaces at import time (register_mcp_server_surfaces), which emits the
    # "CAO_MCP_APPS_ENABLED is set but no IdP is configured" warning. A
    # top-level import here made every trivial CLI invocation (cao --version,
    # cao install, ...) pay that MCP startup and print the warning (issue #428).
    # Only actually starting the server should mount the surface.
    from cli_agent_orchestrator.mcp_server.server import main as run_mcp_server

    click.echo("Starting CAO MCP server...")
    _run: Callable[[], None] = run_mcp_server
    _run()
