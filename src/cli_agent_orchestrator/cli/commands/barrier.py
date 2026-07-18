"""Inspect and cancel callback barriers."""

import json
import os

import click
import requests

from cli_agent_orchestrator.cli.http import bearer_headers, format_domain_detail, response_detail
from cli_agent_orchestrator.constants import MCP_REQUEST_TIMEOUT
from cli_agent_orchestrator.utils.http import CAOHttpClient

cao_http = CAOHttpClient(lambda: requests)


@click.group()
def barrier() -> None:
    """Inspect or cancel callback barriers."""


def _selector(barrier_id: int | None, label: str | None, owner: str | None) -> dict[str, object]:
    if (barrier_id is None) == (label is None):
        raise click.UsageError("provide exactly one of --id or --label")
    if barrier_id is not None:
        return {"barrier_id": barrier_id}
    resolved_owner = owner or os.environ.get("CAO_TERMINAL_ID")
    if not resolved_owner:
        raise click.UsageError("--owner is required for label lookup outside a CAO terminal")
    return {"barrier_label": label, "owner": resolved_owner}


def _emit(response: requests.Response) -> None:
    if response.status_code != 200:
        detail = response_detail(response)
        raise click.ClickException(format_domain_detail(detail) if detail else response.text)
    click.echo(json.dumps(response.json(), indent=2, default=str))


def _selector_options(function):
    function = click.option("--owner")(function)
    function = click.option("--label")(function)
    return click.option("--id", "barrier_id", type=click.IntRange(min=1))(function)


@barrier.command("status")
@_selector_options
def status_cmd(barrier_id: int | None, label: str | None, owner: str | None) -> None:
    response = cao_http.get(
        "/barriers/status",
        params=_selector(barrier_id, label, owner),
        headers=bearer_headers(),
        timeout=MCP_REQUEST_TIMEOUT,
    )
    _emit(response)


@barrier.command("cancel")
@_selector_options
@click.option("--yes", is_flag=True)
def cancel_cmd(
    barrier_id: int | None,
    label: str | None,
    owner: str | None,
    yes: bool,
) -> None:
    if not yes:
        raise click.UsageError("barrier cancel requires --yes")
    response = cao_http.post(
        "/barriers/cancel",
        params=_selector(barrier_id, label, owner),
        headers=bearer_headers(),
        timeout=MCP_REQUEST_TIMEOUT,
    )
    _emit(response)
