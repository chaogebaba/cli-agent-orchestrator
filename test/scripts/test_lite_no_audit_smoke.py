"""Focused empty-project smoke, with adversarial I/O guard evidence."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_empty_project_import_help_health_no_audit(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "private-cao"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/lite_no_audit_smoke.py"),
            "--project",
            str(project),
            "--home",
            str(home),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"knowledge_io": []' in result.stdout
    assert '"project_unchanged": true' in result.stdout


@pytest.mark.slow
@pytest.mark.parametrize(
    "path",
    ["BUGS.md", "orchestrator/HANDOFF.md", ".claude/agents/self-audit.md", "doctrine/rules.md"],
)
def test_smoke_guard_mutants_reject_missing_knowledge_probes(tmp_path, path):
    project = tmp_path / "project"
    project.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/lite_no_audit_smoke.py"),
            "--project",
            str(project),
            "--home",
            str(tmp_path / "private"),
            "--probe-knowledge",
            path,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "forbidden knowledge I/O" in result.stderr
    assert not list(project.iterdir())


def test_findings_harness_requires_explicit_input_before_browser_or_scratch(tmp_path, monkeypatch):
    from cli_agent_orchestrator.chatgpt_web_runner import live_spike

    scratch = tmp_path / "not-created"
    monkeypatch.setattr(live_spike, "_SCRATCH", scratch)

    def forbidden(*args, **kwargs):
        raise AssertionError("browser/profile called before explicit input validation")

    monkeypatch.setattr(live_spike, "resolve_profile_dir", forbidden)
    with pytest.raises(ValueError, match="requires an explicit attach_path"):
        asyncio.run(live_spike._run("findings", None))
    assert not scratch.exists()
