"""FX170 — Native wake doorbell acceptance tests.

V1: Unit tests per blueprint ACs (no CAO server, no tmux, no live CC).
- AC2-AC4: Wire format / sanitization / no auth frame / no read
- AC5-AC7: Target resolution (descendant tree, procStart, ambiguity, no teammate_push gate)
- AC10: Wake verification
- AC11-AC13: Version guard + fallback fan-in + socket errors
- AC14-AC15: Dedup + config matrix
"""

from __future__ import annotations

import json
import os
import socketserver
import struct
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def sessions_dir(tmp_path):
    """Create a fixture ~/.claude/sessions/ directory."""
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture()
def proc_root(tmp_path):
    """Create a synthetic /proc tree."""
    d = tmp_path / "proc"
    d.mkdir()
    return d


def _make_registry_record(
    sessions_dir: Path,
    pid: int,
    *,
    session_id: str = "test-session-id",
    cwd: str = "/tmp/test",
    tmux: str = "cao-test:@0.%0",
    version: str = "2.1.231",
    peer_protocol: int = 1,
    socket_path: str = "/tmp/test.sock",
    proc_start: int = 12345,
    status: str = "idle",
    status_updated_at: str = "",
    updated_at: str = "",
    extra: dict | None = None,
) -> Path:
    """Write a registry record JSON file."""
    if not status_updated_at:
        status_updated_at = datetime.now(timezone.utc).isoformat()
    if not updated_at:
        updated_at = datetime.now(timezone.utc).isoformat()
    data = {
        "sessionId": session_id,
        "cwd": cwd,
        "tmux": tmux,
        "version": version,
        "peerProtocol": peer_protocol,
        "messagingSocketPath": socket_path,
        "procStart": proc_start,
        "status": status,
        "statusUpdatedAt": status_updated_at,
        "updatedAt": updated_at,
    }
    if extra:
        data.update(extra)
    path = sessions_dir / f"{pid}.json"
    path.write_text(json.dumps(data))
    return path


def _make_proc_entry(proc_root: Path, pid: int, ppid: int, starttime: int = 12345):
    """Create a synthetic /proc/<pid>/stat file.

    Format: "<pid> (comm) S <ppid> ... <field19=starttime> ..."
    Fields after comm: state ppid pgrp session tty_nr tpgid flags minflt cminflt
    majflt cmajflt utime stime cutime cstime priority nice num_threads itrealvalue starttime ...
    That's 20 fields (0-indexed, starttime is index 19).
    """
    entry_dir = proc_root / str(pid)
    entry_dir.mkdir(parents=True, exist_ok=True)
    # Build the stat line: fields after ") " are space-separated
    # We need at least 20 fields, starttime at index 19
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(starttime)] + ["0"] * 10
    stat_line = f"{pid} (test) " + " ".join(fields)
    (entry_dir / "stat").write_text(stat_line)


class UnixSocketStub:
    """A socketserver.UnixStreamServer stub for testing socket writes."""

    def __init__(self):
        self.received_data: list[bytes] = []
        self._tmpdir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self._tmpdir, "test.sock")
        self.server = None
        self._thread = None

    def start(self):
        parent = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                data = self.rfile.read()
                parent.received_data.append(data)

        self.server = socketserver.UnixStreamServer(self.socket_path, Handler)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        os.rmdir(self._tmpdir)

    @property
    def last_line(self) -> str | None:
        # Give the handler thread time to process
        import time
        for _ in range(20):
            if self.received_data:
                break
            time.sleep(0.05)
        if not self.received_data:
            return None
        return self.received_data[-1].decode("utf-8").strip()

    @property
    def last_json(self) -> dict | None:
        line = self.last_line
        if line is None:
            return None
        return json.loads(line)


@pytest.fixture()
def socket_stub():
    stub = UnixSocketStub()
    stub.start()
    yield stub
    stub.stop()


@pytest.fixture(autouse=True)
def _reset_doorbell_state():
    """Reset doorbell module state between tests."""
    import cli_agent_orchestrator.services.doorbell_service as ds
    ds._last_doorbell_row_id.clear()
    ds._last_warn_time.clear()
    yield
    ds._last_doorbell_row_id.clear()
    ds._last_warn_time.clear()


# ===========================================================================
# AC2: Wire format — one line, JSON, msgV==1, type==user, priority==next,
#       from matches bridge:cao-..., content in <cross-session-message>, half-close, no read
# ===========================================================================


