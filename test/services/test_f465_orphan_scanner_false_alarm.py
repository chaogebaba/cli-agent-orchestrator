"""F465: Orphan scanner false-alarm regression tests.

Proves:
1. Same-UID user-session processes that are NOT descendants of the server
   (e.g., systemd --user children like sd-pam) no longer cause scan_incomplete.
2. A real orphan (same-UID, IS a server descendant, environ unreadable) still
   correctly marks the scan incomplete → is still flagged.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.orphan_reconcile_service import (
    _is_descendant_of_server,
    generate_incarnation_token,
    scan_incarnation_processes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SERVER_PID = 5000  # Simulated server PID for all tests


def _build_proc_entry(
    tmp_path: Path,
    pid: int,
    ppid: int,
    uid: int,
    *,
    environ: bytes = b"",
    environ_readable: bool = False,
    include_stat: bool = True,
    start_ticks: int = 100,
):
    """Build a minimal /proc/<pid> directory for scanner tests."""
    proc_dir = tmp_path / str(pid)
    proc_dir.mkdir(exist_ok=True)

    env_path = proc_dir / "environ"
    env_path.write_bytes(environ)
    if not environ_readable:
        os.chmod(env_path, 0o000)

    (proc_dir / "status").write_text(
        f"Name:\tproc{pid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
    )

    if include_stat:
        # Fields: pid (comm) state ppid pgrp session tty_nr tpgid flags minflt
        #         cminflt majflt cmajflt utime stime cutime cstime priority nice
        #         num_threads itrealvalue starttime ...
        # starttime is field 22 in /proc/pid/stat (1-based) = index 19 of the
        # post-comm split (0-based, fields after state).
        # We need: state(0) ppid(1) pgrp(2) session(3) tty_nr(4) tpgid(5)
        #          flags(6) minflt(7) cminflt(8) majflt(9) cmajflt(10)
        #          utime(11) stime(12) cutime(13) cstime(14) priority(15)
        #          nice(16) num_threads(17) itrealvalue(18) starttime(19)
        # That's 13 filler zeros for fields 6-18, then starttime at field 19.
        stat_content = (
            f"{pid} (proc{pid}) S {ppid} {pid} {pid} 0 -1 "
            f"0 0 0 0 0 0 0 0 0 0 0 0 0 "
            f"{start_ticks} 0 0\n"
        )
        (proc_dir / "stat").write_text(stat_content)


def _build_server_chain(tmp_path: Path, uid: int):
    """Build a process ancestry: init(1) → systemd_user(1252) → server(5000).

    This simulates the real-world process tree where the CAO server is a
    descendant of systemd --user.
    """
    # PID 1 (init) — different UID
    _build_proc_entry(tmp_path, 1, 0, 0, environ_readable=True, start_ticks=1)
    # PID 1252 (systemd --user) — same UID, ancestor of server
    _build_proc_entry(tmp_path, 1252, 1, uid, start_ticks=10)
    # PID 5000 (CAO server) — same UID
    _build_proc_entry(tmp_path, _SERVER_PID, 1252, uid, environ_readable=True, start_ticks=500)


# ---------------------------------------------------------------------------
# Tests: false-alarm class is gone
# ---------------------------------------------------------------------------


class TestF465FalseAlarmGone:
    """Same-UID non-descendant processes no longer cause scan_incomplete."""

    def test_sd_pam_sibling_preserves_completeness(self, tmp_path):
        """sd-pam (child of systemd --user, sibling of server) → benign, scan complete."""
        token = generate_incarnation_token()
        uid = os.getuid()

        _build_server_chain(tmp_path, uid)
        # PID 1314: sd-pam, child of systemd --user (1252), NOT a descendant of server (5000)
        # start_ticks=1100 > issuance_ticks=1000 → does NOT predate issuance
        # This exercises the F465 descendant check (the predate path won't catch it)
        _build_proc_entry(tmp_path, 1314, 1252, uid, start_ticks=1100)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._read_boot_id",
            return_value="same-boot",
        ):
            result = scan_incarnation_processes(
                token, uid, issuance_ticks=1000, issuance_boot_id="same-boot"
            )

        # Restore permissions for cleanup
        for p in tmp_path.rglob("environ"):
            try:
                os.chmod(p, 0o644)
            except OSError:
                pass

        assert result.complete is True
        assert result.matches == ()
        # Should have a not_server_descendant annotation (F465 path)
        assert any("not_server_descendant" in e for e in result.errors)

    def test_multiple_user_session_procs_all_benign(self, tmp_path):
        """Multiple user-session processes (sd-pam, dbus-daemon, etc.) → all benign."""
        token = generate_incarnation_token()
        uid = os.getuid()

        _build_server_chain(tmp_path, uid)
        # Simulate multiple user session processes — children of systemd --user
        _build_proc_entry(tmp_path, 1314, 1252, uid, start_ticks=11)  # sd-pam
        _build_proc_entry(tmp_path, 1400, 1252, uid, start_ticks=12)  # dbus-daemon
        _build_proc_entry(tmp_path, 1500, 1252, uid, start_ticks=15)  # pipewire

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._read_boot_id",
            return_value="same-boot",
        ):
            result = scan_incarnation_processes(
                token, uid, issuance_ticks=1000, issuance_boot_id="same-boot"
            )

        for p in tmp_path.rglob("environ"):
            try:
                os.chmod(p, 0o644)
            except OSError:
                pass

        assert result.complete is True
        assert result.matches == ()

    def test_non_descendant_without_issuance_ticks(self, tmp_path):
        """Non-descendant process with issuance_ticks=None → still benign (F465 core fix)."""
        token = generate_incarnation_token()
        uid = os.getuid()

        _build_server_chain(tmp_path, uid)
        # PID 1314: not a descendant of server, issuance_ticks not available
        _build_proc_entry(tmp_path, 1314, 1252, uid, start_ticks=11)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._read_boot_id",
            return_value="same-boot",
        ):
            # No issuance_ticks — the pre-issuance fence can't fire
            result = scan_incarnation_processes(token, uid)

        for p in tmp_path.rglob("environ"):
            try:
                os.chmod(p, 0o644)
            except OSError:
                pass

        # Before F465 this would have been complete=False; now it's clean
        assert result.complete is True
        assert any("not_server_descendant" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Tests: real orphan still flagged
# ---------------------------------------------------------------------------


class TestF465RealOrphanStillFlagged:
    """A same-UID process that IS a server descendant still marks scan_incomplete."""

    def test_server_descendant_unreadable_environ_incomplete(self, tmp_path):
        """Process that IS a descendant of server with unreadable environ → incomplete."""
        token = generate_incarnation_token()
        uid = os.getuid()

        _build_server_chain(tmp_path, uid)
        # PID 6000: child of server (5000) — a genuine potential orphan
        _build_proc_entry(tmp_path, 6000, _SERVER_PID, uid, start_ticks=800)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._read_boot_id",
            return_value="same-boot",
        ):
            result = scan_incarnation_processes(
                token, uid, issuance_ticks=700, issuance_boot_id="same-boot"
            )

        for p in tmp_path.rglob("environ"):
            try:
                os.chmod(p, 0o644)
            except OSError:
                pass

        # Server descendant with unreadable environ that doesn't predate issuance
        # MUST still cause scan_incomplete
        assert result.complete is False
        assert any("permission_denied_same_uid" in e for e in result.errors)

    def test_server_descendant_predates_issuance_is_clean(self, tmp_path):
        """Server descendant that predates issuance → annotated but clean."""
        token = generate_incarnation_token()
        uid = os.getuid()

        _build_server_chain(tmp_path, uid)
        # PID 6000: child of server (5000), but started before issuance
        _build_proc_entry(tmp_path, 6000, _SERVER_PID, uid, start_ticks=600)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._read_boot_id",
            return_value="same-boot",
        ):
            result = scan_incarnation_processes(
                token, uid, issuance_ticks=700, issuance_boot_id="same-boot"
            )

        for p in tmp_path.rglob("environ"):
            try:
                os.chmod(p, 0o644)
            except OSError:
                pass

        # Predates issuance — clean even though it's a descendant
        assert result.complete is True
        assert any("predates_issuance" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Tests: _is_descendant_of_server unit tests
# ---------------------------------------------------------------------------


class TestIsDescendantOfServer:
    """Unit tests for the _is_descendant_of_server helper."""

    def test_direct_child_is_descendant(self, tmp_path):
        """Direct child of server → True."""
        uid = os.getuid()
        _build_server_chain(tmp_path, uid)
        _build_proc_entry(tmp_path, 6000, _SERVER_PID, uid, start_ticks=800)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ):
            assert _is_descendant_of_server(6000) is True

    def test_grandchild_is_descendant(self, tmp_path):
        """Grandchild of server → True."""
        uid = os.getuid()
        _build_server_chain(tmp_path, uid)
        _build_proc_entry(tmp_path, 6000, _SERVER_PID, uid, start_ticks=800)
        _build_proc_entry(tmp_path, 7000, 6000, uid, start_ticks=900)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ):
            assert _is_descendant_of_server(7000) is True

    def test_sibling_not_descendant(self, tmp_path):
        """Sibling of server (child of same parent) → False."""
        uid = os.getuid()
        _build_server_chain(tmp_path, uid)
        # PID 1314: child of systemd --user (1252), sibling of server
        _build_proc_entry(tmp_path, 1314, 1252, uid, start_ticks=11)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ):
            assert _is_descendant_of_server(1314) is False

    def test_server_itself_not_descendant(self, tmp_path):
        """Server PID itself → False (it's not its own descendant)."""
        uid = os.getuid()
        _build_server_chain(tmp_path, uid)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ):
            assert _is_descendant_of_server(_SERVER_PID) is False

    def test_ancestor_not_descendant(self, tmp_path):
        """Server ancestor (systemd --user) → False."""
        uid = os.getuid()
        _build_server_chain(tmp_path, uid)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ):
            assert _is_descendant_of_server(1252) is False

    def test_unrelated_process_not_descendant(self, tmp_path):
        """Process with broken parent chain → True (fail closed: cannot prove non-descent)."""
        uid = os.getuid()
        _build_server_chain(tmp_path, uid)
        # PID 9999: parent 8888 doesn't exist → chain breaks at 9999
        # Fail closed: when parent chain can't be walked, assume descendant
        _build_proc_entry(tmp_path, 9999, 8888, uid, start_ticks=50)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service._PROC_ROOT", tmp_path,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.os.getpid",
            return_value=_SERVER_PID,
        ):
            assert _is_descendant_of_server(9999) is True
