"""Unit tests for F114/F118: per-terminal mechanism REVERTED (A-path), guards retained.

F118 A-path deleted the f2489d5e per-terminal agent config mechanism. These tests
verify that the old HOTFIX F114 fallback and per-terminal files are absent.
"""

import subprocess
from pathlib import Path

import pytest


class TestNoF114FallbackSites:
    """test_no_f114_fallback_sites — AC3/AC4 (retained post-F118 A-path)."""

    def test_grep_hotfix_f114_returns_empty(self):
        """grep '# HOTFIX F114' must return no matches in src/."""
        src_dir = Path(__file__).parent.parent.parent / "src"
        result = subprocess.run(
            ["grep", "-r", "--include=*.py", "# HOTFIX F114", str(src_dir)],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "", f"HOTFIX F114 sites still exist:\n{result.stdout}"

    def test_terminal_id_fallback_deleted(self):
        """utils/terminal_id_fallback.py must not exist."""
        fallback_path = (
            Path(__file__).parent.parent.parent
            / "src"
            / "cli_agent_orchestrator"
            / "utils"
            / "terminal_id_fallback.py"
        )
        assert not fallback_path.exists(), f"File still exists: {fallback_path}"


class TestPerTerminalMechanismReverted:
    """F118 AC4: per-terminal agent config mechanism fully removed."""

    def test_no_write_per_terminal_in_kiro_cli(self):
        """_write_per_terminal_agent_config must not exist in kiro_cli.py."""
        kiro_cli_path = (
            Path(__file__).parent.parent.parent
            / "src"
            / "cli_agent_orchestrator"
            / "providers"
            / "kiro_cli.py"
        )
        content = kiro_cli_path.read_text(encoding="utf-8")
        assert "_write_per_terminal_agent_config" not in content
        assert "_apply_per_terminal_agent" not in content
        assert "cao-<tid>" not in content

    def test_no_per_terminal_json_pattern_in_kiro_cli(self):
        """No reference to <tid>.kiro-agent.json pattern in provider."""
        kiro_cli_path = (
            Path(__file__).parent.parent.parent
            / "src"
            / "cli_agent_orchestrator"
            / "providers"
            / "kiro_cli.py"
        )
        content = kiro_cli_path.read_text(encoding="utf-8")
        assert ".kiro-agent.json" not in content