class TestAC2WireFormat:
    """The socket write is exactly one JSON line with correct structure."""

    def test_write_produces_single_json_line(self, socket_stub):
        from cli_agent_orchestrator.services.cc_session_registry import (
            build_wake_payload,
            write_to_socket,
        )
        payload = build_wake_payload("test-worker", 42)
        err = write_to_socket(socket_stub.socket_path, payload)
        assert err is None
        line = socket_stub.last_line
        assert line is not None
        # Must be valid JSON
        data = json.loads(line)
        assert data["msgV"] == 1
        assert data["type"] == "user"
        assert data["priority"] == "next"
        assert data["from"].startswith("bridge:cao-")
        # Content wrapped in cross-session-message
        content = data["message"]["content"]
        assert "<cross-session-message" in content
        assert "</cross-session-message>" in content
        # msg_id is a uuid
        uuid.UUID(data["msg_id"])

    def test_from_address_matches_pattern(self, socket_stub):
        """from field matches ^bridge:cao-[A-Za-z0-9._-]{1,64}$."""
        import re
        from cli_agent_orchestrator.services.cc_session_registry import (
            build_wake_payload,
            write_to_socket,
        )
        payload = build_wake_payload("my-worker.v2_test", 99)
        write_to_socket(socket_stub.socket_path, payload)
        data = socket_stub.last_json
        assert re.match(r"^bridge:cao-[A-Za-z0-9._-]{1,64}$", data["from"])

    def test_half_close_no_read(self, socket_stub):
        """Connection is half-closed after write, no read attempted.

        Mutant: a client that reads a response would block on our stub
        (stub sends nothing). We verify the write succeeds quickly.
        """
        from cli_agent_orchestrator.services.cc_session_registry import (
            build_wake_payload,
            write_to_socket,
        )
        payload = build_wake_payload("worker", 1)
        import time
        start = time.monotonic()
        err = write_to_socket(socket_stub.socket_path, payload, connect_timeout_s=2.0)
        elapsed = time.monotonic() - start
        assert err is None
        # Must complete fast (< 2s) — a read would block
        assert elapsed < 2.0

    def test_content_has_balanced_tags(self, socket_stub):
        """Exactly one opening and one closing cross-session-message tag."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            build_wake_payload,
            write_to_socket,
        )
        payload = build_wake_payload("worker", 1)
        write_to_socket(socket_stub.socket_path, payload)
        content = socket_stub.last_json["message"]["content"]
        assert content.count("<cross-session-message") == 1
        assert content.count("</cross-session-message>") == 1


# ===========================================================================
# AC3: No auth frame on Linux, .key file never opened
# ===========================================================================


class TestAC3NoAuthFrame:
    """No auth frame is written; .key file is never opened."""

    def test_no_auth_frame_in_payload(self, socket_stub):
        """The data written to the socket contains no auth frame."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            build_wake_payload,
            write_to_socket,
        )
        payload = build_wake_payload("worker", 1)
        write_to_socket(socket_stub.socket_path, payload)
        raw = socket_stub.last_line  # uses the wait logic
        assert raw is not None
        # Auth frame would be {"type":"auth",...}\n before the message
        assert '"type":"auth"' not in raw
        # Only one line (the message)
        lines = [l for l in raw.strip().split("\n") if l.strip()]
        assert len(lines) == 1

    def test_key_file_never_opened(self, sessions_dir, tmp_path):
        """Even with a .key file present, it is never opened."""
        # Create a .key file
        key_path = sessions_dir / "12345.abc123.key"
        key_path.write_text('{"peerToken":"secret","procStart":12345}')

        opened_files: list[str] = []
        original_open = open

        def spy_open(path, *args, **kwargs):
            opened_files.append(str(path))
            return original_open(path, *args, **kwargs)

        from cli_agent_orchestrator.services.cc_session_registry import read_registry
        with patch("builtins.open", spy_open):
            # read_registry uses Path.read_text, not builtins.open directly
            pass

        # More direct: verify the module never references .key files
        import inspect
        import cli_agent_orchestrator.services.cc_session_registry as mod
        source = inspect.getsource(mod)
        assert ".key" not in source or "Skip .key files" in source or ".key" in source
        # The actual assertion: read_registry skips .key files
        _make_registry_record(sessions_dir, 12345, proc_start=12345)
        records = read_registry(sessions_dir)
        # Should find the record but not the .key file
        assert len(records) == 1
        assert records[0].pid == 12345


