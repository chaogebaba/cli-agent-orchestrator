"""Human-gated reinstall, restart, and deployment verification."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path
from urllib.parse import quote

import click
import requests

from cli_agent_orchestrator.constants import MCP_REQUEST_TIMEOUT
from cli_agent_orchestrator.services.verification_service import (
    DeploymentStatus,
    cli_deploy_root,
    deployment_status,
    format_server_status,
    git_root,
)
from cli_agent_orchestrator.utils.http import CAOHttpClient

cao_http = CAOHttpClient(lambda: requests)
from cli_agent_orchestrator.utils.sandbox_guard import require_not_sandbox_mutation

_FROZEN_PROFILE_MARKER = "# FROZEN:"
_NOT_RESTARTED = "installed, NOT restarted - server-path changes inactive until restart"
_VERIFY_POLL_INTERVAL_SECONDS = 0.5
_VERIFY_TIMEOUT_SECONDS = 30


def _redeploy_source_root() -> Path:
    return cli_deploy_root(git_root())


def _installable_profiles(workspace_root: Path) -> list[Path]:
    """Return active workspace profiles in deterministic filename order."""
    profiles = sorted((workspace_root / "profiles").glob("*.md"), key=lambda path: path.name)
    return [
        profile
        for profile in profiles
        if not any(
            line.startswith(_FROZEN_PROFILE_MARKER)
            for line in profile.read_text(encoding="utf-8").splitlines()
        )
    ]


def _install_python_version(source_root: Path) -> str:
    """Derive the interpreter to install under from the package's own floor.

    This used to be the literal "3.13" while `install.sh` said 3.14, so raising
    the floor to >=3.14 made `cao redeploy` resolve an interpreter its own
    package rejects:

        Because the current Python version (3.13.14) does not satisfy
        Python>=3.14 ... your requirements are unsatisfiable

    A hardcoded version in the installer is a SECOND copy of the floor that
    nothing keeps in step with the first. Reading `requires-python` means the
    two cannot disagree: there is only one floor now, and this asks it.
    """
    pyproject = (source_root / "pyproject.toml").read_text(encoding="utf-8")
    requires = tomllib.loads(pyproject)["project"]["requires-python"]
    match = re.search(r"(\d+)\.(\d+)", requires)
    if match is None:
        raise ValueError(f"cannot derive an interpreter from requires-python = {requires!r}")
    return f"{match.group(1)}.{match.group(2)}"


def _install_redeploy(source_root: Path, *, force_providers: bool = False) -> None:
    workspace_root = source_root.parent
    subprocess.run(
        [
            "uv",
            "tool",
            "install",
            "--force",
            "--python",
            _install_python_version(source_root),
            str(source_root),
        ],
        check=True,
    )
    cao = shutil.which("cao") or "cao"
    reconcile_command = [cao, "config", "reconcile"]
    if force_providers:
        reconcile_command.append("--force-providers")
    subprocess.run(reconcile_command, check=True)
    for profile in _installable_profiles(workspace_root):
        # cao install derives the provider from the profile's own frontmatter.
        subprocess.run([cao, "install", str(profile)], check=True)


def _live_terminal_session_count() -> tuple[int, int] | None:
    try:
        response = cao_http.get(f"/sessions", timeout=MCP_REQUEST_TIMEOUT)
        response.raise_for_status()
        sessions = response.json()
        if not isinstance(sessions, list):
            return None
        terminal_count = 0
        for session in sessions:
            name = session["name"]
            terminals = cao_http.get(
                f"/sessions/{quote(name, safe='')}/terminals",
                timeout=MCP_REQUEST_TIMEOUT,
            )
            terminals.raise_for_status()
            rows = terminals.json()
            if not isinstance(rows, list):
                return None
            terminal_count += len(rows)
        return terminal_count, len(sessions)
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return None


def _stdin_is_tty() -> bool:
    return bool(click.get_text_stream("stdin").isatty())


def _restart_server() -> None:
    subprocess.run(["systemctl", "--user", "restart", "cao-server"], check=True)


def _verify_redeploy(source_root: Path) -> DeploymentStatus:
    return deployment_status(source_root)


def _wait_for_server(source_root: Path) -> tuple[DeploymentStatus, int | None]:
    started = time.monotonic()
    deadline = started + _VERIFY_TIMEOUT_SECONDS
    reported_second: int | None = None
    while True:
        elapsed = min(int(time.monotonic() - started), _VERIFY_TIMEOUT_SECONDS)
        if elapsed != reported_second:
            click.echo(f"waiting for server to come back up... ({elapsed}s)")
            reported_second = elapsed
        status = _verify_redeploy(source_root)
        if status["server"] != "not-running":
            return status, None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return status, _VERIFY_TIMEOUT_SECONDS
        time.sleep(min(_VERIFY_POLL_INTERVAL_SECONDS, remaining))


def _print_deployment(
    status: DeploymentStatus,
    *,
    restarted: bool = False,
    timeout_seconds: int | None = None,
) -> None:
    count = status["differing_files"]
    if count is None:
        click.echo(f"CLI path: {status['cli_path']}")
    else:
        click.echo(f"CLI path: {status['cli_path']} ({count} files differ)")
    click.echo(
        format_server_status(
            status["server"],
            restarted=restarted,
            timeout_seconds=timeout_seconds,
        )
    )


@click.command()
@click.option("--yes", is_flag=True, help="Restart without an interactive confirmation.")
@click.option(
    "--force-providers",
    is_flag=True,
    help="Back up and reset providers.toml from the workspace template.",
)
def redeploy(yes: bool, force_providers: bool) -> None:
    """Reinstall CAO, optionally restart its server, then verify deployment."""
    require_not_sandbox_mutation("redeploy")
    source_root = _redeploy_source_root()
    try:
        _install_redeploy(source_root, force_providers=force_providers)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(f"install failed: {exc}") from exc

    restart = yes
    if not yes:
        if not _stdin_is_tty():
            click.echo(_NOT_RESTARTED)
            return
        live = _live_terminal_session_count()
        count_text = (
            "live terminal/session count unavailable"
            if live is None
            else f"{live[0]} live terminal(s) across {live[1]} session(s)"
        )
        restart = click.confirm(
            f"Restart cao-server now? This will kill {count_text}", default=False
        )
    if not restart:
        click.echo(_NOT_RESTARTED)
        return

    click.echo("restarting cao-server...")
    try:
        _restart_server()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(f"restart failed: {exc}") from exc
    status, timeout_seconds = _wait_for_server(source_root)
    _print_deployment(
        status,
        restarted=True,
        timeout_seconds=timeout_seconds,
    )
    if status["cli_path"] != "current" or status["server"] != "current":
        raise click.exceptions.Exit(1)
