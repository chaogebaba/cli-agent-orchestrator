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
    """Follow a uuid | identity_key | terminal_id to its root and print the timeline.

    F829 A2.4 — five distinguished states, NEVER a manufactured root/owner/success
    timeline:

    1. ``unknown_handle``            — nothing (no incarnation row, no root).
    2. ``incarnation_present_root_absent`` — a ``terminal_identity`` row exists but
       carries no ``identity_key`` (F631 registered, F829 root never minted).
    3. ``dangling_root_link``        — the incarnation's ``identity_key`` points at a
       root that does not exist.
    4. ``root_present_owner_unknown``— the root exists with ``owner_principal`` NULL
       (a NULL-owner/legacy root; claimable via ``cao identity claim``).
    5. ``binding_or_artifact_missing``— the root exists and is owned but has no
       captured ``provider_session_id`` (or its artifact is unresolved).

    For a MISSING root (states 1-3) the output carries ``identity_key: null``,
    the provider + history lifecycle from the incarnation row when present, the
    root/link status, ``resumable: false``, the reason, and only sanitized
    creation evidence — never a fabricated root.
    """
    from cli_agent_orchestrator.clients.database import (
        get_conversation_events,
        get_conversation_incarnations,
        get_terminal_identity,
        resolve_conversation_identity,
    )

    try:
        root = resolve_conversation_identity(identifier)
    except ValueError as exc:
        raise click.ClickException(str(exc))  # session_ambiguous

    if root is None:
        # No resolvable root. Distinguish the missing-root taxonomy from the
        # terminal_identity history (A2.4) instead of a flat "not found".
        incarnation = get_terminal_identity(identifier)
        if incarnation is None:
            state = "unknown_handle"
            link_status = "no_incarnation_row"
        elif not incarnation.get("identity_key"):
            state = "incarnation_present_root_absent"
            link_status = "identity_key_null"
        else:
            state = "dangling_root_link"
            link_status = f"identity_key={incarnation.get('identity_key')} -> (root missing)"
        missing = {
            "identity_key": None,
            "state": state,
            "resumable": False,
            "reason": "identity_missing",
            "provider": incarnation.get("provider") if incarnation else None,
            "history_lifecycle": incarnation.get("lifecycle") if incarnation else None,
            "link_status": link_status,
            "creation_evidence": (
                {
                    "terminal_id": incarnation.get("terminal_id"),
                    "created_at": incarnation.get("created_at"),
                    "cwd": incarnation.get("cwd"),
                    "session_name": incarnation.get("session_name"),
                }
                if incarnation
                else None
            ),
        }
        if as_json:
            click.echo(_json.dumps(missing, default=str, indent=2))
            return
        click.echo(f"identity_key: null  state={state}  resumable=false  reason=identity_missing")
        click.echo(f"  provider={missing['provider'] or '-'}  link={link_status}")
        click.echo(f"  history_lifecycle={missing['history_lifecycle'] or '-'}")
        if missing["creation_evidence"]:
            click.echo(f"  creation_evidence={missing['creation_evidence']}")
        return

    key = root["identity_key"]
    # States 4/5: the root exists — annotate owner/binding for the operator.
    if root.get("owner_principal") is None:
        root_state = "root_present_owner_unknown"
    elif not root.get("provider_session_id"):
        root_state = "binding_or_artifact_missing"
    else:
        root_state = "root_present"
    incarnations = get_conversation_incarnations(key)
    events = get_conversation_events(key)
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "root": root,
                    "state": root_state,
                    "incarnations": incarnations,
                    "events": events,
                },
                default=str,
                indent=2,
            )
        )
        return
    click.echo(_fmt_row(root))
    click.echo(f"  state={root_state}  owner={root.get('owner_principal') or 'none'}")
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


@identity.command("release")
@click.argument("identity_key")
@click.option("--owner", "owner_principal", required=True, help="The owning principal (mailbox).")
def identity_release(identity_key: str, owner_principal: str) -> None:
    """F829 A2.5: owner-guarded release of a LEAKED resume claim (operator recovery).

    Clears a stuck ``resume_claim`` (a resume attempt that died before the claim
    was reconciled by its TTL) ONLY when ``--owner`` matches the root's recorded
    owner. A recoverer cannot release a claim on a conversation it does not own.
    """
    from cli_agent_orchestrator.clients.database import release_resume_claim_owned

    res = release_resume_claim_owned(identity_key, owner_principal)
    if res.get("released"):
        click.echo(f"released: {identity_key} (claim cleared)")
        return
    reason = res.get("reason")
    if reason == "not_owner":
        raise click.ClickException("not the owner of this identity")
    if reason == "unknown_identity":
        raise click.ClickException(f"no conversation identity '{identity_key}'")
    if reason == "no_active_claim":
        raise click.ClickException("no active resume claim to release")
    if reason == "claimant_live":
        raise click.ClickException(
            "the resume claim is still within its TTL (claimant live or of "
            "uncertain liveness); refusing to release a claim that may race a "
            "resume in flight — retry after resume.claim_ttl_s elapses"
        )
    raise click.ClickException(f"release failed: {reason}")


@identity.command("backfill-owners")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def identity_backfill_owners(as_json: bool) -> None:
    """F829 A2.2: re-run the provenance-checked owner backfill (operator entry point).

    The automatic backfill runs once at server start; this re-runs the SAME
    idempotent logic on demand so an operator can recover a terminal-fallback
    root that was skipped then (e.g. it held an active resume claim). Recovery
    path: ``cao identity release`` to clear a stuck claim, then this. There is NO
    owner-override on ``cao identity claim`` — this is the sanctioned recovery.
    """
    from cli_agent_orchestrator.clients.database import run_owner_backfill_operator

    tally = run_owner_backfill_operator()
    if as_json:
        click.echo(_json.dumps(tally, indent=2))
        return
    click.echo(
        f"owner backfill complete: backfilled={tally['backfilled']} "
        f"skipped={tally['skipped']} concurrent={tally['concurrent']}"
    )