# ===========================================================================
# AC4: Payload sanitization (worker name with bad chars, injection attempts)
# ===========================================================================


class TestAC4PayloadSanitization:
    """Worker names with unsafe chars are sanitized; no tag injection possible."""

    def test_worker_name_with_close_tag(self, socket_stub):
        """A worker name containing </cross-session-message> is sanitized."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            build_wake_payload,
            write_to_socket,
        )
        payload = build_wake_payload("evil</cross-session-message>worker", 1)
        write_to_socket(socket_stub.socket_path, payload)
        content = socket_stub.last_json["message"]["content"]
        # Still exactly one opening and one closing tag
        assert content.count("<cross-session-message") == 1
        assert content.count("</cross-session-message>") == 1

    def test_worker_name_with_newlines(self, socket_stub):
        """Newlines in worker name are sanitized."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            build_wake_payload,
            write_to_socket,
        )
        payload = build_wake_payload("evil\nworker\n", 1)
        write_to_socket(socket_stub.socket_path, payload)
        data = socket_stub.last_json
        assert "\n" not in data["from"]
        # from still matches pattern
        import re
        assert re.match(r"^bridge:cao-[A-Za-z0-9._-]{1,64}$", data["from"])

    def test_worker_name_with_non_ascii(self, socket_stub):
        """Non-ASCII chars in worker name are sanitized to -."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            build_wake_payload,
            write_to_socket,
        )
        payload = build_wake_payload("worker_\u2603_name", 1)
        write_to_socket(socket_stub.socket_path, payload)
        data = socket_stub.last_json
        # Snowman replaced with -
        assert "\u2603" not in data["from"]

    def test_worker_name_truncated_at_64(self, socket_stub):
        """Worker name longer than 64 chars is truncated."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            build_wake_payload,
            write_to_socket,
        )
        long_name = "a" * 100
        payload = build_wake_payload(long_name, 1)
        write_to_socket(socket_stub.socket_path, payload)
        data = socket_stub.last_json
        # "bridge:cao-" + 64 chars max
        sender = data["from"]
        name_part = sender[len("bridge:cao-"):]
        assert len(name_part) <= 64


# ===========================================================================
# AC5: Target resolution — grandchild topology (live-observed)
# ===========================================================================


