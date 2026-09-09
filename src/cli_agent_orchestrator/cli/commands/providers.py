"""F829 D10: provider-capability CLI verbs.

* ``cao providers capabilities [--provider <p>]`` — the declared∧measured
  capability read-out. For each provider and each of the four capability axes
  ({fork, resume, capture, artifact_locate}) it prints the DECLARED flag, the
  MEASURED evidence state (passed|failed|unknown|unmeasured), and the derived
  ADVERTISED verdict (declaration ∧ passing evidence). Advertised is the
  release-time gate; a declared-but-unmeasured axis is NOT advertised.

``cao providers probe`` is a follow-up (blueprint D10 new-verb list): the D9
probe harness populates the evidence rows this read-out renders. This command
is read-only over the same DB cao-server uses (co-located, like ``cao diag`` /
``cao identity``); it never mutates evidence.
"""

from __future__ import annotations

import json as _json
from typing import Any

import click

from cli_agent_orchestrator.services.capability_evidence import (
    CAPABILITY_KEYS,
    EvidenceState,
    provider_declares,
)

# The A1 intended-resumable provider matrix (kept explicit so removing a
# declaration cannot silently drop a provider from the read-out).
_PROVIDERS = ("codex", "kiro_cli", "claude_code", "pi_cli")
# Deterministic axis order for display.
_AXES = ("fork", "resume", "capture", "artifact_locate")


def _measured_state(provider: str, operation: str) -> str:
    """The measured evidence state string for an axis, or 'unmeasured'."""
    from cli_agent_orchestrator.clients.database import get_capability_evidence

    row = get_capability_evidence(provider, operation)
    if not row:
        return "unmeasured"
    return str(row.get("state") or "unmeasured")


def _advertised(declared: bool, measured: str) -> bool:
    """Declared ∧ passing evidence (D10 advertising gate)."""
    return declared and measured == EvidenceState.PASSED.value


@click.group()
def providers() -> None:
    """Inspect provider capabilities (declared ∧ measured) (F829 D10)."""


@providers.command("capabilities")
@click.option("--provider", "provider", default=None, help="Scope to one provider.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def capabilities(provider: str | None, as_json: bool) -> None:
    """Declared ∧ measured capability read-out (advertised = declaration ∧ passing)."""
    assert set(_AXES) == CAPABILITY_KEYS  # display order must cover every axis
    targets = [provider] if provider else list(_PROVIDERS)
    rows: list[dict[str, Any]] = []
    for prov in targets:
        for axis in _AXES:
            declared = provider_declares(prov, axis)
            measured = _measured_state(prov, axis)
            rows.append(
                {
                    "provider": prov,
                    "capability": axis,
                    "declared": declared,
                    "measured": measured,
                    "advertised": _advertised(declared, measured),
                }
            )
    if as_json:
        click.echo(_json.dumps(rows, indent=2))
        return
    click.echo(
        f"{'provider':<12}  {'capability':<15}  {'declared':<8}  {'measured':<11}  advertised"
    )
    for r in rows:
        click.echo(
            f"{r['provider']:<12}  {r['capability']:<15}  "
            f"{str(r['declared']).lower():<8}  {r['measured']:<11}  "
            f"{str(r['advertised']).lower()}"
        )
