"""``cao-orchestrator sync-routing`` — the relocated ``cao redeploy`` routing hunk.

``<workspace>/orchestrator/routing.toml`` is skill-owned content (the routing
*mechanism* stays infrastructure; the bindings file does not — user, 2026-09-11),
so copying it into CAO's agent store left ``cao redeploy`` and became this
command. ``cao redeploy`` keeps the neutral ``profiles/positions`` and
``profiles/overlays`` sync unchanged.
"""

from __future__ import annotations

from pathlib import Path

import click

from cli_agent_orchestrator.public_api.workspace import (
    atomic_copy,
    local_agent_store_dir,
    routing_toml_path,
)


@click.command("sync-routing")
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Workspace root holding orchestrator/routing.toml (default: current directory).",
)
def sync_routing(workspace: Path | None) -> None:
    """Copy <workspace>/orchestrator/routing.toml into CAO's agent store."""
    workspace_root = (workspace or Path.cwd()).resolve()
    routing = workspace_root / "orchestrator" / "routing.toml"
    if not routing.is_file():
        raise click.ClickException(f"{routing}: not found")
    local_agent_store_dir().mkdir(parents=True, exist_ok=True)
    atomic_copy(routing, routing_toml_path())
    click.echo(f"synced: {routing} -> {routing_toml_path()}")