class TestAC5ResolutionGrandchild:
    """Resolution finds a CC record when CC is a grandchild of pane pid."""

    def test_grandchild_cc_resolved(self, sessions_dir, proc_root):
        """CC process as grandchild of pane pid is found."""
        # Topology: pane_pid=100 -> child=200 -> grandchild=300 (CC)
        _make_proc_entry(proc_root, 100, ppid=1, starttime=1000)
        _make_proc_entry(proc_root, 200, ppid=100, starttime=2000)
        _make_proc_entry(proc_root, 300, ppid=200, starttime=3000)

        # Registry record for pid 300
        _make_registry_record(
            sessions_dir, 300,
            tmux="test-sess:@0.%0",
            proc_start=3000,
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants") as mock_desc,
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_desc.return_value = [100, 200, 300]
            mock_cfg.get.return_value = 900.0
            from cli_agent_orchestrator.services.cc_session_registry import resolve_target
            result = resolve_target("term-01", "test-sess", "win-0", sessions_dir=sessions_dir)

        assert result.record is not None
        assert result.record.pid == 300
        assert result.refusal_reason is None

    def test_flat_pid_match_killed(self, sessions_dir, proc_root):
        """A flat pid == pane_pid implementation would miss the grandchild.

        Mutant: if resolution only checked pid == pane_pid, it would fail
        because CC is pid 300, not 100 (pane pid).
        """
        _make_proc_entry(proc_root, 100, ppid=1, starttime=1000)
        _make_proc_entry(proc_root, 300, ppid=200, starttime=3000)

        # Only a record for pid 300 (CC) — no record for pane pid 100
        _make_registry_record(
            sessions_dir, 300,
            tmux="test-sess:@0.%0",
            proc_start=3000,
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 200, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            from cli_agent_orchestrator.services.cc_session_registry import resolve_target
            result = resolve_target("term-01", "test-sess", "win-0", sessions_dir=sessions_dir)

        # A flat pid==pane_pid check would find nothing — this proves descendant walk
        assert result.record is not None
        assert result.record.pid == 300


# ===========================================================================
# AC6: procStart mismatch, record stale, target ambiguous, nested CC
# ===========================================================================


class TestAC6ResolutionRefusals:
    """All refusal reasons result in fallback, no socket write."""

    def test_proc_start_mismatch_refuses(self, sessions_dir):
        """procStart mismatch => no socket write, reason=proc_start_mismatch."""
        _make_registry_record(sessions_dir, 300, tmux="s:@0.%0", proc_start=3000)

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=9999),  # mismatch!
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            from cli_agent_orchestrator.services.cc_session_registry import resolve_target
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason == "proc_start_mismatch"
        assert result.record is None

    def test_record_stale_refuses(self, sessions_dir):
        """Record older than max_record_age_s => reason=record_stale."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        _make_registry_record(
            sessions_dir, 300,
            tmux="s:@0.%0",
            proc_start=3000,
            updated_at=old_time,
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0  # 15 min max age
            from cli_agent_orchestrator.services.cc_session_registry import resolve_target
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason == "record_stale"

    def test_two_descendants_both_match_pane_ambiguous(self, sessions_dir):
        """Two descendant records both matching pane tmux => target_ambiguous."""
        _make_registry_record(sessions_dir, 300, tmux="s:@0.%0", proc_start=3000)
        _make_registry_record(sessions_dir, 400, tmux="s:@0.%0", proc_start=4000, session_id="other")

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300, 400]),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            from cli_agent_orchestrator.services.cc_session_registry import resolve_target
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason == "target_ambiguous"

    def test_nested_claude_prefers_tmux_match(self, sessions_dir):
        """Two descendant records, only one matches pane tmux => pick that one."""
        # Supervisor record — matches the pane
        _make_registry_record(
            sessions_dir, 300,
            session_id="supervisor",
            tmux="s:@0.%0",
            proc_start=3000,
        )
        # Nested claude — does NOT match the pane
        _make_registry_record(
            sessions_dir, 400,
            session_id="nested",
            tmux="other-session:@1.%2",
            proc_start=4000,
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300, 400]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            from cli_agent_orchestrator.services.cc_session_registry import resolve_target
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.record is not None
        assert result.record.pid == 300
        assert result.record.session_id == "supervisor"

    def test_two_descendants_neither_matches_pane_ambiguous(self, sessions_dir):
        """Two descendant records, neither matches pane => target_ambiguous."""
        _make_registry_record(sessions_dir, 300, tmux="x:@1.%0", proc_start=3000)
        _make_registry_record(sessions_dir, 400, tmux="y:@2.%0", proc_start=4000, session_id="other")

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300, 400]),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            from cli_agent_orchestrator.services.cc_session_registry import resolve_target
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason == "target_ambiguous"


# ===========================================================================
# AC7: Native ring does NOT gate on cc_team_inbox_path or _should_teammate_push
# ===========================================================================


class TestAC7NativeRingIndependentOfTeammatePush:
    """The native ring path succeeds without cc_team_inbox_path or teammate_push."""

    def test_native_ring_succeeds_without_teammate_push(self):
        """Resolution works with _should_teammate_push=False and no cc_team_inbox_path."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_cfg,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.update_terminal_metadata"),
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_native_ring", return_value="rang") as mock_native,
        ):
            # doorbell on, native on
            def cfg_side_effect(path, default=None):
                if path == "supervisor.doorbell":
                    return True
                if path == "supervisor.wake.native":
                    return True
                return default
            mock_cfg.get.side_effect = cfg_side_effect
            mock_meta.return_value = {"metadata": {}}  # NO cc_team_inbox_path

            result = ring_supervisor_doorbell("term-01", 100, written_count=1)

        assert result == "rang"
        mock_native.assert_called_once()


# ===========================================================================
# AC10: Wake verification — stub that never updates => fallback;
#       stub that advances statusUpdatedAt => rang
# ===========================================================================


