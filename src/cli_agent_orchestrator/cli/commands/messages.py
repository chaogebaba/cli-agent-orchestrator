"""Inspect durable inbox delivery traces."""

import json
import click
import requests

from cli_agent_orchestrator.constants import API_BASE_URL, MCP_REQUEST_TIMEOUT


@click.group()
def messages():
    """Inspect inbox message delivery."""


@messages.command("trace")
@click.argument("message_id", type=int)
@click.option("--json", "as_json", is_flag=True)
def trace_cmd(message_id: int, as_json: bool) -> None:
    try:
        response = requests.get(f"{API_BASE_URL}/messages/{message_id}/trace",
                                timeout=MCP_REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise click.ClickException(f"could not reach cao-server: {exc}")
    if response.status_code != 200:
        raise click.ClickException(f"trace request failed: {response.text}")
    body = response.json()
    if as_json:
        click.echo(json.dumps(body, indent=2))
        return
    click.echo(f"message {message_id}  status={body['message']['status']}")
    for attempt in body["attempts"]:
        click.echo(f"{attempt['started_at']}  {attempt['outcome'] or 'delivering':12} "
                   f"{attempt['attempt_uuid']}  {attempt.get('reason') or ''}")
