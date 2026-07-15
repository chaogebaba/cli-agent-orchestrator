"""Operator commands for provider-session fork bases."""

import json

import click
import requests

from cli_agent_orchestrator.constants import API_BASE_URL


@click.group()
def base() -> None:
    """Manage fork-base registrations."""


@base.command("register")
@click.argument("name")
@click.option(
    "--provider", required=True, type=click.Choice(["codex", "grok_cli"])
)
@click.option("--uuid", "session_uuid", required=True)
@click.option("--cwd", required=True)
@click.option("--profile", required=True)
@click.option("--summary")
def register(
    name: str,
    provider: str,
    session_uuid: str,
    cwd: str,
    profile: str,
    summary: str | None,
) -> None:
    """Register stored provider history as a global, source-less base."""
    payload = {
        "name": name,
        "provider": provider,
        "session_uuid": session_uuid,
        "cwd": cwd,
        "profile": profile,
    }
    if summary is not None:
        payload["summary"] = summary
    try:
        response = requests.post(f"{API_BASE_URL}/bases/register", json=payload)
    except requests.RequestException as exc:
        raise click.ClickException(f"base registration failed: {exc}") from exc

    if response.status_code == 400:
        detail = response.json().get("detail", {})
        if isinstance(detail, dict) and detail.get("code") and detail.get("message"):
            click.echo(f"{detail['code']}: {detail['message']}", err=True)
            raise click.exceptions.Exit(1)
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise click.ClickException(f"base registration failed: {exc}") from exc
    click.echo(json.dumps(response.json(), indent=2, sort_keys=True))