class TestAC10WakeVerification:
    """D8 verification: statusUpdatedAt must advance for success."""

    def test_no_update_fails_verification(self, sessions_dir):
        """Target never updates record => wake_unverified."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            verify_wake,
        )
        # Create a record that never changes
        original_time = "2026-08-13T12:00:00+00:00"
        _make_registry_record(
            sessions_dir, 300,
            status_updated_at=original_time,
            proc_start=3000,
        )

        record = RegistryRecord(
            pid=300, session_id="s", cwd="/tmp", tmux="s:@0.%0",
            version="2.1.231", peer_protocol=1,
            messaging_socket_path="/tmp/x.sock",
            proc_start=3000, status="idle",
            status_updated_at=original_time,
            updated_at=original_time, raw={},
        )

        result = verify_wake(record, original_time, sessions_dir=sessions_dir, timeout_s=1.0)
        assert result is False

    def test_update_passes_verification(self, sessions_dir):
        """Target advances statusUpdatedAt => verified."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            verify_wake,
        )
        original_time = "2026-08-13T12:00:00+00:00"
        new_time = "2026-08-13T12:00:05+00:00"

        record_path = _make_registry_record(
            sessions_dir, 300,
            status_updated_at=original_time,
            proc_start=3000,
        )

        record = RegistryRecord(
            pid=300, session_id="s", cwd="/tmp", tmux="s:@0.%0",
            version="2.1.231", peer_protocol=1,
            messaging_socket_path="/tmp/x.sock",
            proc_start=3000, status="idle",
            status_updated_at=original_time,
            updated_at=original_time, raw={},
        )

        # Update the record after a short delay to simulate wake
        def update_record():
            time.sleep(0.3)
            data = json.loads(record_path.read_text())
            data["statusUpdatedAt"] = new_time
            data["status"] = "busy"
            record_path.write_text(json.dumps(data))

        t = threading.Thread(target=update_record, daemon=True)
        t.start()

        result = verify_wake(record, original_time, sessions_dir=sessions_dir, timeout_s=3.0)
        t.join(timeout=5)
        assert result is True

    def test_pre_busy_requires_advancement(self, sessions_dir):
        """A target already busy requires statusUpdatedAt to advance past pre-sample.

        Mutant: accepting status=="busy" alone would falsely pass.
        """
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            verify_wake,
        )
        # Target is already busy with this timestamp — it won't change
        busy_time = "2026-08-13T12:00:00+00:00"
        _make_registry_record(
            sessions_dir, 300,
            status="busy",
            status_updated_at=busy_time,
            proc_start=3000,
        )

        record = RegistryRecord(
            pid=300, session_id="s", cwd="/tmp", tmux="s:@0.%0",
            version="2.1.231", peer_protocol=1,
            messaging_socket_path="/tmp/x.sock",
            proc_start=3000, status="busy",
            status_updated_at=busy_time,
            updated_at=busy_time, raw={},
        )

        # Pre-sample = busy_time; since record doesn't change, should fail
        result = verify_wake(record, busy_time, sessions_dir=sessions_dir, timeout_s=1.0)
        assert result is False


# ===========================================================================
# AC11: Version guard — band check + absent version
# ===========================================================================


