"""Workspace handoff-ledger checks."""

import re
from pathlib import Path

import click

from cli_agent_orchestrator.services.verification_service import find_workspace_file


@click.group()
def ledger() -> None:
    """Inspect the workspace handoff ledger."""


@ledger.command("check")
def check() -> None:
    """Warn about stale re-entry text and count pending ledger rows."""
    path = find_workspace_file(Path.cwd(), "HANDOFF.md")
    if path is None:
        raise click.ClickException("HANDOFF.md not found in cwd or any parent")
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^## POST-RESTART RE-ENTRY.*?(?=^## )", text, re.M | re.S)
    header = match.group(0) if match else ""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 5 and cells[0].lower() != "feature":
            rows.append(cells)
    stale = []
    pending = 0
    for cells in rows:
        feature, status = cells[0], cells[4].lower()
        if status in {"drained-pass", "drained-fail"}:
            if feature and feature.casefold() in header.casefold():
                stale.append(feature)
        else:
            pending += 1
    for feature in stale:
        click.echo(f"warning: POST-RESTART RE-ENTRY names drained feature: {feature}")
    click.echo(f"pending-row count: {pending}")

