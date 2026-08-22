"""Inspect and override evidence-gated consumer seams."""

from __future__ import annotations

import json

import click

from cli_agent_orchestrator.clients.database import init_db
from cli_agent_orchestrator.services import seam_activation, seam_parity


@click.group()
def seam() -> None:
    """Inspect parity state or apply rollback/reset overrides."""

    init_db()
    seam_parity.startup_repair()


@seam.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def status_cmd(as_json: bool) -> None:
    rows = seam_parity.status_rows()
    if as_json:
        click.echo(json.dumps([row.__dict__ for row in rows], indent=2, default=str))
        return
    click.echo(
        "op\tauthority\tversions(a/active/rollback)\tphase\tclean\tmismatches\tbuild\tlast_mismatch"
    )
    for row in rows:
        versions = f"{row.accepted_version}/{row.active_version}/{row.rollback_version}"
        mismatch = row.last_mismatch_detail or "-"
        click.echo(
            f"{row.consumer_op}\t{row.authority}\t{versions}\t{row.phase}\t"
            f"{row.clean_samples}\t{len(row.mismatches)}\t{row.build_id}\t{mismatch}"
        )


@seam.command("rollback")
@click.argument("consumer_op", type=click.Choice(seam_parity.PARITY_CONSUMER_OPS))
def rollback_cmd(consumer_op: str) -> None:
    result = seam_parity.manual_rollback(consumer_op)
    if not isinstance(result, seam_activation.RolledBack):
        raise click.ClickException(f"rollback conflict for {consumer_op}")
    click.echo(f"rolled back {consumer_op}; collecting window opened")


@seam.command("reset")
@click.argument("consumer_op", type=click.Choice(seam_parity.PARITY_CONSUMER_OPS))
def reset_cmd(consumer_op: str) -> None:
    seam_parity.reset(consumer_op)
    click.echo(f"reset {consumer_op}")


__all__ = ["seam"]
