"""Terminal commands for CLI Agent Orchestrator."""

import json
import os

import click
import requests

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.cli.http import served_error_message
from cli_agent_orchestrator.constants import TERMINAL_LOG_DIR
from cli_agent_orchestrator.utils.http import CAOHttpClient

cao_http = CAOHttpClient(lambda: requests)
from cli_agent_orchestrator.utils.terminal import sync_backend_from_server


@click.group()
def terminal():
    """Manage CAO terminals."""


@terminal.command("hibernated")
@click.option("--mine", "mine", default=None, help="Filter to this owner principal (mailbox id).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def hibernated(mine: str | None, as_json: bool) -> None:
    """F829 D5: list conversation identities in a recoverable/parked state.

    Alias of ``cao identity list`` under the terminals verb (blueprint D5:
    ``cao terminals hibernated``). Listing never mutates lifecycle.
    """
    import json as _json

    from cli_agent_orchestrator.clients.database import list_hibernated_identities

    rows = list_hibernated_identities(owner_principal=mine)
    if as_json:
        click.echo(_json.dumps(rows, default=str, indent=2))
        return
    if not rows:
        click.echo("No hibernated/detached/capture_unknown identities.")
        return
    for root in rows:
        uuid = root.get("provider_session_id")
        uuid8 = (uuid[:8] + "…") if uuid else "-"
        click.echo(
            f"{root['identity_key']}  {root['provider']:<11}  "
            f"{root.get('provider_namespace') or '-':<16}  {root['lifecycle']:<15}  "
            f"uuid={uuid8}  cur={root.get('current_terminal_id') or 'none'}"
        )


@terminal.command("restore")
@click.argument("terminal_id")
def restore(terminal_id: str):
    """Restore a deleted terminal from its snapshot.

    Creates a plain shell window in the original session at the original
    working directory and loads the saved scrollback history into the pane.
    The session must still exist.
    """
    snapshot_path = TERMINAL_LOG_DIR / f"{terminal_id}.snapshot.json"
    scrollback_path = TERMINAL_LOG_DIR / f"{terminal_id}.scrollback"

    if not snapshot_path.exists():
        raise click.ClickException(f"No snapshot found for terminal {terminal_id}")

    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise click.ClickException(f"Failed to read snapshot: {e}")

    session_name = snapshot["session_name"]
    working_directory = snapshot.get("working_directory")
    original_window = snapshot.get("window_name", terminal_id)

    # Verify session exists
    try:
        response = cao_http.get(f"/sessions/{session_name}")
        if response.status_code == 404:
            raise click.ClickException(
                f"Session '{session_name}' no longer exists. Cannot restore."
            )
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        # F241 (#64): raise_for_status() above raises HTTPError, which this arm
        # never caught — a served 4xx/5xx escaped as a bare traceback. Report
        # the status and body the server actually sent.
        raise click.ClickException(served_error_message(e))
    except requests.exceptions.ConnectionError:
        raise click.ClickException("Failed to connect to cao-server")

    # Create a plain window (no agent) in the existing session
    # Pass the scrollback file as the initial command: cat prints it as output,
    # then exec replaces cat with the user's login shell (tmux-resurrect pattern).
    window_name = f"restored-{original_window}"
    login_shell = os.environ.get("SHELL", "bash")

    if scrollback_path.exists():
        window_shell = f"cat '{scrollback_path}'; exec {login_shell} -l"
    else:
        window_shell = f"exec {login_shell} -l"

    try:
        sync_backend_from_server()
        get_backend().create_window(
            session_name,
            window_name,
            terminal_id,
            working_directory,
            window_shell=window_shell,
        )
    except Exception as e:
        raise click.ClickException(f"Failed to create window: {e}")

    click.echo(
        f"Restored terminal {terminal_id} as window '{window_name}' in session '{session_name}'"
    )
    click.echo(
        f"Original agent: {snapshot.get('agent_profile', 'N/A')} | Original window: {original_window}"
    )
    if working_directory:
        click.echo(f"Working directory: {working_directory}")
