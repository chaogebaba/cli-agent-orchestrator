"""F587 D19 — codex seed failure carries a redacted output tail on EVERY branch.

AC6: a seed forced to time out AND a seed forced to rc≠0 both raise with an
error payload containing the last output line; a tail carrying a planted secret
is redacted via ``services.secret_gate`` before it reaches the exception (and
thus the assign error payload). Observability only — no retry, no behaviour
change on the success path.
"""

import subprocess
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.providers import codex as codex_mod
from cli_agent_orchestrator.providers.codex import (
    _SEED_FAILURE_TAIL_LINES,
    CodexProvider,
    _seed_failure_tail,
)

# ---- the shared tail helper ----------------------------------------------


def test_seed_failure_tail_empty_and_none():
    assert _seed_failure_tail(None) == ""
    assert _seed_failure_tail("") == ""


def test_seed_failure_tail_bounds_to_max_lines():
    text = "\n".join(f"line {i}" for i in range(100))
    tail = _seed_failure_tail(text)
    lines = tail.splitlines()
    assert len(lines) == _SEED_FAILURE_TAIL_LINES
    assert lines[-1] == "line 99"


def test_seed_failure_tail_redacts_planted_secret():
    """A planted token in the tail is scrubbed — the original bytes never
    survive into the returned string (secret_gate arm)."""
    planted = "sk-ant-api03-DEADBEEFdeadbeefDEADBEEFdeadbeef0123456789abcdefXYZ"
    text = f"some log line\nAuthorization: Bearer {planted}\ntrailing"
    tail = _seed_failure_tail(text)
    assert planted not in tail
    assert "REDACTED" in tail


# ---- both failure branches carry the tail --------------------------------


def _argv_patches(monkeypatch):
    """Neutralise the argv-building helpers so seed_resume_identity reaches the
    subprocess call without a real profile/binary."""
    monkeypatch.setattr(codex_mod, "load_agent_profile", lambda _p: {})
    monkeypatch.setattr(codex_mod, "resolve_provider_binary", lambda _n: "/bin/true")
    monkeypatch.setattr(
        codex_mod, "_resolved_codex_profile_config", lambda _prof, _name: (None, {})
    )


def test_timeout_branch_carries_output_tail(monkeypatch):
    _argv_patches(monkeypatch)
    exc = subprocess.TimeoutExpired(cmd="codex", timeout=90, output="hung line 1\nSTALL_MARKER_XYZ")
    with patch.object(codex_mod.subprocess, "run", side_effect=exc):
        with pytest.raises(RuntimeError) as ei:
            CodexProvider.seed_resume_identity("/tmp/x", "codex_dev")
    msg = str(ei.value)
    assert msg.startswith("seed_timeout")
    assert "STALL_MARKER_XYZ" in msg


def test_timeout_branch_redacts_secret_in_tail(monkeypatch):
    _argv_patches(monkeypatch)
    planted = "sk-ant-api03-DEADBEEFdeadbeefDEADBEEFdeadbeef0123456789abcdefXYZ"
    exc = subprocess.TimeoutExpired(
        cmd="codex", timeout=90, output=f"Bearer {planted}\nfinal hung line"
    )
    with patch.object(codex_mod.subprocess, "run", side_effect=exc):
        with pytest.raises(RuntimeError) as ei:
            CodexProvider.seed_resume_identity("/tmp/x", "codex_dev")
    assert planted not in str(ei.value)


def test_rc_nonzero_branch_carries_output_tail(monkeypatch):
    _argv_patches(monkeypatch)
    completed = subprocess.CompletedProcess(
        args=["codex"], returncode=3, stdout="boot\nRC_FAIL_MARKER_ABC"
    )
    with patch.object(codex_mod.subprocess, "run", return_value=completed):
        with pytest.raises(RuntimeError) as ei:
            CodexProvider.seed_resume_identity("/tmp/x", "codex_dev")
    msg = str(ei.value)
    assert "seed_exec_failed rc=3" in msg
    assert "RC_FAIL_MARKER_ABC" in msg


def test_rc_nonzero_branch_redacts_secret_in_tail(monkeypatch):
    _argv_patches(monkeypatch)
    planted = "sk-ant-api03-DEADBEEFdeadbeefDEADBEEFdeadbeef0123456789abcdefXYZ"
    completed = subprocess.CompletedProcess(
        args=["codex"], returncode=1, stdout=f"Authorization: Bearer {planted}\nlast"
    )
    with patch.object(codex_mod.subprocess, "run", return_value=completed):
        with pytest.raises(RuntimeError) as ei:
            CodexProvider.seed_resume_identity("/tmp/x", "codex_dev")
    assert planted not in str(ei.value)
