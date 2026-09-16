"""Entry point for ``cao-orchestrator``, the optional orchestrator skill's CLI.

Base ``cao`` carries no command that reads a skill-owned knowledge path. The
three that did live here instead, behind a second console script that a
lightweight project simply never runs (wp-arch-modular-core A.3/A.4).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import click

from cli_agent_orchestrator.cli.orchestrator_commands.cert_status import cert_status
from cli_agent_orchestrator.cli.orchestrator_commands.certify import certify
from cli_agent_orchestrator.cli.orchestrator_commands.fold_corpus import fold_corpus
from cli_agent_orchestrator.cli.orchestrator_commands.ledger import ledger
from cli_agent_orchestrator.cli.orchestrator_commands.lint_doctrine import lint_doctrine
from cli_agent_orchestrator.cli.orchestrator_commands.sync_routing import sync_routing

try:
    __version__ = version("cli-agent-orchestrator")
except PackageNotFoundError:  # pragma: no cover - source checkout without metadata
    __version__ = "unknown"


@click.group()
@click.version_option(__version__, "-V", "--version", prog_name="cao-orchestrator")
def cli() -> None:
    """Orchestrator-skill commands over knowledge this project owns."""


cli.add_command(ledger)
cli.add_command(fold_corpus)
cli.add_command(sync_routing)
cli.add_command(lint_doctrine)
cli.add_command(certify)
cli.add_command(cert_status)


if __name__ == "__main__":
    cli()
