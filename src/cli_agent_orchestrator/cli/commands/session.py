"""Session commands for CLI Agent Orchestrator."""

import json
import os
import sys
import time
from urllib.parse import quote

import click
import requests

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils.terminal import poll_until_done

# Default poll timeout for sync send (seconds). Pass --timeout to override.
_DEFAULT_SEND_TIMEOUT = 300


def _get_sessions():
    response = requests.get(f"{API_BASE_URL}/sessions")
    response.raise_for_status()
    return response.json()


def _get_terminals(session_name):
    response = requests.get(f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/terminals")
    response.raise_for_status()
    return response.json()


def _get_terminal(terminal_id):
    response = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}")
    response.raise_for_status()
    return response.json()


def _get_terminal_output(terminal_id):
    response = requests.get(
        f"{API_BASE_URL}/terminals/{terminal_id}/output", params={"mode": "last"}
    )
    response.raise_for_status()
    return response.json()


def _resolve_conductor(session_name):
    terminals = _get_terminals(session_name)
    if not terminals:
        raise click.ClickException(f"No terminals found for session '{session_name}'")
    return terminals[0], terminals


@click.group()
def session():
    """Manage CAO sessions."""


@session.command("start")
@click.argument("session_name", required=False)
@click.option("--agents", required=True)
@click.option("--provider")
@click.option("--cwd", "working_directory")
@click.option("--tools", "allowed_tools")
@click.option("--env", "env_pairs", multiple=True)
@click.option("--allow-incomplete-brief", is_flag=True)
@click.option("--memory", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def start(session_name, agents, provider, working_directory, allowed_tools,
          env_pairs, allow_incomplete_brief, memory, as_json):
    """Start a CAO session through the canonical lifecycle API."""
    from cli_agent_orchestrator.cli.commands.launch import _parse_env_pairs
    params = {"agent_profile": agents}
    if session_name:
        params["session_name"] = session_name
    if provider:
        params["provider"] = provider
    if working_directory:
        params["working_directory"] = working_directory
    if allowed_tools:
        params["allowed_tools"] = allowed_tools
    if allow_incomplete_brief:
        params["allow_incomplete_brief"] = "true"
    if memory:
        params["memory"] = "true"
    kwargs = {"params": params}
    if env_pairs:
        kwargs["json"] = {"env_vars": _parse_env_pairs(env_pairs)}
    response = requests.post(f"{API_BASE_URL}/sessions/start", **kwargs)
    payload = response.json()
    if response.status_code == 422 and payload.get("bootstrap", {}).get("status") == "seed_failed":
        click.echo(json.dumps(payload, indent=2) if as_json else
                   f"bootstrap failed [{payload['bootstrap']['error_code']}]", err=True)
        raise click.exceptions.Exit(2)
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise click.ClickException(f"session start failed: {exc}")
    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"Session created: {payload['session']['name']}")
        click.echo(f"Terminal created: {payload['supervisor_terminal']['name']}")


@session.command("manifest")
@click.option("--session", "session_name")
@click.option("--json", "as_json", is_flag=True)
@click.option("--brief", is_flag=True)
def manifest(session_name, as_json, brief):
    """Print the canonical session manifest or its compact Markdown brief."""
    if as_json and brief:
        raise click.ClickException("choose exactly one of --json or --brief")
    if not session_name:
        terminal_id = os.environ.get("CAO_TERMINAL_ID")
        if not terminal_id:
            raise click.ClickException("--session is required outside a CAO terminal")
        try:
            session_name = _get_terminal(terminal_id)["session_name"]
        except requests.RequestException as exc:
            raise click.ClickException(f"could not resolve caller session: {exc}")
    try:
        response = requests.get(f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/manifest")
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise click.ClickException(f"failed to fetch session manifest: {exc}")
    if brief:
        from cli_agent_orchestrator.services.session_manifest_service import render_session_brief
        click.echo(render_session_brief(payload))
    else:
        click.echo(json.dumps(payload, indent=2))


@session.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_sessions(as_json):
    """List all active CAO sessions."""
    try:
        sessions = _get_sessions()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")

    if not sessions:
        if as_json:
            click.echo("[]")
        else:
            click.echo("No active sessions")
        return

    rows = []
    for s in sessions:
        try:
            terminals = _get_terminals(s["name"])
            conductor = terminals[0] if terminals else None
            if conductor:
                conductor = _get_terminal(conductor["id"])
            rows.append((s["name"], conductor, len(terminals)))
        except requests.exceptions.RequestException:
            continue

    if as_json:
        result = []
        for name, conductor, terminal_count in rows:
            result.append(
                {
                    "session": name,
                    "conductor": (
                        {
                            "id": conductor["id"],
                            "agent_profile": conductor.get("agent_profile"),
                            "provider": conductor.get("provider"),
                            "status": conductor.get("status"),
                        }
                        if conductor
                        else None
                    ),
                    "terminal_count": terminal_count,
                }
            )
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"{'SESSION':<25} {'CONDUCTOR':<12} {'STATUS':<15} {'TERMINALS':<10}")
        click.echo("-" * 65)
        for name, conductor, terminal_count in rows:
            conductor_id = conductor["id"] if conductor else "N/A"
            status = conductor.get("status", "N/A") if conductor else "N/A"
            click.echo(f"{name:<25} {conductor_id:<12} {status:<15} {terminal_count:<10}")


