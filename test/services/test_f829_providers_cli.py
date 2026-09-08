"""F829 D10: `cao providers capabilities` read-out is registered and renders the
declared∧measured advertising view over the real evidence store."""

from __future__ import annotations

import json

from click.testing import CliRunner


def test_providers_cli_registered():
    from cli_agent_orchestrator.cli.commands.providers import providers
    from cli_agent_orchestrator.cli.main import cli

    assert "capabilities" in providers.commands
    assert "providers" in cli.commands


def test_capabilities_readout_reflects_declaration_and_measurement(real_sqlite_env):
    """Declared∧measured: codex declares resume; once a passing row is recorded
    the read-out shows advertised=true for that axis; kiro (guarded, unmeasured)
    shows advertised=false."""
    from cli_agent_orchestrator.cli.commands.providers import capabilities
    from cli_agent_orchestrator.clients import database as d

    d.record_capability_evidence("codex", "resume", "passed")

    runner = CliRunner()
    result = runner.invoke(capabilities, ["--provider", "codex", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    resume_row = next(r for r in rows if r["capability"] == "resume")
    assert resume_row["declared"] is True
    assert resume_row["measured"] == "passed"
    assert resume_row["advertised"] is True

    result_k = runner.invoke(capabilities, ["--provider", "kiro_cli", "--json"])
    rows_k = json.loads(result_k.output)
    resume_k = next(r for r in rows_k if r["capability"] == "resume")
    # kiro declares resume but is UNMEASURED here → not advertised (B4 guard).
    assert resume_k["declared"] is True
    assert resume_k["measured"] == "unmeasured"
    assert resume_k["advertised"] is False
