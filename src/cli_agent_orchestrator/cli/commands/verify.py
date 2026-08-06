"""Verification commands for suite artifacts, deploy state, and diff scope."""

import subprocess
from pathlib import Path

import click

from cli_agent_orchestrator.kernel.receiver_state.trace_manifest import regenerate_manifest
from cli_agent_orchestrator.services.verification_service import (
    changed_files,
    cli_deploy_root,
    deployment_status,
    format_server_status,
    git_root,
    verify_suite_log,
)


@click.group()
def verify() -> None:
    """Verify repository and installed runtime state."""


@verify.command("manifest")
@click.option("--regen", is_flag=True, help="Regenerate the receiver-state trace manifest.")
def manifest(regen: bool) -> None:
    """Maintain the receiver-state trace manifest."""
    if not regen:
        raise click.UsageError("Pass --regen to regenerate the trace manifest.")
    hits, files_touched, changed = regenerate_manifest(git_root())
    click.echo(
        f"Trace manifest: hits={hits} files_touched={files_touched} "
        f"changed={'yes' if changed else 'no'}"
    )


@verify.command("suite-log")
@click.argument("path", type=click.Path(path_type=Path))
def suite_log(path: Path) -> None:
    """Verify a stamped suite log against the current tree."""
    passed, reasons, mtime = verify_suite_log(path)
    click.echo(f"{'PASS' if passed else 'FAIL'}: {path}")
    click.echo(f"mtime: {mtime}")
    for reason in reasons:
        click.echo(f"reason: {reason}")
    if not passed:
        raise click.exceptions.Exit(1)


@verify.command("deploy")
def deploy() -> None:
    """Compare the installed CLI and running server with this working tree."""
    try:
        root = cli_deploy_root(git_root())
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(str(exc))
    status = deployment_status(root)
    state = status["cli_path"]
    count = status["differing_files"]
    if count is None:
        click.echo(f"CLI path: {state}")
    else:
        click.echo(f"CLI path: {state} ({count} files differ)")
    server_state = status["server"]
    click.echo(format_server_status(server_state))
    if state != "current" or server_state != "current":
        raise click.exceptions.Exit(1)


@verify.command("scope")
@click.argument("files", nargs=-1, required=True, type=click.Path(path_type=Path))
def scope(files: tuple[Path, ...]) -> None:
    """Require working-tree changes to exactly match FILES."""
    root = git_root()
    actual = set(changed_files(root))
    expected = {
        (
            str((Path.cwd() / path).resolve().relative_to(root))
            if not path.is_absolute()
            else str(path.resolve().relative_to(root))
        )
        for path in files
    }
    unexpected, missing = sorted(actual - expected), sorted(expected - actual)
    exact = not unexpected and not missing
    click.echo("PASS: exact scope match" if exact else "FAIL: scope mismatch")
    click.echo(f"unexpected changes: {', '.join(unexpected) if unexpected else '(none)'}")
    click.echo(f"missing expected: {', '.join(missing) if missing else '(none)'}")
    if not exact:
        raise click.exceptions.Exit(1)