@session.command("recover")
@click.argument("session_name")
@click.option("--reason", required=True, type=click.Choice(["provider-reauth", "epoch"]))
@click.option("--provider", default="codex", type=click.Choice(["codex", "grok_cli"]))
@click.option("--terminal", "terminal_ids", multiple=True)
@click.option("--interrupt", is_flag=True)
@click.option("--acknowledge-ownership", is_flag=True)
@click.option("--base", "base_names", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def recover(
    session_name, reason, provider, terminal_ids, interrupt,
    acknowledge_ownership, base_names, as_json,
):
    """Explicitly rebind provider sessions after authentication changes."""
    if acknowledge_ownership and len(terminal_ids) != 1:
        raise click.ClickException(
            "--acknowledge-ownership requires exactly one --terminal selector"
        )
    if reason == "epoch" and (terminal_ids or interrupt or acknowledge_ownership):
        raise click.ClickException(
            "epoch recovery rejects --terminal, --interrupt, and --acknowledge-ownership"
        )
    if reason == "provider-reauth" and base_names:
        raise click.ClickException("provider-reauth rejects --base")
    payload = {
        "reason": reason,
        "provider": provider,
        "terminal_ids": list(terminal_ids),
        "interrupt": interrupt,
        "acknowledge_ownership": acknowledge_ownership,
    }
    if reason == "epoch":
        payload["base_names"] = list(base_names)
    try:
        response = requests.post(
            f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/recover",
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise click.ClickException(f"provider recovery failed: {exc}")
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    if reason == "provider-reauth":
        click.echo(f"Recovery: {result.get('session', session_name)} ({provider})")
    else:
        click.echo(f"Recovery: {result.get('session', session_name)} (epoch)")
    for item in result.get("results", []):
        detail = f" [{item['error_code']}]" if item.get("error_code") else ""
        reconciliation = " reconciliation-required" if item.get(
            "requires_supervisor_reconciliation") else ""
        label = item.get("terminal_id") or item.get("base")
        click.echo(f"{label}: {item['status']}{detail}{reconciliation}")
    if reason == "epoch":
        for item in result.get("respawn_candidates", []):
            click.echo(
                f"offer {item['intent_id']}: {item['profile']} from {item['base']} "
                f"[{item['base_state']}]"
            )
    if result.get("manifest_error"):
        click.echo(f"manifest: failed [{result['manifest_error']}]", err=True)


@session.command("close")
@click.argument("session_name")
@click.option("--keep-bases", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def close(session_name, keep_bases, force, as_json):
    """Close a session and mechanically settle bases and warm intents."""
    try:
        response = requests.post(
            f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/close",
            params={"keep_bases": str(keep_bases).lower(), "force": str(force).lower()},
        )
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise click.ClickException(f"session close failed: {exc}")
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    click.echo(f"Closed: {session_name} session_closed={str(result['session_closed']).lower()}")
    for item in result.get("terminals", []):
        click.echo(f"{item['terminal_id']}: {item['status']}")
    for item in result.get("bases", []):
        click.echo(f"base {item['base']}: {item['status']}")
    intents = result.get("intents", {})
    click.echo(f"intents: removed={intents.get('removed', 0)} retained={intents.get('retained', 0)}")


@session.command()
@click.argument("session_name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def status(session_name, as_json):
    """Show the lifecycle status/v1 projection."""
    try:
        response = requests.get(
            f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/status"
        )
        response.raise_for_status()
        result = response.json()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"failed to fetch session status: {e}")
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    click.echo(f"Session: {result['session']['name']}")
    click.echo(f"Backend present: {str(result['backend_present']).lower()}")
    click.echo(f"Epoch: {result['epoch']['count'] if result['epoch'] else 'none'}")
    click.echo(f"Ready bases: {len(result['ready_bases'])}")
    click.echo(f"Warm intents: {len(result['warm_intents'])}")
    click.echo(f"Quarantined: {len(result['quarantined'])}")
    click.echo("Ledger: unavailable")


@session.command()
@click.argument("session_name")
@click.argument("message")
@click.option("--terminal", "terminal_id", help="Send to a specific terminal ID")
@click.option(
    "--async", "is_async", is_flag=True, help="Send and return immediately without waiting"
)
@click.option(
    "--timeout",
    "timeout",
    type=int,
    default=None,
    help=f"Timeout in seconds (default: {_DEFAULT_SEND_TIMEOUT}s; ignored with --async)",
)
def send(session_name, message, terminal_id, is_async, timeout):
    """Send a message to a session's conductor (or specific terminal)."""
    try:
        if terminal_id:
            target_id = terminal_id
        else:
            conductor, _ = _resolve_conductor(session_name)
            target_id = conductor["id"]

        status_resp = requests.get(f"{API_BASE_URL}/terminals/{target_id}")
        status_resp.raise_for_status()
        current_status = status_resp.json().get("status")
        # "completed" is a valid pre-send state: the terminal has finished its
        # previous task and is ready to accept a new message.
        if current_status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
            raise click.ClickException(
                f"Terminal {target_id} is currently {current_status}. Wait for it to finish before sending."
            )

        response = requests.post(
            f"{API_BASE_URL}/terminals/{target_id}/input",
            params={"message": message},
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")

    if is_async:
        click.echo(f"Message sent to terminal {target_id}")
        return

    time.sleep(3)
    effective_timeout = timeout if timeout is not None else _DEFAULT_SEND_TIMEOUT
    interrupted = False
    try:
        poll_until_done(target_id, effective_timeout)
    except KeyboardInterrupt:
        interrupted = True

    try:
        output_resp = requests.get(
            f"{API_BASE_URL}/terminals/{target_id}/output",
            params={"mode": "last"},
        )
        output_resp.raise_for_status()
        output = output_resp.json().get("output", "")
        if output:
            click.echo(output)
    except requests.exceptions.RequestException:
        pass

    if interrupted:
        sys.exit(130)
