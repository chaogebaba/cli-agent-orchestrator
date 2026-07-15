"""Operator lifecycle for durable supervisor mailboxes."""

import json

import click
import requests

from cli_agent_orchestrator.cli.http import bearer_headers, format_domain_detail, response_detail
from cli_agent_orchestrator.constants import API_BASE_URL, MCP_REQUEST_TIMEOUT


@click.group()
def mailbox() -> None:
    """Inspect and delete durable supervisor mailboxes."""


@mailbox.command("list")
def list_cmd() -> None:
    response = requests.get(
        f"{API_BASE_URL}/mailboxes", headers=bearer_headers(), timeout=MCP_REQUEST_TIMEOUT
    )
    if response.status_code != 200:
        detail = response_detail(response)
        raise click.ClickException(format_domain_detail(detail) if detail else response.text)
    click.echo(json.dumps(response.json(), indent=2))


@mailbox.command("delete")
@click.option("--session", "session_name", required=True)
@click.option("--role", type=click.Choice(["supervisor"]), required=True)
@click.option("--yes", is_flag=True)
def delete_cmd(session_name: str, role: str, yes: bool) -> None:
    if not yes:
        raise click.UsageError("mailbox delete requires --yes")
    listing = requests.get(
        f"{API_BASE_URL}/mailboxes", headers=bearer_headers(), timeout=MCP_REQUEST_TIMEOUT
    )
    if listing.status_code != 200:
        detail = response_detail(listing)
        raise click.ClickException(format_domain_detail(detail) if detail else listing.text)
    row = next((item for item in listing.json().get("items", [])
                if item.get("session_name") == session_name and item.get("role") == role), None)
    if row is None:
        raise click.ClickException("unknown_mailbox: unknown mailbox")
    response = requests.delete(
        f"{API_BASE_URL}/mailboxes/{row['id']}", headers=bearer_headers(),
        timeout=MCP_REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        detail = response_detail(response)
        raise click.ClickException(format_domain_detail(detail) if detail else response.text)
    click.echo(json.dumps(response.json(), indent=2))
