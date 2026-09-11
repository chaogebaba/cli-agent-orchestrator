"""F216 — null messagingSocketPath: parse-time normalization + socket_unpublished gate.

Revert-sensitive tests: reverting the F216 fix makes these fail (not flake).

- Registry record with messagingSocketPath:null → normalized to ""
- Resolution/emission refuses with "socket_unpublished" BEFORE any socket connect attempt
- Verdict fields are computed (not constant-assigned) — tested via distinct inputs
"""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
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
# Test: the seat carrier refuses with socket_unpublished, zero connects
# ===========================================================================
#
# WP-ARCH 3c K3: these three arms drove ``doorbell_service._attempt_native_ring``
# when they were written. That function is deleted, but the gate it held is not
# retired — it MOVED. ``queue_carrier.NativeSeatCarrier.emit`` is the seat's only
# native emitter now, and it reaches the same four steps through the same
# ``cc_session_registry`` functions: resolve, version guard, socket check, write.
# So the arms are re-pointed at the new owner rather than deleted; what they pin
# is unchanged — an unpublished socket is refused BEFORE any connect, and the
# refusal string is computed from the input rather than constant.
#
# The carrier's own suite (``test/app/delivery/test_seat_wake.py``) stubs the
# carrier and sets ``reason`` by hand, so it asserts what the tick does WITH a
# refusal. These are the only arms that assert the real ``emit`` PRODUCES one.


def _emit(terminal_id: str):
    """Drive the real carrier and hand back its refusal reason."""
    from cli_agent_orchestrator.services.queue_carrier import NativeSeatCarrier

    return NativeSeatCarrier().emit(
        terminal_id=terminal_id,
        line="wake",
        sender_key="w1",
        sender_name="worker-1",
        msg_id="msg-f216",
    )


@contextmanager
def _carrier_patches(sessions_dir: Path, proc_root: Path, pane_pid: int = 400):
    """The resolution context ``emit`` runs in: one pane, one synthetic /proc."""
    patches = (
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"tmux_session": "cao-test", "tmux_window": "win-0"},
        ),
        patch(
            "cli_agent_orchestrator.services.cc_session_registry.first_pane",
            return_value=("%0", pane_pid),
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
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


class TestF216SocketUnpublishedGate:
    """The carrier refuses before any socket.connect when the socket path is empty."""

    def test_ring_returns_socket_unpublished_on_null_path(self, sessions_dir, proc_root):
        """A null socket path yields 'socket_unpublished', not a connect error.

        Revert-sensitive: without the gate, the code would call sock.connect("")
        → OSError EINVAL, returning "socket_error:22" (not "socket_unpublished").
        """
        _make_record_json(sessions_dir, 500, messaging_socket_path=None)
        # Create proc tree: pane_pid=400 → child=500
        _make_proc_entry(proc_root, 400, ppid=1, starttime=88888)
        _make_proc_entry(proc_root, 500, ppid=400, starttime=99999)

        mock_socket = MagicMock()
        with (
            _carrier_patches(sessions_dir, proc_root),
            patch("socket.socket", mock_socket),
        ):
            emission = _emit("term-f216")

        assert emission.reason == "socket_unpublished"
        # CRITICAL: zero socket.connect() attempts
        mock_socket.return_value.connect.assert_not_called()

    def test_ring_returns_socket_unpublished_on_empty_string_path(self, sessions_dir, proc_root):
        """Explicit empty string also triggers socket_unpublished gate."""
        _make_record_json(sessions_dir, 600, messaging_socket_path="")
        _make_proc_entry(proc_root, 400, ppid=1, starttime=88888)
        _make_proc_entry(proc_root, 600, ppid=400, starttime=99999)

        mock_socket = MagicMock()
        with (
            _carrier_patches(sessions_dir, proc_root),
            patch("socket.socket", mock_socket),
        ):
            emission = _emit("term-f216-empty")

        assert emission.reason == "socket_unpublished"
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

        with _carrier_patches(sessions_dir, proc_root):
            result_null_socket = _emit("term-f216-v").reason

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

        with _carrier_patches(sessions_dir2, proc_root):
            result_bad_version = _emit("term-f216-v2").reason

        # Different inputs → different verdicts (computed, not constant)
        assert result_null_socket == "socket_unpublished"
        assert result_bad_version == "version_out_of_band"
        assert result_null_socket != result_bad_version


# ===========================================================================
# Test: a non-object registry DOCUMENT is skipped, not raised (F216 #55)
# ===========================================================================


class TestF216NonObjectDocument:
    """The parse surface survives an externally-written non-object JSON file.

    Revert-sensitive: without the ``isinstance(data, dict)`` guard the minimum
    -field membership test raises TypeError out of ``read_registry`` and every
    caller on the delivery path dies over one bad file.
    """

    @pytest.mark.parametrize("payload", ["null", '"a string"', "42", "[1, 2, 3]", "true"])
    def test_non_object_document_is_skipped(self, sessions_dir, payload):
        (sessions_dir / "900.json").write_text(payload)
        from cli_agent_orchestrator.services.cc_session_registry import read_registry

        assert read_registry(sessions_dir) == []

    def test_valid_record_survives_a_null_sibling(self, sessions_dir):
        """One poisoned file must not erase the healthy records beside it."""
        (sessions_dir / "901.json").write_text("null")
        _make_record_json(sessions_dir, 902, messaging_socket_path="/run/user/1000/cc.sock")
        from cli_agent_orchestrator.services.cc_session_registry import read_registry

        records = read_registry(sessions_dir)
        assert [r.pid for r in records] == [902]
        assert records[0].messaging_socket_path == "/run/user/1000/cc.sock"
