"""AC-LITE-3 — the CLI split holds, in both directions.

``wp-arch-modular-core.md`` A.4 moves three commands off base ``cao`` onto a second console
script, ``cao-orchestrator``, because each of them reads a skill-owned knowledge path. The
split is an accepted breaking change (user, 2026-09-16), so the contract is not merely
"the command is gone" — a bare Click "No such command" would leave every existing caller
guessing. Base keeps a hidden stub that names its replacement.

Both directions are asserted. A one-directional test passes through half the drift:
base could re-acquire a skill command, or ``cao-orchestrator`` could quietly grow one.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli as base_cli
from cli_agent_orchestrator.cli.orchestrator_main import cli as orchestrator_cli

REPO_ROOT = Path(__file__).resolve().parents[2]

# The closed skill-only command set: the three commands A.4 MOVED off base, plus the
# F788 #645 certification verbs, which are born here and must never appear on base —
# a `cao certify` would put a writer of orchestration judgment on the infra CLI that a
# lightweight project runs. `fold` itself stays on base — only corpus discovery left,
# so `cao fold FILE` is untouched and is asserted separately below.
MOVED = ("ledger", "fold-corpus", "sync-routing", "certify", "cert-status")

POINTER = "cao-orchestrator"


def _as_path(entry_point_target: str) -> str:
    """``pkg.sub.mod:Object`` -> ``pkg/sub/mod``, so a dotted target is comparable to a path.

    Without this the A.2 matcher — which is anchored on path separators — never fires on an
    entry point, and the plugin assertion below would be vacuously true.
    """
    return entry_point_target.partition(":")[0].replace(".", "/")


def _visible_commands(group: click.Group) -> set[str]:
    ctx = click.Context(group, info_name=group.name)
    return {name for name in group.list_commands(ctx) if not group.get_command(ctx, name).hidden}


def test_base_help_lists_none_of_the_moved_commands() -> None:
    result = CliRunner().invoke(base_cli, ["--help"])
    assert result.exit_code == 0, result.output
    visible = _visible_commands(base_cli)
    assert visible.isdisjoint(MOVED), f"base re-acquired a skill command: {visible & set(MOVED)}"
    for name in MOVED:
        assert f"\n  {name} " not in result.output


def test_base_keeps_its_other_top_level_commands() -> None:
    """A.4: base keeps 31 of 34. The split must not have cost anything else."""
    expected = {
        "profile",
        "agents",
        "base",
        "barrier",
        "doctor",
        "launch",
        "config",
        "init",
        "install",
        "redeploy",
        "sandbox",
        "shutdown",
        "schedule",
        "seam",
        "env",
        "mcp-server",
        "info",
        "memory",
        "skills",
        "session",
        "terminal",
        "workflow",
        "messages",
        "mailbox",
        "fold",
        "suite",
        "verify",
        "diag",
        "gate",
        "update",
        "tui",
        "auto-answers",
        "identity",
        "providers",
    }
    missing = expected - _visible_commands(base_cli)
    assert not missing, f"base lost commands the split should not touch: {sorted(missing)}"


@pytest.mark.parametrize("args", [["ledger"], ["ledger", "check"]])
def test_base_ledger_exits_nonzero_with_a_one_line_pointer(args: list[str]) -> None:
    """The stub reports the move instead of failing as an unknown command."""
    result = CliRunner().invoke(base_cli, args)
    assert result.exit_code != 0
    message = [line for line in result.output.splitlines() if line.strip()][-1]
    assert POINTER in message, result.output
    assert len(message.splitlines()) == 1


def test_base_fold_corpus_flag_points_at_the_skill_cli() -> None:
    result = CliRunner().invoke(base_cli, ["fold", "--check", "--corpus"])
    assert result.exit_code != 0
    assert POINTER in result.output
    assert "--corpus" not in CliRunner().invoke(base_cli, ["fold", "--help"]).output


def test_base_fold_file_is_unchanged(tmp_path: Path) -> None:
    """Only corpus discovery moved; `cao fold FILE` is a generic Markdown editor (A.4)."""
    doc = tmp_path / "doc.md"
    doc.write_text("# heading\n\nbody\n", encoding="utf-8")
    result = CliRunner().invoke(base_cli, ["fold", str(doc), "--check"])
    assert result.exit_code == 0, result.output


def test_orchestrator_cli_exposes_exactly_the_skill_command_set() -> None:
    assert _visible_commands(orchestrator_cli) == set(MOVED)


def test_pyproject_declares_both_console_scripts() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["cao"] == "cli_agent_orchestrator.cli.main:cli"
    assert scripts["cao-orchestrator"] == "cli_agent_orchestrator.cli.orchestrator_main:cli"


def test_no_plugin_entry_point_injects_skill_knowledge_into_base() -> None:
    """A.5's second mutation: a plugin must not be the seam's back door.

    ``cao.plugins`` is a real, pre-existing entry-point group, and it is NOT a command
    registry — ``plugins/base.py`` offers ``setup``/``teardown``/``on_mcp_server`` and the
    ``@hook`` event decorator, and nothing that can add a Click command. So the boundary
    risk is not injection of a subcommand; it is a plugin whose module is skill-owned,
    which would load knowledge code into every base process at startup. That is what is
    asserted, plus the absence of a command-registration hook that would change the answer.
    """
    from test.helpers.knowledge_io_scan import knowledge_domain

    from cli_agent_orchestrator.plugins.base import CaoPlugin

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = data["project"]["entry-points"]["cao.plugins"]
    offenders = {
        name: target for name, target in entry_points.items() if knowledge_domain(_as_path(target))
    }
    assert not offenders, f"a plugin loads skill-owned code into base: {offenders}"

    command_hooks = [
        name
        for name in dir(CaoPlugin)
        if not name.startswith("__") and ("command" in name or "cli" in name)
    ]
    assert not command_hooks, f"the plugin API grew a CLI surface: {command_hooks}"


def test_mutant_plugin_entry_point_naming_a_skill_module_turns_red() -> None:
    from test.helpers.knowledge_io_scan import knowledge_domain

    mutant = {"orch_ledger": "cli_agent_orchestrator.orchestrator.ledger:LedgerPlugin"}
    assert {n: t for n, t in mutant.items() if knowledge_domain(_as_path(t))}


def test_mutant_reregistering_ledger_on_base_turns_red() -> None:
    """The AC-LITE-3 mutation arm: put the skill command back on base and the check fails."""
    from cli_agent_orchestrator.cli.orchestrator_commands.ledger import ledger as real_ledger

    mutant = click.Group("cao", commands=dict(base_cli.commands))
    mutant.add_command(real_ledger)

    assert "ledger" in _visible_commands(mutant), "fixture did not re-register the command"
    with pytest.raises(AssertionError):
        visible = _visible_commands(mutant)
        assert visible.isdisjoint(MOVED), f"base re-acquired: {visible & set(MOVED)}"


def test_mutant_orchestrator_cli_growing_a_fourth_command_turns_red() -> None:
    """The reverse drift: the skill CLI is a closed set, not a dumping ground."""
    mutant = click.Group("cao-orchestrator", commands=dict(orchestrator_cli.commands))
    mutant.add_command(click.Command("extra", callback=lambda: None))
    assert _visible_commands(mutant) != set(MOVED)
