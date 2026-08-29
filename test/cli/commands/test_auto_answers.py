"""Acceptance tests for ``cao auto-answers test`` (F530 diagnosability)."""

from __future__ import annotations

import textwrap

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.services import auto_responder as ar

_CHOOSER_PANE = "\n".join(
    [
        "Choose working directory to resume this session",
        "  Session = latest cwd recorded in the resumed session",
        "  Current = your current working directory",
        "",
        "  1. Use session directory (/home/x/proj)",
        "\u203a 2. Use current directory (/home/x/proj/.cao/worktrees/y)",
        "  3. Always use session directory",
        "  4. Always use current directory",
        "  Press enter to continue",
        "",
        "\u2022 Working (12s \u2022 esc to interrupt)",
        "\u203a Ask Codex to do anything",
        "  gpt-5.6-sol high \u00b7 Context 66% left",
    ]
)

_RULES_YAML = textwrap.dedent("""\
    - name: codex-resume-working-directory
      enabled: true
      match_mode: regex
      question: "Choose working directory to resume this session"
      options: ["Use session directory", "Use current directory", "Press enter to continue"]
      answer: ["Down", "Enter"]
    - name: codex-trust-dir
      enabled: true
      match_mode: contains
      question: "Do you trust the contents of this directory?"
      options: ["Yes, continue", "No, quit"]
      answer: ["Enter"]
    """)


@pytest.fixture()
def _isolated_rules(tmp_path, monkeypatch):
    """Point the rule store at a controlled codex.yaml so the CLI is hermetic."""
    rules_dir = tmp_path / "auto-answers"
    rules_dir.mkdir()
    (rules_dir / "codex.yaml").write_text(_RULES_YAML, encoding="utf-8")
    monkeypatch.setattr(ar, "AUTO_ANSWER_DIR", rules_dir)
    ar._store._cache.clear()
    yield
    ar._store._cache.clear()


def test_auto_answers_test_reports_match_and_chrome_region(tmp_path, _isolated_rules):
    pane = tmp_path / "pane.txt"
    pane.write_text(_CHOOSER_PANE, encoding="utf-8")

    result = CliRunner().invoke(cli, ["auto-answers", "test", "codex", str(pane)])

    assert result.exit_code == 0, result.output
    assert "codex-resume-working-directory (mode=regex) \u2192 MATCH" in result.output
    assert "chrome-filtered match region: True" in result.output
    assert "verdict: a rule MATCHES" in result.output
    # The chrome (spinner) is present in the unfiltered region dump.
    assert "esc to interrupt" in result.output


def test_auto_answers_test_names_failing_field_when_no_rule_matches(tmp_path, _isolated_rules):
    pane = tmp_path / "pane.txt"
    pane.write_text(
        "just some ordinary output\n\u203a Ask Codex to do anything\n", encoding="utf-8"
    )

    result = CliRunner().invoke(cli, ["auto-answers", "test", "codex", str(pane)])

    assert result.exit_code == 0, result.output
    assert "NO rule matched" in result.output
    assert "reject: question(regex)" in result.output
    assert "reject: question(contains)" in result.output
