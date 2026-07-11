"""Run and record the repository test suite."""

import re
import subprocess

import click

from cli_agent_orchestrator.services.verification_service import run_suite


@click.command()
@click.argument("feature", type=click.STRING)
def suite(feature: str) -> None:
    """Run the full suite and atomically record a stamped FEATURE log."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", feature):
        raise click.ClickException("feature must contain only letters, digits, '.', '_' or '-'")
    try:
        exit_code, path, summary = run_suite(feature, click.get_text_stream("stdout"))
    except (OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(str(exc))
    click.echo(f"summary: {summary}")
    click.echo(f"exit code: {exit_code}")
    click.echo(f"log: {path}")
    if exit_code:
        raise click.exceptions.Exit(exit_code)
