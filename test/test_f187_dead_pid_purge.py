"""Test for F187: down --purge proceeds when the recorded server PID is dead."""

from __future__ import annotations

import json
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
