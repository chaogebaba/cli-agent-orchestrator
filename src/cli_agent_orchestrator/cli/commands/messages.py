"""Inspect durable inbox delivery traces."""

import json
import os
from typing import cast
import click
import requests

from cli_agent_orchestrator.constants import API_BASE_URL, MCP_REQUEST_TIMEOUT
from cli_agent_orchestrator.cli.http import (
    bearer_headers, format_domain_detail, response_detail,
)


@click.group()
def messages():
    """Inspect inbox message delivery."""


@messages.command("trace")
@click.argument("message_id", type=int)
@click.option("--json", "as_json", is_flag=True)
def trace_cmd(message_id: int, as_json: bool) -> None:
    try:
        response = requests.get(f"{API_BASE_URL}/messages/{message_id}/trace",
                                timeout=MCP_REQUEST_TIMEOUT, headers=bearer_headers())
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


def _resolve_me(value: str) -> str:
    if value != "me":
        return value
    terminal_id = os.environ.get("CAO_TERMINAL_ID")
    if not terminal_id:
        raise click.UsageError("--to me requires CAO_TERMINAL_ID")
    response = requests.get(
        f"{API_BASE_URL}/mailboxes", headers=bearer_headers(), timeout=MCP_REQUEST_TIMEOUT
    )
    if response.status_code == 200:
        current = next((item for item in response.json().get("items", [])
                        if item.get("current_terminal_id") == terminal_id), None)
        if current:
            return cast(str, current["id"])
    return terminal_id


@messages.command("list")
@click.option("--to", "receiver", required=True)
@click.option("--since")
@click.option("--after-id", type=click.IntRange(min=0))
@click.option("--limit", type=click.IntRange(1, 100), default=25, show_default=True)
@click.option("--status", "status_value", type=click.Choice(
    ["pending", "delivering", "delivered", "delivery_failed", "failed"]
))
def list_cmd(receiver: str, since: str | None, after_id: int | None,
             limit: int, status_value: str | None) -> None:
    """List durable inbox messages in ascending id order."""
    params: dict[str, object] = {"to": _resolve_me(receiver), "limit": limit}
    if since is not None:
        params["since"] = since
    if after_id is not None:
        params["after_id"] = after_id
    if status_value is not None:
        params["status"] = status_value
    try:
        response = requests.get(
            f"{API_BASE_URL}/messages", params=params, headers=bearer_headers(),
            timeout=MCP_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise click.ClickException(f"could not reach cao-server: {exc}")
    if response.status_code != 200:
        detail = response_detail(response)
        raise click.ClickException(
            format_domain_detail(detail) if detail else f"list request failed: {response.text}"
        )
    click.echo(json.dumps(response.json(), indent=2))


@messages.command("ack")
@click.option("--up-to", "up_to_id", required=True, type=click.IntRange(min=1))
def ack_cmd(up_to_id: int) -> None:
    """Advance this supervisor's durable consumption cursor."""
    terminal_id = os.environ.get("CAO_TERMINAL_ID")
    if not terminal_id:
        raise click.UsageError("messages ack requires CAO_TERMINAL_ID")
    try:
        response = requests.post(
            f"{API_BASE_URL}/messages/ack",
            json={"terminal_id": terminal_id, "up_to_id": up_to_id},
            headers=bearer_headers(), timeout=MCP_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise click.ClickException(f"could not reach cao-server: {exc}")
    if response.status_code != 200:
        detail = response_detail(response)
        raise click.ClickException(
            format_domain_detail(detail) if detail else f"ack request failed: {response.text}"
        )
    click.echo(json.dumps(response.json(), indent=2))
