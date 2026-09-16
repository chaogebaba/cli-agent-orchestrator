"""``cao-orchestrator cert-status`` — the staleness detector F788 #645 found missing.

The measured incident: ``general``'s ``(kiro_cli, general)`` and ``(codex, general)``
PASS rows sat at ``position_sha 5eb08f4f9de16738`` while the store had moved to
``ca548c655b4ceb3e``. ``routing.cell_certified`` answered UNCERTIFIED, every
routing-driven assign for those providers was refused ``E-PROVIDER-UNCERTIFIED``, and
nothing said so until an assign failed — for weeks, while the fleet worked through the
explicit ``provider=`` bypass.

Every input to that failure was on disk the whole time. This command diffs each
recorded row against the pair the resolver computes now, on both axes, and exits 1
when any row has drifted. Pointed at a store by ``--positions-dir``/``--workspace``,
it is equally the pre-commit sweep and the operator's answer to "is this cell live?".
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

import click

from cli_agent_orchestrator.cli.orchestrator_commands.certify import resolve_positions_dir

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime is avoided on purpose
    from cli_agent_orchestrator.utils.clause_lint import CertFinding


@click.command("cert-status")
@click.option("--position", "-P", default=None, help="Only report this position.")
@click.option(
    "--axis",
    type=click.Choice(["provider", "herdr", "all"]),
    default="all",
    show_default=True,
    help="Which certification block(s) to sweep.",
)
@click.option(
    "--workspace",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Workspace root holding profiles/positions (default: current directory).",
)
@click.option(
    "--positions-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Sweep this positions store instead of the workspace/installed one.",
)
@click.option(
    "--warn-only",
    is_flag=True,
    default=False,
    help="Report drift but exit 0 (the pre-commit mode; block is a later ratchet).",
)
def cert_status(
    position: Optional[str],
    axis: str,
    workspace: Optional[Path],
    positions_dir: Optional[Path],
    warn_only: bool,
) -> None:
    """Report certification rows whose recorded shas no longer match the store."""
    from cli_agent_orchestrator.utils.clause_lint import lint_certifications

    store = resolve_positions_dir(workspace, positions_dir)
    findings = lint_certifications(store, axis=axis)
    if position:
        findings = [f for f in findings if f.position == position]

    scope = f"{store}" + (f" (position={position})" if position else "")
    if not findings:
        click.echo(f"cert-status: all rows current — {scope}")
        return

    click.echo(f"cert-status: {len(findings)} drifted row(s) — {scope}")
    for finding in _sorted(findings):
        click.echo(f"  {finding.message()}")
    click.echo(
        "re-certify with: cao-orchestrator certify --position <P> --provider <X> "
        "--axis <provider|herdr> --evidence <file>"
    )
    if warn_only:
        return
    raise SystemExit(1)


def _sorted(findings: List["CertFinding"]) -> List["CertFinding"]:
    return sorted(findings, key=lambda f: (f.position, f.axis, f.provider, f.code))
