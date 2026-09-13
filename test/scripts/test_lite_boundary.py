"""AC-LITE-3 slice-1: executable RED boundary plus non-growing debt inventory.

Known debt is not exempted by the scanner. The xfail is an actual unresolved
acceptance arm, not a declaration that lite has shipped. All mutations operate
in temporary input trees, never the live source or a user's hook files.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.lite_boundary import (
    knowledge_domain,
    scan_artifact,
    scan_configuration,
    scan_python,
    scan_repository,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def current_findings():
    return scan_repository(ROOT)


@pytest.mark.xfail(
    strict=True, reason="AC-LITE-3 blocked by inventoried infrastructure reverse edges"
)
def test_ac_lite3_current_boundary_is_not_yet_achieved(current_findings):
    assert not current_findings, current_findings


def test_slice1_unresolved_source_debt_cannot_grow(current_findings):
    assert {(row.location, row.rule, row.evidence) for row in current_findings} == {
        (
            "src/cli_agent_orchestrator/chatgpt_web_runner/production.py",
            "reverse-import",
            "cli_agent_orchestrator.chatgpt_web_runner.orchestrator.ReviewRequest",
        ),
        (
            "src/cli_agent_orchestrator/chatgpt_web_runner/production.py",
            "reverse-import",
            "cli_agent_orchestrator.chatgpt_web_runner.orchestrator.run_review",
        ),
        (
            "src/cli_agent_orchestrator/cli/commands/ledger.py:check",
            "knowledge-io",
            "find_workspace_file,read_text -> HANDOFF.md",
        ),
        (
            "src/cli_agent_orchestrator/cli/commands/redeploy.py:_sync_composition_stores",
            "knowledge-io",
            "glob,is_dir,is_file,mkdir,read_text -> /orchestrator",
        ),
        (
            "src/cli_agent_orchestrator/services/fold_service.py:_p10_corpus_paths",
            "knowledge-io",
            "glob,is_dir,is_file,rglob -> /GOLDEN-TIPS.md",
        ),
        (
            "src/cli_agent_orchestrator/services/session_service.py:canonical_session_env",
            "knowledge-io",
            "is_dir -> /orchestrator",
        ),
    }


@pytest.mark.parametrize(
    "source",
    [
        "from orchestrator import self_audit",
        "from cli_agent_orchestrator.doctrine import rules",
        "import importlib; importlib.import_module('self_' + 'audit')",
        "from pathlib import Path; Path('HANDOFF.md').exists()",
        "from pathlib import Path; Path('BUGS.md').read_text()",
        "from pathlib import Path; Path('orchestrator') .glob('*.md')",
        "from pathlib import Path; Path('doctrine').stat()",
        "from pathlib import Path; Path('.claude/agents/self-audit.md').read_bytes()",
        "from pathlib import Path; p = 'HAND' + 'OFF' + '.md'; Path(p).read_text()",
        "from pathlib import Path; d = 'doctrine'; p = Path('/project') / d; p.is_dir()",
        "from pathlib import Path; p = 'GOLDEN-TIPS.md'; alias = p; Path(alias).open()",
        "from pathlib import Path; stem = 'MISTAKES'; Path(f'{stem}.md').exists()",
        "from pathlib import Path; Path('policy-renamed.md').read_text(); purpose='compliance auditor'",
        "scheduler.start_compliance_auditor()",
        "from pathlib import Path; Path('BUGS.md').write_text('new audit state')",
        "from builtins import open as consume; consume('BUGS.md')",
        "scheduler.schedule('self-audit', periodic=True)",
        "hooks.register('SessionStart', command='self-audit-gen.sh')",
    ],
)
def test_named_source_mutants_turn_boundary_red(source):
    assert scan_python(source, "src/cli_agent_orchestrator/api/probe.py"), source


def test_generic_safety_journal_diag_and_historical_citations_remain_allowed():
    assert not scan_python(
        '''
"""Design authority: orchestrator/blueprints/kernel.md."""
from pathlib import Path
def inspect_runtime():
    """Never reads HANDOFF.md or starts self-audit."""
    return Path('delivery-journal.json').read_text()
''',
        "src/cli_agent_orchestrator/app/diag/probe.py",
    )


@pytest.mark.parametrize(
    "value",
    [
        {"dependencies": ["self-audit>=1"]},
        {"scripts": {"audit": "orchestrator.self_audit:main"}},
        {"package-data": ["doctrine/**/*.md"]},
        {"before-start": "test -f orchestrator/HANDOFF.md"},
        {"hooks": {"SessionStart": [{"command": "run self-audit-gen.sh"}]}},
        {"hooks": {"PreToolUse": [{"command": "dispatch compliance_auditor"}]}},
    ],
)
def test_metadata_entry_startup_and_hook_mutants_turn_boundary_red(value):
    assert scan_configuration(value, "mutant")


def test_wheel_sdist_and_installed_artifact_mutants_turn_red(tmp_path):
    wheel = tmp_path / "mutant.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("cli_agent_orchestrator/doctrine/rules.md", "knowledge")
        archive.writestr("mutant.dist-info/entry_points.txt", "audit = self_audit:main")
    assert {row.rule for row in scan_artifact(wheel)} == {
        "packaged-knowledge",
        "knowledge-entry-or-dependency",
        "knowledge-content",
    }
    sdist = tmp_path / "mutant.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        payload = b"knowledge"
        member = tarfile.TarInfo("mutant/orchestrator/GOLDEN-TIPS.md")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    assert scan_artifact(sdist)[0].rule == "packaged-knowledge"
    installed = tmp_path / "installed"
    (installed / "cli_agent_orchestrator").mkdir(parents=True)
    (installed / "cli_agent_orchestrator" / "probe.py").write_text(
        "from pathlib import Path; Path('BUGS.md').exists()"
    )
    assert scan_artifact(installed)[0].rule == "knowledge-io"


def test_renaming_a_packaged_audit_prompt_does_not_evade_content_scan(tmp_path):
    wheel = tmp_path / "renamed.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "cli_agent_orchestrator/resources/innocent.md",
            "You are the compliance auditor. Judge doctrine rules.",
        )
    assert scan_artifact(wheel)[0].rule == "knowledge-content"


@pytest.mark.slow
def test_executable_scanner_does_not_hide_debt_or_return_success():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/lite_boundary.py"), "--root", str(ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert data["boundary_achieved"] is False
    assert len(data["findings"]) == 6


@pytest.mark.parametrize(
    "path",
    [
        "orchestrator/HANDOFF.md",
        "doctrine/rules.md",
        "ORCH_MAP.md",
        "WP-BACKLOG.md",
        "BUGS.md",
        "MISTAKES.md",
        "GOLDEN-TIPS.md",
        ".claude/agents/self-audit.md",
        "blueprints/renamed.md",
        "audit/archive/self_audit_report.json",
    ],
)
def test_closed_knowledge_paths_are_covered(path):
    assert knowledge_domain(path)
