"""F829 D5: conversation-identity CLI verbs.

* ``cao identity list`` (alias behind ``cao terminal hibernated``) — canonical
  identities in a recoverable/parked state, optionally ``--mine``.
* ``cao identity diag <uuid|identity_key|terminal_id>`` — follow any id to its
  root and print the ordered event timeline + incarnations.
* ``cao identity attach <identity_key> <artifact>`` — owner-only explicit
  artifact attribution for a capture_unknown identity (D7).
* ``cao identity claim <identity_key>`` — an owner claims a NULL-owner / legacy
  root so it becomes resumable (supervisor ask 1 / legacy_unknown_owner).

These read/write the server's DB directly (same pattern as ``cao diag``), so
they run co-located with cao-server. Listing never mutates lifecycle (D6).
"""

from __future__ import annotations

import json as _json
from typing import Any

import click


def _fmt_row(root: dict[str, Any]) -> str:
    uuid = root.get("provider_session_id")
    uuid8 = (uuid[:8] + "…") if uuid else "-"
    return (
        f"{root['identity_key']}  {root['provider']:<11}  "
        f"{root.get('provider_namespace') or '-':<16}  {root['lifecycle']:<15}  "
        f"{(root.get('model') or '-')}·{(root.get('reasoning_effort') or '-')}  "
        f"uuid={uuid8}  cur={root.get('current_terminal_id') or 'none'}"
    )


@click.group()
def identity() -> None:
    """Inspect and recover durable conversation identities (F829)."""


@identity.command("list")
@click.option("--mine", "mine", default=None, help="Filter to this owner principal (mailbox id).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def identity_list(mine: str | None, as_json: bool) -> None:
    """List conversation identities in a recoverable/parked state."""
    from cli_agent_orchestrator.clients.database import list_hibernated_identities

    rows = list_hibernated_identities(owner_principal=mine)
    if as_json:
        click.echo(_json.dumps(rows, default=str, indent=2))
        return
    if not rows:
        click.echo("No hibernated/detached/capture_unknown identities.")
        return
    for root in rows:
        click.echo(_fmt_row(root))


@identity.command("diag")
@click.argument("identifier")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def identity_diag(identifier: str, as_json: bool) -> None:
    """Follow a uuid | identity_key | terminal_id to its root and print the timeline."""
    from cli_agent_orchestrator.clients.database import (
        get_conversation_events,
        get_conversation_incarnations,
        resolve_conversation_identity,
    )

    try:
        root = resolve_conversation_identity(identifier)
    except ValueError as exc:
        raise click.ClickException(str(exc))  # session_ambiguous
    if root is None:
        raise click.ClickException(f"No conversation identity resolves '{identifier}'.")
    key = root["identity_key"]
    incarnations = get_conversation_incarnations(key)
    events = get_conversation_events(key)
    if as_json:
        click.echo(
            _json.dumps(
                {"root": root, "incarnations": incarnations, "events": events},
                default=str,
                indent=2,
            )
        )
        return
    click.echo(_fmt_row(root))
    click.echo(f"  incarnations ({len(incarnations)}):")
    for inc in incarnations:
        click.echo(
            f"    {inc['terminal_id']}  {inc['lifecycle']:<7}  "
            f"created={inc.get('created_at')}  uuid={inc.get('provider_session_id') or '-'}"
        )
    click.echo(f"  timeline ({len(events)}):")
    for ev in events:
        click.echo(f"    {ev.get('created_at')}  {ev['event']:<24}  {ev.get('detail') or ''}")


@identity.command("attach")
@click.argument("identity_key")
@click.argument("artifact")
@click.option("--owner", "owner_principal", required=True, help="Your durable principal (mailbox).")
def identity_attach(identity_key: str, artifact: str, owner_principal: str) -> None:
    """Explicitly attribute an ARTIFACT to a capture_unknown identity (owner-only)."""
    from cli_agent_orchestrator.services.kiro_capture import attach_identity_artifact

    res = attach_identity_artifact(identity_key, artifact, owner_principal=owner_principal)
    status = res.get("status")
    if status == "attached":
        click.echo(f"attached: {identity_key} -> {res.get('provider_session_id')}")
    elif status == "not_owner":
        raise click.ClickException("not the owner of this identity")
    elif status == "no_root":
        raise click.ClickException(f"no conversation identity '{identity_key}'")
    else:
        raise click.ClickException(f"attach failed: {status}")


@identity.command("claim")
@click.argument("identity_key")
@click.option("--owner", "owner_principal", required=True, help="The claiming principal (mailbox).")
def identity_claim(identity_key: str, owner_principal: str) -> None:
    """Claim ownership of a NULL-owner / legacy identity so it becomes resumable."""
    from cli_agent_orchestrator.clients.database import claim_identity_owner

    res = claim_identity_owner(identity_key, owner_principal)
    status = res.get("status")
    if status == "claimed":
        click.echo(f"claimed: {identity_key} owner={owner_principal}")
    elif status == "already_owned":
        raise click.ClickException(
            f"already owned by {res.get('owner_principal')}; cannot reassign"
        )
    else:
        raise click.ClickException(f"claim failed: {status}")
