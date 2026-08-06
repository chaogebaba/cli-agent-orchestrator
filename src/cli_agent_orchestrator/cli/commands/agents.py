"""Fleet-oriented agent commands."""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any
from urllib.parse import quote

import click
import requests

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils.http import EndpointConfigurationError, cao_http
from cli_agent_orchestrator.utils.session_lookup import resolve_session_name

_CLI_TIMEOUT = 10.0
_MISSING = "—"


@click.group()
def agents() -> None:
    """Inspect the agents in a session."""


def _format_age(value: float | None) -> str:
    if value is None:
        return _MISSING
    seconds = max(0, int(value))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 60 * 60:
        return f"{seconds // 60}m"
    if seconds < 48 * 60 * 60:
        return f"{seconds // (60 * 60)}h"
    return f"{seconds // (24 * 60 * 60)}d"


def _render_fleet(payload: dict[str, Any]) -> None:
    terminals = payload["terminals"]
    counts = Counter(row["status"] for row in terminals)
    header = f"Session: {payload['session_name']} — {len(terminals)} terminals"
    for status in TerminalStatus:
        count = counts.get(status.value, 0)
        if count:
            header += f" · {count} {status.value}"
    orphan_count = sum(bool(row["orphan"]) for row in terminals)
    if orphan_count:
        header += f" · {orphan_count} orphan"
    click.echo(header)

    headings = ("IDX", "ID", "PROFILE", "STATUS", "AGE", "PARENT", "WINDOW", "⚠")
    rows = []
    for terminal in terminals:
        flagged = (
            terminal["status"] == TerminalStatus.ERROR.value
            or terminal["orphan"]
            or terminal["reparented_from"] is not None
        )
        rows.append(
            (
                str(terminal["window_index"]) if terminal["window_index"] is not None else _MISSING,
                terminal["id"],
                terminal["profile"] or _MISSING,
                terminal["status"],
                _format_age(terminal["since_last_input"]),
                terminal["parent_id"] or _MISSING,
                terminal["window_name"] or _MISSING,
                "⚠" if flagged else "",
            )
        )
    widths = [
        max(len(headings[index]), *(len(row[index]) for row in rows))
        for index in range(len(headings))
    ]
    click.echo("  ".join(value.ljust(widths[index]) for index, value in enumerate(headings)))
    for row in rows:
        click.echo(
            "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
        )


def _resolution_error(exc: Exception, terminal_id: str | None) -> click.ClickException:
    if isinstance(exc, EndpointConfigurationError):
        return click.ClickException(f"CAO endpoint binding is invalid: {exc}")
    if isinstance(exc, (requests.HTTPError, KeyError)):
        return click.ClickException(
            f"could not resolve the session for terminal {terminal_id or _MISSING}"
        )
    if isinstance(exc, ValueError):
        return click.ClickException(str(exc))
    return click.ClickException(f"could not connect to cao-server: {exc}")


@agents.command()
@click.argument("session_name", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output the raw fleet payload as JSON.")
def status(session_name: str | None, as_json: bool) -> None:
    """Show the live agent roster for a session."""
    terminal_id = os.environ.get("CAO_TERMINAL_ID")
    try:
        resolved_session = resolve_session_name(session_name, timeout=_CLI_TIMEOUT)
    except Exception as exc:
        raise _resolution_error(exc, terminal_id) from exc

    try:
        response = cao_http.get(
            f"/sessions/{quote(resolved_session, safe='')}/fleet", timeout=_CLI_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
    except EndpointConfigurationError as exc:
        raise click.ClickException(f"CAO endpoint binding is invalid: {exc}") from exc
    except requests.HTTPError as exc:
        raise click.ClickException(
            f"could not fetch fleet for session '{resolved_session}': {exc}"
        ) from exc
    except Exception as exc:
        raise click.ClickException(f"could not connect to cao-server: {exc}") from exc

    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        _render_fleet(payload)
