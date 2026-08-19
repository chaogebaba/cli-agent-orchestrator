"""Test that scripts/sweep_stale_grok_homes.py imports cleanly and runs in dry-run mode.

Guards against dead imports (B1 from gate R1).
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sweep_stale_grok_homes.py"


class TestSweepScriptImport:
    """B1 guard: the sweep script must be importable without ImportError."""

    def test_script_compiles(self):
        """Script can be compiled (catches SyntaxError and dead imports at module level)."""
        source = SCRIPT.read_text(encoding="utf-8")
        compile(source, str(SCRIPT), "exec")

    def test_dry_run_against_nonexistent_root(self, tmp_path, monkeypatch):
        """Dry-run exits cleanly when the managed root does not exist."""
        # Override Path.home() to a scratch dir with no grok/terminals
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            env={
                **__import__("os").environ,
                "HOME": str(tmp_path),
            },
            timeout=30,
        )
        # Should exit 0 — "Managed root does not exist" is a clean exit
        assert result.returncode == 0
        assert "does not exist" in result.stdout or "No stale" in result.stdout

    def test_dry_run_with_stale_dir(self, tmp_path, monkeypatch):
        """Dry-run identifies a stale directory without removing it."""
        # Create a fake managed root with a stale dir
        managed_root = tmp_path / ".aws" / "cli-agent-orchestrator" / "grok" / "terminals"
        managed_root.mkdir(parents=True)
        stale_dir = managed_root / "orphan-abc123abc123"
        stale_dir.mkdir()

        # Mock the database import to return no live terminals
        # We do this by creating a stub module
        stub_dir = tmp_path / "stub"
        stub_dir.mkdir()
        pkg = stub_dir / "cli_agent_orchestrator"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        clients = pkg / "clients"
        clients.mkdir()
        (clients / "__init__.py").write_text("")
        (clients / "database.py").write_text("def list_all_terminals():\n    return []\n")

        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            env={
                **__import__("os").environ,
                "HOME": str(tmp_path),
                "PYTHONPATH": str(stub_dir),
            },
            timeout=30,
        )
        assert result.returncode == 0
        assert "1 stale" in result.stdout
        assert "Dry-run" in result.stdout
        # Dir must still exist (dry-run)
        assert stale_dir.exists()