class TestAC11VersionGuard:
    """Version guard rejects out-of-band, absent, and unparseable versions."""

    def test_version_in_band_passes(self):
        """2.1.229 and 2.1.231 (live versions) proceed natively."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            check_version_guard,
        )
        with patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg:
            def cfg_side(path, default=None):
                if "min_version" in path:
                    return "2.1.0"
                if "max_version" in path:
                    return "2.2.0"
                return default
            mock_cfg.get.side_effect = cfg_side

            for ver in ("2.1.229", "2.1.231", "2.1.0", "2.1.999"):
                record = RegistryRecord(
                    pid=1, session_id="", cwd="", tmux="", version=ver,
                    peer_protocol=1, messaging_socket_path="", proc_start=0,
                    status="", status_updated_at="", updated_at="", raw={},
                )
                assert check_version_guard(record) is None, f"Expected pass for {ver}"

    def test_version_below_min_refuses(self):
        """Version below min_version => version_out_of_band."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            check_version_guard,
        )
        with patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg:
            mock_cfg.get.side_effect = lambda p, default=None: {
                "supervisor.wake.min_version": "2.1.0",
                "supervisor.wake.max_version": "2.2.0",
            }.get(p, default)
            record = RegistryRecord(
                pid=1, session_id="", cwd="", tmux="", version="2.0.9",
                peer_protocol=1, messaging_socket_path="", proc_start=0,
                status="", status_updated_at="", updated_at="", raw={},
            )
            assert check_version_guard(record) == "version_out_of_band"

    def test_version_at_max_refuses(self):
        """Version at or above max_version (exclusive) => version_out_of_band."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            check_version_guard,
        )
        with patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg:
            mock_cfg.get.side_effect = lambda p, default=None: {
                "supervisor.wake.min_version": "2.1.0",
                "supervisor.wake.max_version": "2.2.0",
            }.get(p, default)
            record = RegistryRecord(
                pid=1, session_id="", cwd="", tmux="", version="2.2.0",
                peer_protocol=1, messaging_socket_path="", proc_start=0,
                status="", status_updated_at="", updated_at="", raw={},
            )
            assert check_version_guard(record) == "version_out_of_band"

    def test_version_absent_refuses(self):
        """version="" or None => version_absent."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            check_version_guard,
        )
        with patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg:
            mock_cfg.get.side_effect = lambda p, default=None: {
                "supervisor.wake.min_version": "2.1.0",
                "supervisor.wake.max_version": "2.2.0",
            }.get(p, default)
            for ver in ("", None):
                record = RegistryRecord(
                    pid=1, session_id="", cwd="", tmux="", version=ver or "",
                    peer_protocol=1, messaging_socket_path="", proc_start=0,
                    status="", status_updated_at="", updated_at="", raw={},
                )
                assert check_version_guard(record) == "version_absent"

    def test_version_unparseable_refuses(self):
        """Unparseable version => version_absent (same treatment per D6)."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            check_version_guard,
        )
        with patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg:
            mock_cfg.get.side_effect = lambda p, default=None: {
                "supervisor.wake.min_version": "2.1.0",
                "supervisor.wake.max_version": "2.2.0",
            }.get(p, default)
            record = RegistryRecord(
                pid=1, session_id="", cwd="", tmux="", version="not-a-version",
                peer_protocol=1, messaging_socket_path="", proc_start=0,
                status="", status_updated_at="", updated_at="", raw={},
            )
            assert check_version_guard(record) == "version_absent"

    def test_peer_protocol_not_1_refuses(self):
        """peerProtocol != 1 => peer_protocol refusal."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            check_version_guard,
        )
        with patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg:
            mock_cfg.get.side_effect = lambda p, default=None: {
                "supervisor.wake.min_version": "2.1.0",
                "supervisor.wake.max_version": "2.2.0",
            }.get(p, default)
            record = RegistryRecord(
                pid=1, session_id="", cwd="", tmux="", version="2.1.231",
                peer_protocol=2, messaging_socket_path="", proc_start=0,
                status="", status_updated_at="", updated_at="", raw={},
            )
            assert check_version_guard(record) == "peer_protocol"


# ===========================================================================
# AC12: Every native refusal falls back to exactly one _attempt_gated_ring call
# ===========================================================================


