"""F216 — null messagingSocketPath: parse-time normalization + socket_unpublished gate.

Revert-sensitive tests: reverting the F216 fix makes these fail (not flake).

- Registry record with messagingSocketPath:null → normalized to ""
- Resolution/ring refuses with "socket_unpublished" BEFORE any socket connect attempt
- Verdict fields are computed (not constant-assigned) — tested via distinct inputs
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def sessions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture()
def proc_root(tmp_path: Path) -> Path:
    d = tmp_path / "proc"
    d.mkdir()
    return d


def _make_record_json(
    sessions_dir: Path,
    pid: int,
    *,
    messaging_socket_path=None,
    session_id: str = "sess-1",
    version: str = "2.1.231",
    peer_protocol: int = 1,
    tmux: str = "cao-test:@0.%0",
    proc_start: int = 99999,
) -> Path:
    """Write a registry JSON record.  messaging_socket_path=None writes JSON null."""
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "sessionId": session_id,
        "cwd": "/tmp",
        "tmux": tmux,
        "version": version,
        "peerProtocol": peer_protocol,
        "messagingSocketPath": messaging_socket_path,  # None → JSON null
        "procStart": proc_start,
        "status": "idle",
        "statusUpdatedAt": now,
        "updatedAt": now,
    }
    path = sessions_dir / f"{pid}.json"
    path.write_text(json.dumps(data))
    return path


def _make_proc_entry(proc_root: Path, pid: int, ppid: int, starttime: int = 99999):
    entry_dir = proc_root / str(pid)
    entry_dir.mkdir(parents=True, exist_ok=True)
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(starttime)] + ["0"] * 10
    stat_line = f"{pid} (test) " + " ".join(fields)
    (entry_dir / "stat").write_text(stat_line)


# ===========================================================================
# Test: null messagingSocketPath is normalized to "" at parse time
# ===========================================================================


class TestF216NullSocketPathParsing:
    """read_registry normalizes explicit JSON null to empty string."""

    def test_explicit_null_becomes_empty_string(self, sessions_dir):
        """messagingSocketPath: null in JSON → record.messaging_socket_path == ''."""
        _make_record_json(sessions_dir, 100, messaging_socket_path=None)
        from cli_agent_orchestrator.services.cc_session_registry import read_registry

        records = read_registry(sessions_dir)
        assert len(records) == 1
        assert records[0].messaging_socket_path == ""

    def test_absent_field_becomes_empty_string(self, sessions_dir):
        """Missing messagingSocketPath key → record.messaging_socket_path == ''."""
        now = datetime.now(timezone.utc).isoformat()
        data = {
            "sessionId": "s1",
            "cwd": "/tmp",
            "tmux": "",
            "version": "2.1.232",
            "peerProtocol": 1,
            # messagingSocketPath intentionally absent
            "procStart": 123,
            "status": "idle",
            "statusUpdatedAt": now,
            "updatedAt": now,
        }
        (sessions_dir / "200.json").write_text(json.dumps(data))
        from cli_agent_orchestrator.services.cc_session_registry import read_registry

        records = read_registry(sessions_dir)
        assert len(records) == 1
        assert records[0].messaging_socket_path == ""

    def test_valid_path_preserved(self, sessions_dir):
        """A real path is preserved unmodified."""
        _make_record_json(sessions_dir, 300, messaging_socket_path="/run/user/1000/cc.sock")
        from cli_agent_orchestrator.services.cc_session_registry import read_registry

        records = read_registry(sessions_dir)
        assert len(records) == 1
        assert records[0].messaging_socket_path == "/run/user/1000/cc.sock"

    @pytest.mark.parametrize(
        "field,json_key",
        [
            ("session_id", "sessionId"),
            ("cwd", "cwd"),
            ("tmux", "tmux"),
            ("version", "version"),
            ("status", "status"),
        ],
    )
    def test_all_string_fields_null_safe(self, sessions_dir, field, json_key):
        """Every string field in RegistryRecord normalizes null → ''."""
        now = datetime.now(timezone.utc).isoformat()
        data = {
            "sessionId": "s",
            "cwd": "/tmp",
            "tmux": "x:@0.%0",
            "version": "2.1.231",
            "peerProtocol": 1,
            "messagingSocketPath": "/sock",
            "procStart": 1,
            "status": "idle",
            "statusUpdatedAt": now,
            "updatedAt": now,
        }
        # Set the target field to null
        data[json_key] = None
        (sessions_dir / "400.json").write_text(json.dumps(data))
        from cli_agent_orchestrator.services.cc_session_registry import read_registry

        records = read_registry(sessions_dir)
        assert len(records) == 1
        assert getattr(records[0], field) == ""


# ===========================================================================
# Test: _attempt_native_ring refuses with socket_unpublished, zero connects
# ===========================================================================


class TestF216SocketUnpublishedGate:
    """Ring refuses before any socket.connect when socket path is empty."""

    def test_ring_returns_socket_unpublished_on_null_path(
        self, sessions_dir, proc_root
    ):
        """_attempt_native_ring returns 'socket_unpublished' for null socket path.

        Revert-sensitive: without the gate, the code would call sock.connect("")
        → OSError EINVAL, returning "socket_error:22" (not "socket_unpublished").
        """
        # Create a valid record with null socket path
        _make_record_json(sessions_dir, 500, messaging_socket_path=None)
        # Create proc tree: pane_pid=400 → child=500
        _make_proc_entry(proc_root, 400, ppid=1, starttime=88888)
        _make_proc_entry(proc_root, 500, ppid=400, starttime=99999)

        mock_socket = MagicMock()

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={"tmux_session": "cao-test", "tmux_window": "win-0"},
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.pane_pid",
                return_value=400,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._PROC_ROOT",
                proc_root,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry._sessions_dir",
                return_value=sessions_dir,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id",
                return_value="@0",
            ),
            patch("socket.socket", mock_socket),
        ):
            from cli_agent_orchestrator.services.doorbell_service import (
                _attempt_native_ring,
            )

            result = _attempt_native_ring("term-f216", 42)

        assert result == "socket_unpublished"
        # CRITICAL: zero socket.connect() attempts
        mock_socket.return_value.connect.assert_not_called()

    def test_ring_returns_socket_unpublished_on_empty_string_path(
        self, sessions_dir, proc_root
    ):
        """Explicit empty string also triggers socket_unpublished gate."""
        _make_record_json(sessions_dir, 600, messaging_socket_path="")
        _make_proc_entry(proc_root, 400, ppid=1, starttime=88888)
        _make_proc_entry(proc_root, 600, ppid=400, starttime=99999)

        mock_socket = MagicMock()

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={"tmux_session": "cao-test", "tmux_window": "win-0"},
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.pane_pid",
                return_value=400,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._PROC_ROOT",
                proc_root,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry._sessions_dir",
                return_value=sessions_dir,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id",
                return_value="@0",
            ),
            patch("socket.socket", mock_socket),
        ):
            from cli_agent_orchestrator.services.doorbell_service import (
                _attempt_native_ring,
            )

            result = _attempt_native_ring("term-f216-empty", 43)

        assert result == "socket_unpublished"
        mock_socket.return_value.connect.assert_not_called()

    def test_verdict_fields_computed_not_constant(self, sessions_dir, proc_root):
        """Verifies that different inputs produce distinct refusal reasons.

        If the code just returned a hardcoded string, this wouldn't pass for
        both the socket_unpublished case AND a version guard failure.
        """
        # Case 1: null socket → socket_unpublished
        _make_record_json(sessions_dir, 700, messaging_socket_path=None)
        _make_proc_entry(proc_root, 400, ppid=1, starttime=88888)
        _make_proc_entry(proc_root, 700, ppid=400, starttime=99999)

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={"tmux_session": "cao-test", "tmux_window": "win-0"},
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.pane_pid",
                return_value=400,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._PROC_ROOT",
                proc_root,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry._sessions_dir",
                return_value=sessions_dir,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id",
                return_value="@0",
            ),
        ):
            from cli_agent_orchestrator.services.doorbell_service import (
                _attempt_native_ring,
            )

            result_null_socket = _attempt_native_ring("term-f216-v", 44)

        # Case 2: bad version → version_out_of_band
        # Use a sessions_dir2 to avoid cross-contamination
        sessions_dir2 = sessions_dir.parent / "sessions2"
        sessions_dir2.mkdir()
        _make_record_json(
            sessions_dir2,
            800,
            messaging_socket_path="/tmp/valid.sock",
            version="99.0.0",  # way out of band
        )
        _make_proc_entry(proc_root, 800, ppid=400, starttime=99999)

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={"tmux_session": "cao-test", "tmux_window": "win-0"},
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.pane_pid",
                return_value=400,
            ),
            patch(
                "cli_agent_orchestrator.services.fork_context_service._PROC_ROOT",
                proc_root,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry._sessions_dir",
                return_value=sessions_dir2,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id",
                return_value="@0",
            ),
        ):
            result_bad_version = _attempt_native_ring("term-f216-v2", 45)

        # Different inputs → different verdicts (computed, not constant)
        assert result_null_socket == "socket_unpublished"
        assert result_bad_version == "version_out_of_band"
        assert result_null_socket != result_bad_version
