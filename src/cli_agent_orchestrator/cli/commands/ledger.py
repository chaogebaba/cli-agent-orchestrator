"""Base-CLI stub for the relocated ``ledger`` command group.

The real implementation moved to ``cao-orchestrator ledger`` (the optional
orchestrator skill's console script) because it walks parents for
``orchestrator/HANDOFF.md`` and parses skill vocabulary. The stub stays on base,
hidden from ``cao --help``, so the breaking change reports itself instead of
surfacing as Click's bare "No such command".
"""

from __future__ import annotations

import click

_POINTER = "moved: run `cao-orchestrator ledger check` (orchestrator skill command)"


@click.group(hidden=True, invoke_without_command=True)
@click.pass_context
def ledger(ctx: click.Context) -> None:
    """Moved to the `cao-orchestrator` command."""
    if ctx.invoked_subcommand is None:
        raise click.ClickException(_POINTER)


@ledger.command("check")
def check() -> None:
    """Moved to the `cao-orchestrator` command."""
    raise click.ClickException(_POINTER)
