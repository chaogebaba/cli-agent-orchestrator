"""SEED_RULES is the repo-canonical source of ~/.aws/cli-agent-orchestrator/auto-answers/<provider>.yaml
(created only if absent). Lock the codex seed's shape so a live-yaml rule change is mirrored here.
"""

from pathlib import Path

from cli_agent_orchestrator.services import auto_responder as ar


def _codex_seed_rules():
    return ar._RuleStore._load  # noqa: SLF001 - hermetic loader


def test_codex_seed_parses_expected_rules(tmp_path: Path) -> None:
    path = tmp_path / "codex.yaml"
    path.write_text(ar.SEED_RULES["codex.yaml"])
    rules = ar._RuleStore._load(path)
    names = [r.name for r in rules]
    assert names == [
        "codex-usage-resets",
        "codex-trust-dir",
        "codex-trust-dir-subdir",
        "codex-resume-working-directory",
    ]


def test_codex_seed_usage_resets_is_human_gated(tmp_path: Path) -> None:
    path = tmp_path / "codex.yaml"
    path.write_text(ar.SEED_RULES["codex.yaml"])
    rules = {r.name: r for r in ar._RuleStore._load(path)}
    assert rules["codex-usage-resets"].is_wait


def test_codex_seed_trust_subdir_matches_worktree_prompt(tmp_path: Path) -> None:
    path = tmp_path / "codex.yaml"
    path.write_text(ar.SEED_RULES["codex.yaml"])
    rules = {r.name: r for r in ar._RuleStore._load(path)}
    screen = (
        "This folder is a subdirectory of a Git project. Trusting will apply to the "
        "repository root. › 1. Yes, continue 2. No, quit Press enter to continue"
    )
    assert rules["codex-trust-dir-subdir"].matches(screen)
    assert not rules["codex-trust-dir-subdir"].matches(
        "Do you trust the contents of this directory? Yes, continue No, quit"
    )
