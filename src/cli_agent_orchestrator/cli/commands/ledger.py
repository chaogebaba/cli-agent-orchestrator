"""Workspace handoff-ledger checks."""

import re
from pathlib import Path

import click

from cli_agent_orchestrator.services.verification_service import find_workspace_file

# Recognized status tokens (case-insensitive).
_DRAINED_STATUSES = {"drained-pass", "drained-fail", "verified"}
_PENDING_STATUSES = {"pending", "pending-activation"}

# Canonical ledger heading pattern (S2):
# - "## Live ledger" (exact)
# - "## Live ledger additions" optionally followed by whitespace/suffix
# Must NOT match "## Live ledger archive".
_LEDGER_HEADING_RE = re.compile(
    r"^## Live ledger(?:\s+additions(?:\s.*)?)?\s*$",
    re.M,
)


def _extract_ledger_section(text: str) -> str | None:
    """Return text under the LAST canonical ## Live ledger heading, up to the next H2 or EOF."""
    # Find all canonical headings; use the last one (newest appended ledger).
    matches = list(_LEDGER_HEADING_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    after = text[last.end():]
    # Find next H2 boundary.
    next_h2 = re.search(r"^## ", after, re.M)
    if next_h2:
        return after[: next_h2.start()]
    return after


def _parse_table_rows(section: str) -> list[tuple[str, str]]:
    """Parse canonical table rows -> list of (feature, status)."""
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        # Header row detection (case-insensitive "feature" in first cell).
        if len(cells) >= 5 and cells[0].lower() == "feature":
            continue
        if len(cells) >= 5:
            rows.append((cells[0], cells[4]))
    return rows


def _parse_bullet_rows(section: str) -> list[tuple[str, str]]:
    """Parse legacy bullet rows carrying `status: <token>`."""
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("-", "*")):
            continue
        status_match = re.search(r"status:\s*(\S+)", stripped, re.IGNORECASE)
        if status_match:
            # Feature name is the first meaningful token(s) after the bullet.
            # Typically: "- F213 ... status: PENDING"
            feature_match = re.match(r"[-*]\s+(.+?)(?:\s+\.{2,}|\s+status:)", stripped, re.IGNORECASE)
            feature = feature_match.group(1).strip() if feature_match else ""
            rows.append((feature, status_match.group(1)))
    return rows


def _feature_in_reentry(feature: str, reentry_text: str) -> bool:
    """Check if feature appears as a whole token in the re-entry text (B1 exact-match)."""
    pattern = r"(?<![A-Za-z0-9])" + re.escape(feature) + r"(?![A-Za-z0-9])"
    return bool(re.search(pattern, reentry_text, re.I))


@click.group()
def ledger() -> None:
    """Inspect the workspace handoff ledger."""


@ledger.command("check")
def check() -> None:
    """Warn about stale re-entry text and count pending ledger rows."""
    path = find_workspace_file(Path.cwd(), "orchestrator/HANDOFF.md")
    if path is None:
        path = find_workspace_file(Path.cwd(), "HANDOFF.md")
    if path is None:
        raise click.ClickException(
            "HANDOFF.md not found in cwd or any parent "
            "(checked orchestrator/HANDOFF.md and HANDOFF.md)"
        )
    text = path.read_text(encoding="utf-8")

    # Extract POST-RESTART RE-ENTRY section for stale-name check.
    reentry_match = re.search(r"^## POST-RESTART RE-ENTRY.*?(?=^## |\Z)", text, re.M | re.S)
    reentry_text = reentry_match.group(0) if reentry_match else ""

    # Scope to the live-ledger section.
    ledger_section = _extract_ledger_section(text)
    if ledger_section is None:
        click.echo("warning: no live ledger section found in HANDOFF.md")
        click.echo("pending-row count: 0")
        return

    # Parse both formats; table takes precedence if present.
    rows = _parse_table_rows(ledger_section)
    if not rows:
        rows = _parse_bullet_rows(ledger_section)

    stale: list[str] = []
    pending = 0
    for feature, raw_status in rows:
        status = raw_status.strip().lower()
        if status in _DRAINED_STATUSES:
            if feature and _feature_in_reentry(feature, reentry_text):
                stale.append(feature)
        elif status in _PENDING_STATUSES:
            pending += 1
        else:
            click.echo(f"warning: unrecognized ledger status '{raw_status.strip()}' for '{feature}'")

    for feature in stale:
        click.echo(f"warning: POST-RESTART RE-ENTRY names drained feature: {feature}")
    click.echo(f"pending-row count: {pending}")