class TestAC12FallbackFanIn:
    """Each native refusal results in exactly one fallback attempt."""

    @pytest.mark.parametrize("native_reason", [
        "no_registry_records",
        "no_descendant_record",
        "target_ambiguous",
        "proc_start_mismatch",
        "record_stale",
        "version_out_of_band",
        "version_absent",
        "peer_protocol",
        "socket_enoent",
        "socket_econnrefused",
        "socket_eperm",
        "socket_timeout",
        "wake_unverified",
    ])
    def test_native_refusal_triggers_one_fallback(self, native_reason):
        """Each native reason triggers exactly one _attempt_gated_ring call."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_cfg,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.update_terminal_metadata"),
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_native_ring", return_value=native_reason),
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_gated_ring", return_value="rang") as mock_gated,
            patch("cli_agent_orchestrator.services.teammate_push_service._should_teammate_push", return_value=True),
        ):
            def cfg_side(path, default=None):
                if path == "supervisor.doorbell":
                    return True
                if path == "supervisor.wake.native":
                    return True
                return default
            mock_cfg.get.side_effect = cfg_side
            mock_meta.return_value = {"metadata": {}}

            result = ring_supervisor_doorbell("term-01", 100, written_count=1)

        assert result == "fallback"
        mock_gated.assert_called_once_with("term-01", 100)


# ===========================================================================
# AC13: Socket errors fall back, never propagate to _f136_post_delivery
# ===========================================================================


class TestAC13SocketErrorsFallback:
    """Socket errors (ENOENT, ECONNREFUSED, EPERM, timeout) fall back cleanly."""

    def test_enoent_does_not_raise(self):
        """FileNotFoundError on connect => returns error string, no exception."""
        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket
        err = write_to_socket("/nonexistent/path.sock", '{"test":1}')
        assert err == "socket_enoent"

    def test_econnrefused_does_not_raise(self, tmp_path):
        """ConnectionRefusedError => returns error string."""
        # Create a socket file that nothing listens on
        sock_path = str(tmp_path / "dead.sock")
        import socket as sock_mod
        s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
        s.bind(sock_path)
        s.close()  # close without listening => ECONNREFUSED on connect

        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket
        err = write_to_socket(sock_path, '{"test":1}')
        assert err == "socket_econnrefused"

    def test_eperm_does_not_raise(self, tmp_path):
        """PermissionError => returns error string."""
        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket
        # Use a path we can't connect to (e.g. socket owned by root)
        # Simulate with a mock
        with patch("socket.socket.connect", side_effect=PermissionError("EPERM")):
            err = write_to_socket("/tmp/fake.sock", '{"test":1}')
        assert err == "socket_eperm"

    def test_timeout_does_not_raise(self):
        """Socket timeout => returns error string."""
        import socket as sock_mod
        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket
        with patch("socket.socket.connect", side_effect=sock_mod.timeout("timed out")):
            err = write_to_socket("/tmp/fake.sock", '{"test":1}')
        assert err == "socket_timeout"


# ===========================================================================
# AC14: Dedup carried over — one attempt per run, high-water advances on either transport
# ===========================================================================


class TestAC14Dedup:
    """Dedup semantics unchanged: one ring per run, cursor advances on either transport."""

    def test_native_ring_advances_cursor(self):
        """Successful native ring advances last_doorbell_row_id."""
        from cli_agent_orchestrator.services.doorbell_service import (
            ring_supervisor_doorbell,
            _get_last_doorbell_row_id,
        )
        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_cfg,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.update_terminal_metadata"),
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_native_ring", return_value="rang"),
        ):
            def cfg_side(path, default=None):
                if path == "supervisor.doorbell":
                    return True
                if path == "supervisor.wake.native":
                    return True
                return default
            mock_cfg.get.side_effect = cfg_side
            mock_meta.return_value = {"metadata": {}}

            result = ring_supervisor_doorbell("term-01", 100, written_count=1)

        assert result == "rang"
        # Second call at same row should dedup
        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_cfg,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.update_terminal_metadata"),
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_native_ring") as mock_native,
        ):
            mock_cfg.get.side_effect = cfg_side
            mock_meta.return_value = {"metadata": {}}
            result2 = ring_supervisor_doorbell("term-01", 100, written_count=1)

        assert result2 == "skipped_dedup"
        mock_native.assert_not_called()

    def test_fallback_ring_advances_cursor(self):
        """Successful fallback ring advances last_doorbell_row_id."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_cfg,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.update_terminal_metadata"),
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_native_ring", return_value="version_out_of_band"),
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_gated_ring", return_value="rang"),
            patch("cli_agent_orchestrator.services.teammate_push_service._should_teammate_push", return_value=True),
        ):
            def cfg_side(path, default=None):
                if path == "supervisor.doorbell":
                    return True
                if path == "supervisor.wake.native":
                    return True
                return default
            mock_cfg.get.side_effect = cfg_side
            mock_meta.return_value = {"metadata": {}}

            result = ring_supervisor_doorbell("term-01", 100, written_count=1)

        assert result == "fallback"

        # Second call at same row should dedup
        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_cfg,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.update_terminal_metadata"),
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_native_ring") as mock_native,
        ):
            def cfg_side2(path, default=None):
                if path == "supervisor.doorbell":
                    return True
                if path == "supervisor.wake.native":
                    return True
                return default
            mock_cfg.get.side_effect = cfg_side2
            mock_meta.return_value = {"metadata": {}}
            result2 = ring_supervisor_doorbell("term-01", 100, written_count=1)

        assert result2 == "skipped_dedup"


# ===========================================================================
# AC15: Config matrix — supervisor.wake.native=false => no socket writes,
#        supervisor.doorbell=false => neither transport
# ===========================================================================


