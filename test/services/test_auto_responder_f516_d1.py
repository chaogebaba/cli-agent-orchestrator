"""F516 commit 7: D1 suppress-tier audit + codex adopted lever.

Codex is the ONLY provider adopted in this train (blueprint D1 SCOPE r3-S6):
its trust/approval dialogs never render because CAO launches codex with --yolo
(= --dangerously-bypass-approvals-and-sandbox, implying approval_policy="never"
and a trusted workspace). The unit-checkable part of AC8 is that the lever is
wired into the launch command; the live-spawn "dialog absent" proof is LIVE-ONLY.
"""

import shlex
from pathlib import Path

from cli_agent_orchestrator.providers.codex import CodexProvider

AUDIT_DOC = Path(__file__).parents[2] / "docs" / "f516-d1-suppress-tier-audit.md"


def test_codex_launches_with_the_adopted_yolo_suppression_lever():
    provider = CodexProvider("d1term", "sess", "win", None)
    argv = shlex.split(provider._build_codex_command())
    assert "--yolo" in argv


def test_codex_still_suppresses_the_startup_update_dialog_at_source():
    provider = CodexProvider("d1term", "sess", "win", None)
    argv = shlex.split(provider._build_codex_command())
    assert "check_for_update_on_startup=false" in argv


def test_d1_audit_table_documents_codex_only_adoption():
    text = AUDIT_DOC.read_text(encoding="utf-8")
    assert "adoption in this train is codex-only" in text
    for provider in ("kiro", "cline", "grok", "claude"):
        assert provider in text
    assert "audit-only" in text
    assert "LIVE-ONLY" in text
