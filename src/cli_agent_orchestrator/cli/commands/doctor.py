"""Pre-flight health check for CAO provider binaries."""

import os
import sys
from pathlib import Path

import click

from cli_agent_orchestrator.utils.binary_resolution import resolve_provider_binary


@click.command()
def doctor() -> None:
    """Pre-flight check: verify provider binaries are resolvable and executable."""
    providers = {
        "codex": "codex",
        "grok": str(Path.home() / ".grok" / "bin" / "grok"),
        "kiro": str(Path.home() / ".kiro" / "bin" / "kiro"),
    }

    all_pass = True
    for name, binary in providers.items():
        resolved = resolve_provider_binary(binary) if name == "codex" else binary
        exists = os.path.isfile(resolved) and os.access(resolved, os.X_OK)
        status = "PASS" if exists else "FAIL"
        if not exists:
            all_pass = False
        click.echo(f"  {name}: {status} ({resolved})")

    sys.exit(0 if all_pass else 1)
