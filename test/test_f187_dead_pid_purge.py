"""Test for F187: down --purge proceeds when the recorded server PID is dead."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator.sandbox_bootstrap import (
    MANIFEST_NAME,
    MUTABLE_PATHS,
    PROVIDERS,
    SHARED_AUTH_PROVIDERS,
    SandboxError,
    _load_owned,
    _process_start_time,
)


class TestF187DeadPidPurge:
    """down --purge must proceed when the server PID is already dead."""

    def test_process_start_time_raises_on_dead_pid(self) -> None:
        """Pre-fix behavior: _process_start_time raises SandboxError for dead PID."""
        dead_pid = 4194304  # very unlikely to be alive
        with pytest.raises(SandboxError, match="cannot identify process"):
            _process_start_time(dead_pid)

    def test_load_owned_tolerates_dead_pid_with_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post-fix: allow_dead_pid=True returns pid_record with _dead flag."""
        root = tmp_path / "sbx"
        root.mkdir(mode=0o700)
        nonce = uuid.uuid4().hex
        dead_pid = 4194304

        # Write pidfile
        pidfile = root / "sandbox.pid"
        pid_record = {
            "pid": dead_pid,
            "start_time": 99999999,
            "owner_nonce": nonce,
        }
        pidfile.write_text(json.dumps(pid_record, sort_keys=True), encoding="utf-8")
        pidfile.chmod(0o600)

        # Patch validate_manifest and read_manifest to return a minimal manifest
        fake_manifest = {"pidfile": str(pidfile), "owner_nonce": nonce}
        monkeypatch.setattr(
            "cli_agent_orchestrator.sandbox_bootstrap.validate_manifest",
            lambda m, p: m,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.sandbox_bootstrap.read_manifest",
            lambda p: fake_manifest,
        )

        # Without allow_dead_pid, it raises
        with pytest.raises(SandboxError, match="cannot identify process"):
            _load_owned(root, allow_dead_pid=False)

        # With allow_dead_pid, it succeeds and marks _dead
        _, _, result_pid = _load_owned(root, allow_dead_pid=True)
        assert result_pid.get("_dead") is True
        assert result_pid["pid"] == dead_pid

    def test_load_owned_rejects_wrong_nonce_even_dead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with allow_dead_pid, owner_nonce mismatch is still rejected."""
        root = tmp_path / "sbx"
        root.mkdir(mode=0o700)
        dead_pid = 4194304

        pidfile = root / "sandbox.pid"
        pid_record = {
            "pid": dead_pid,
            "start_time": 99999999,
            "owner_nonce": "wrong-nonce",
        }
        pidfile.write_text(json.dumps(pid_record, sort_keys=True), encoding="utf-8")
        pidfile.chmod(0o600)

        fake_manifest = {"pidfile": str(pidfile), "owner_nonce": "correct-nonce"}
        monkeypatch.setattr(
            "cli_agent_orchestrator.sandbox_bootstrap.validate_manifest",
            lambda m, p: m,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.sandbox_bootstrap.read_manifest",
            lambda p: fake_manifest,
        )

        with pytest.raises(SandboxError, match="pidfile owner mismatch"):
            _load_owned(root, allow_dead_pid=True)



class TestF187S1LivePidRefusal:
    """S1: down --purge REFUSES when recorded PID is alive (kills mutant M-D).

    The fix's contract: allow_dead_pid only bypasses the dead-PID exception.
    When the PID is alive, start_time validation ALWAYS runs regardless of
    allow_dead_pid. A live PID with mismatched start_time (PID-reuse) is
    rejected with "sandbox pid identity mismatch".
    """

    def _setup_live_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, start_time_offset: int = 0
    ) -> Path:
        """Create manifest+pidfile pointing at THIS process (guaranteed alive)."""
        root = tmp_path / "sbx"
        root.mkdir(mode=0o700)
        nonce = uuid.uuid4().hex
        live_pid = os.getpid()
        real_start_time = _process_start_time(live_pid)

        pidfile = root / "sandbox.pid"
        pid_record = {
            "pid": live_pid,
            "start_time": real_start_time + start_time_offset,
            "owner_nonce": nonce,
        }
        pidfile.write_text(json.dumps(pid_record, sort_keys=True), encoding="utf-8")
        pidfile.chmod(0o600)

        fake_manifest = {"pidfile": str(pidfile), "owner_nonce": nonce}
        monkeypatch.setattr(
            "cli_agent_orchestrator.sandbox_bootstrap.validate_manifest",
            lambda m, p: m,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.sandbox_bootstrap.read_manifest",
            lambda p: fake_manifest,
        )
        return root

    def test_load_owned_refuses_live_pid_with_mismatched_start_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Live PID + wrong start_time → rejected even with allow_dead_pid=True.

        This is the PID-reuse scenario: a different process now occupies the
        recorded PID. The fix must not skip start_time validation.
        """
        root = self._setup_live_pid(tmp_path, monkeypatch, start_time_offset=9999)
        with pytest.raises(SandboxError, match="sandbox pid identity mismatch"):
            _load_owned(root, allow_dead_pid=True)

    def test_load_owned_refuses_live_pid_without_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Live PID + wrong start_time → rejected with allow_dead_pid=False too."""
        root = self._setup_live_pid(tmp_path, monkeypatch, start_time_offset=9999)
        with pytest.raises(SandboxError, match="sandbox pid identity mismatch"):
            _load_owned(root, allow_dead_pid=False)

    def test_load_owned_accepts_live_pid_with_matching_start_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Live PID + correct start_time → accepted (this IS our process)."""
        root = self._setup_live_pid(tmp_path, monkeypatch, start_time_offset=0)
        _, _, pid_record = _load_owned(root, allow_dead_pid=True)
        assert pid_record.get("_dead") is None
        assert pid_record["pid"] == os.getpid()
