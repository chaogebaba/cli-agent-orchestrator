"""Pre-flight health check and registry self-heal for CAO.

Provides:
- ``cao doctor`` — verify provider binaries are resolvable and executable
- ``cao doctor readopt`` — re-adopt live tmux panes whose terminal rows vanished
"""

import os
import sys
from pathlib import Path

import click

from cli_agent_orchestrator.utils.binary_resolution import resolve_provider_binary


class _DoctorGroup(click.Group):
    """Group that invokes the check subcommand when called without a subcommand."""

    def invoke(self, ctx: click.Context) -> None:
        if not ctx.args and not ctx.invoked_subcommand:
            # No subcommand given — run the default "check" behaviour
            ctx.invoke(check)
            return
        super().invoke(ctx)


@click.group(cls=_DoctorGroup, invoke_without_command=True)
def doctor() -> None:
    """Pre-flight check and registry self-heal utilities."""


@doctor.command()
def check() -> None:
    """Verify provider binaries are resolvable and executable."""
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


@doctor.command()
@click.option("--apply", is_flag=True, default=False, help="Execute readoptions (default: dry-run)")
def readopt(apply: bool) -> None:
    """Re-adopt live tmux panes whose terminal rows vanished.

    Scans the host tmux server for cao-* sessions with windows named
    <profile>-<terminalid>. For each ID missing from the terminals table,
    reconstructs the minimal registry rows (terminal + supervisor mailbox
    when the profile role is 'supervisor').

    DRY-RUN by default — prints the plan without writing. Pass --apply to
    execute.
    """
    from cli_agent_orchestrator.services.readopt_service import (
        apply_readopt,
        scan_for_orphans,
    )

    click.echo("Scanning tmux for orphaned CAO windows...")
    result = scan_for_orphans()

    if result.skipped_test:
        click.echo(f"  Skipped {len(result.skipped_test)} test-session windows")
    if result.skipped_existing:
        click.echo(f"  Skipped {len(result.skipped_existing)} already-registered terminals")

    if not result.planned:
        click.echo("  No orphaned terminals found. Registry is consistent.")
        sys.exit(0)

    click.echo(f"\n  Found {len(result.planned)} orphaned terminal(s):")
    for plan in result.planned:
        mailbox_note = " [+mailbox]" if plan.needs_mailbox else ""
        click.echo(
            f"    {plan.terminal_id}  session={plan.tmux_session}  "
            f"profile={plan.agent_profile}  provider={plan.provider}  "
            f"lifecycle={plan.lifecycle}{mailbox_note}"
        )

    if not apply:
        click.echo("\n  DRY RUN — pass --apply to execute.")
        sys.exit(0)

    click.echo("\n  Applying...")
    apply_readopt(result)

    if result.applied:
        click.echo(f"  Adopted {len(result.applied)} terminal(s): {', '.join(result.applied)}")
    if result.errors:
        click.echo(f"  Errors ({len(result.errors)}):")
        for err in result.errors:
            click.echo(f"    {err}")
        sys.exit(1)

    sys.exit(0)
