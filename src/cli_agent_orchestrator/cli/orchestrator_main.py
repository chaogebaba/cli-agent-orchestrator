"""Entry point for ``cao-orchestrator``, the optional orchestrator skill's CLI.

Base ``cao`` carries no command that reads a skill-owned knowledge path. The
three that did live here instead, behind a second console script that a
lightweight project simply never runs (wp-arch-modular-core A.3/A.4).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import click

from cli_agent_orchestrator.cli.orchestrator_commands.evidence import evidence
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


#: The verbs the migration ledger may claim as mechanisms, id -> command.
#:
#: A ledger row reading ``mechanism EXISTS [evidence-verify]`` has to resolve to
#: real code, and ``lint-doctrine`` deliberately never scans
#: ``cli/orchestrator_commands/``: resolving an id against the package that also
#: CITES it would let the linter satisfy its own check.  The registration site is
#: outside that package and is where the verb actually becomes reachable, so it
#: is the honest place for the claim to land.  Keeping the ids in a dict that
#: does the registering means the map cannot drift from the CLI —
#: ``test_lite_command_surface`` asserts every entry is a visible command.
SKILL_MECHANISMS: dict[str, click.Command] = {
    "lint-doctrine": lint_doctrine,
    "evidence-verify": evidence,
}

cli.add_command(ledger)
cli.add_command(fold_corpus)
cli.add_command(sync_routing)
for _mechanism in SKILL_MECHANISMS.values():
    cli.add_command(_mechanism)


if __name__ == "__main__":
    cli()