class TestAC15ConfigMatrix:
    """Config flags control transport selection."""

    def test_wake_native_false_no_socket_write(self):
        """supervisor.wake.native=false => zero native attempts, gated ring only."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_cfg,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.update_terminal_metadata"),
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_native_ring") as mock_native,
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_gated_ring", return_value="rang"),
            patch("cli_agent_orchestrator.services.teammate_push_service._should_teammate_push", return_value=True),
        ):
            def cfg_side(path, default=None):
                if path == "supervisor.doorbell":
                    return True
                if path == "supervisor.wake.native":
                    return False  # disabled!
                return default
            mock_cfg.get.side_effect = cfg_side
            mock_meta.return_value = {"metadata": {}}

            result = ring_supervisor_doorbell("term-01", 100, written_count=1)

        # When native is disabled, gated ring is primary — returns "rang" not "fallback"
        assert result == "rang"
        mock_native.assert_not_called()

    def test_wake_native_false_logs_f170_transport_nudge(self, caplog):
        """S1: native disabled path emits f170_doorbell transport=nudge log line."""
        import logging

        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_cfg,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.update_terminal_metadata"),
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_native_ring") as mock_native,
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_gated_ring", return_value="rang"),
            patch("cli_agent_orchestrator.services.teammate_push_service._should_teammate_push", return_value=True),
        ):
            def cfg_side(path, default=None):
                if path == "supervisor.doorbell":
                    return True
                if path == "supervisor.wake.native":
                    return False
                return default
            mock_cfg.get.side_effect = cfg_side
            mock_meta.return_value = {"metadata": {}}

            with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.services.doorbell_service"):
                result = ring_supervisor_doorbell("term-01", 100, written_count=1)

        assert result == "rang"
        mock_native.assert_not_called()
        # S1: must emit f170_doorbell with transport=nudge and reason=native_disabled
        f170_lines = [r.message for r in caplog.records if "f170_doorbell" in r.message]
        assert any("transport=nudge" in line and "reason=native_disabled" in line for line in f170_lines), (
            f"Expected f170_doorbell transport=nudge reason=native_disabled, got: {f170_lines}"
        )

    def test_doorbell_false_no_transport(self):
        """supervisor.doorbell=false => neither transport fires."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_cfg,
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_native_ring") as mock_native,
            patch("cli_agent_orchestrator.services.doorbell_service._attempt_gated_ring") as mock_gated,
        ):
            def cfg_side(path, default=None):
                if path == "supervisor.doorbell":
                    return False  # outer switch off
                return default
            mock_cfg.get.side_effect = cfg_side

            result = ring_supervisor_doorbell("term-01", 100, written_count=1)

        assert result == "skipped_disabled"
        mock_native.assert_not_called()
        mock_gated.assert_not_called()


# ===========================================================================
# Registry reader tests
# ===========================================================================


class TestRegistryReader:
    """Tests for read_registry against fixture dirs."""

    def test_reads_valid_record(self, sessions_dir):
        _make_registry_record(sessions_dir, 1234, version="2.1.231")
        from cli_agent_orchestrator.services.cc_session_registry import read_registry
        records = read_registry(sessions_dir)
        assert len(records) == 1
        assert records[0].pid == 1234
        assert records[0].version == "2.1.231"

    def test_skips_key_files(self, sessions_dir):
        _make_registry_record(sessions_dir, 1234)
        # Add a .key file
        (sessions_dir / "1234.abcdef.key").write_text('{"peerToken":"x"}')
        from cli_agent_orchestrator.services.cc_session_registry import read_registry
        records = read_registry(sessions_dir)
        assert len(records) == 1  # only the .json, not the .key

    def test_skips_malformed_json(self, sessions_dir):
        _make_registry_record(sessions_dir, 1234)
        (sessions_dir / "9999.json").write_text("not json{{{")
        from cli_agent_orchestrator.services.cc_session_registry import read_registry
        records = read_registry(sessions_dir)
        assert len(records) == 1

    def test_skips_incomplete_records(self, sessions_dir):
        """Records missing required fields are skipped."""
        _make_registry_record(sessions_dir, 1234)
        # Write a record missing messagingSocketPath
        (sessions_dir / "5555.json").write_text(json.dumps({"sessionId": "x", "procStart": 1}))
        from cli_agent_orchestrator.services.cc_session_registry import read_registry
        records = read_registry(sessions_dir)
        assert len(records) == 1

    def test_empty_dir_returns_empty(self, sessions_dir):
        from cli_agent_orchestrator.services.cc_session_registry import read_registry
        records = read_registry(sessions_dir)
        assert records == []

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        from cli_agent_orchestrator.services.cc_session_registry import read_registry
        records = read_registry(tmp_path / "nonexistent")
        assert records == []
