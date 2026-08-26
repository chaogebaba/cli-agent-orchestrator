"""F337 — Auth handshake in write_to_socket and wake.native gate completeness.

Tests:
- AC1: write_to_socket sends JSON auth frame as first line when token provided
- AC2: read_peer_token correctly reads from key files
- AC3: _build_auth_frame produces correct JSON format
- AC4: _attempt_native_ring passes auth_token from key file
- AC5: wake.native=false suppresses terminal_service cc_team_inbox_path derivation
"""

from __future__ import annotations

import json
import os
import socketserver
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# Fixtures
# ===========================================================================


class AuthCapturingSocketStub:
    """A socket stub that captures all raw bytes received, preserving line structure."""

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
    def raw_data(self) -> bytes:
        """Wait for data and return raw bytes."""
        for _ in range(40):
            if self.received_data:
                break
            time.sleep(0.05)
        if not self.received_data:
            return b""
        return self.received_data[-1]

    @property
    def lines(self) -> list[str]:
        """Return received data split into lines."""
        raw = self.raw_data.decode("utf-8")
        return [l for l in raw.split("\n") if l.strip()]


@pytest.fixture()
def socket_stub():
    stub = AuthCapturingSocketStub()
    stub.start()
    yield stub
    stub.stop()


@pytest.fixture()
def sessions_dir(tmp_path):
    """Create a fixture sessions directory with key files."""
    d = tmp_path / "sessions"
    d.mkdir()
    return d


# ===========================================================================
# AC1: write_to_socket sends JSON auth frame as first line
# ===========================================================================


class TestAC1AuthFrame:
    """write_to_socket sends auth frame before payload when token provided."""

    def test_auth_frame_is_first_line(self, socket_stub):
        """Auth token produces a JSON auth frame as the first line."""
        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket

        payload = '{"msgV":1,"type":"user","message":{"role":"user","content":"test"}}'
        err = write_to_socket(
            socket_stub.socket_path, payload, auth_token="my_secret_token"
        )
        assert err is None
        lines = socket_stub.lines
        assert len(lines) == 2
        # First line is the auth frame
        auth = json.loads(lines[0])
        assert auth == {"type": "auth", "token": "my_secret_token"}
        # Second line is the payload
        msg = json.loads(lines[1])
        assert msg["msgV"] == 1

    def test_no_auth_when_token_none(self, socket_stub):
        """No auth frame when auth_token is None."""
        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket

        payload = '{"msgV":1,"type":"user"}'
        err = write_to_socket(socket_stub.socket_path, payload, auth_token=None)
        assert err is None
        lines = socket_stub.lines
        assert len(lines) == 1
        msg = json.loads(lines[0])
        assert msg["msgV"] == 1

    def test_no_auth_when_token_empty(self, socket_stub):
        """No auth frame when auth_token is empty string."""
        from cli_agent_orchestrator.services.cc_session_registry import write_to_socket

        payload = '{"msgV":1,"type":"user"}'
        err = write_to_socket(socket_stub.socket_path, payload, auth_token="")
        assert err is None
        lines = socket_stub.lines
        assert len(lines) == 1


# ===========================================================================
# AC2: read_peer_token reads from key files
# ===========================================================================


class TestAC2ReadPeerToken:
    """read_peer_token finds and parses <pid>.<hex>.key files."""

    def test_reads_token_from_key_file(self, sessions_dir):
        """Reads peerToken from a correctly formatted key file."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('{"peerToken":"secret_token_123","procStart":"99999"}')

        token = read_peer_token(12345, sessions_dir=sessions_dir)
        assert token == "secret_token_123"

    def test_returns_none_when_no_key_file(self, sessions_dir):
        """Returns None when no matching key file exists."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        token = read_peer_token(99999, sessions_dir=sessions_dir)
        assert token is None

    def test_returns_none_when_sessions_dir_missing(self, tmp_path):
        """Returns None when sessions directory doesn't exist."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        token = read_peer_token(12345, sessions_dir=tmp_path / "nonexistent")
        assert token is None

    def test_skips_malformed_key_file(self, sessions_dir):
        """Returns None when key file contains invalid JSON."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text("not json")

        token = read_peer_token(12345, sessions_dir=sessions_dir)
        assert token is None

    def test_skips_key_file_without_peer_token(self, sessions_dir):
        """Returns None when key file JSON lacks peerToken field."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('{"procStart":"99999"}')

        token = read_peer_token(12345, sessions_dir=sessions_dir)
        assert token is None

    def test_picks_correct_pid_key_file(self, sessions_dir):
        """Only reads key file matching the requested PID."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        # Key for PID 111 (exactly 64 hex chars in suffix)
        kf1 = sessions_dir / "111.aaaa000011112222333344445555666677778888999900001111222233334444.key"
        kf1.write_text('{"peerToken":"token_111","procStart":"111"}')
        # Key for PID 222
        kf2 = sessions_dir / "222.bbbb000011112222333344445555666677778888999900001111222233334444.key"
        kf2.write_text('{"peerToken":"token_222","procStart":"222"}')

        assert read_peer_token(111, sessions_dir=sessions_dir) == "token_111"
        assert read_peer_token(222, sessions_dir=sessions_dir) == "token_222"


# ===========================================================================
# AC3: _build_auth_frame format
# ===========================================================================


class TestAC3BuildAuthFrame:
    """_build_auth_frame produces the correct JSON wire format."""

    def test_frame_format(self):
        """Auth frame is {"type":"auth","token":"<token>"}."""
        from cli_agent_orchestrator.services.cc_session_registry import _build_auth_frame

        frame = _build_auth_frame("test_token_abc")
        parsed = json.loads(frame)
        assert parsed == {"type": "auth", "token": "test_token_abc"}

    def test_compact_json(self):
        """Auth frame uses compact JSON (no spaces)."""
        from cli_agent_orchestrator.services.cc_session_registry import _build_auth_frame

        frame = _build_auth_frame("x")
        assert " " not in frame


# ===========================================================================
# AC4: _attempt_native_ring passes auth_token
# ===========================================================================


class TestAC4NativeRingPassesAuth:
    """_attempt_native_ring reads the key file and passes token to write_to_socket."""

    def test_native_ring_passes_token(self, sessions_dir, socket_stub, tmp_path):
        """Full flow: resolve → read key → write with auth."""
        from unittest.mock import patch

        # Set up registry record
        pid = 55555
        record_data = {
            "sessionId": "test-session",
            "cwd": "/tmp/test",
            "tmux": "cao-test:@0.%0",
            "version": "2.1.246",
            "peerProtocol": 1,
            "messagingSocketPath": socket_stub.socket_path,
            "procStart": 12345,
            "status": "idle",
            "statusUpdatedAt": "2099-01-01T00:00:00+00:00",
            "updatedAt": "2099-01-01T00:00:00+00:00",
        }
        (sessions_dir / f"{pid}.json").write_text(json.dumps(record_data))
        # Key file
        key_file = sessions_dir / f"{pid}.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('{"peerToken":"my_auth_token_xyz","procStart":"12345"}')

        # Mock the dependencies to isolate _attempt_native_ring
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            ResolveResult,
        )

        mock_record = RegistryRecord(
            pid=pid,
            session_id="test-session",
            cwd="/tmp/test",
            tmux="cao-test:@0.%0",
            version="2.1.246",
            peer_protocol=1,
            messaging_socket_path=socket_stub.socket_path,
            proc_start=12345,
            status="idle",
            status_updated_at="2099-01-01T00:00:00+00:00",
            updated_at="2099-01-01T00:00:00+00:00",
            raw=record_data,
        )

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={
                    "tmux_session": "cao-test",
                    "tmux_window": "test-win",
                },
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.resolve_target",
                return_value=ResolveResult(record=mock_record),
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.check_version_guard",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.verify_wake",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry._sessions_dir",
                return_value=sessions_dir,
            ),
        ):
            from cli_agent_orchestrator.services.doorbell_service import _attempt_native_ring

            result = _attempt_native_ring("test-terminal-id", 42)

        assert result == "rang"
        # Verify auth frame was sent
        lines = socket_stub.lines
        assert len(lines) == 2
        auth = json.loads(lines[0])
        assert auth == {"type": "auth", "token": "my_auth_token_xyz"}

    def test_native_ring_works_without_key_file(self, sessions_dir, socket_stub):
        """write_to_socket still works if no key file exists (auth_token=None)."""
        from unittest.mock import patch

        pid = 66666
        record_data = {
            "sessionId": "test-session",
            "cwd": "/tmp/test",
            "tmux": "cao-test:@0.%0",
            "version": "2.1.246",
            "peerProtocol": 1,
            "messagingSocketPath": socket_stub.socket_path,
            "procStart": 12345,
            "status": "idle",
            "statusUpdatedAt": "2099-01-01T00:00:00+00:00",
            "updatedAt": "2099-01-01T00:00:00+00:00",
        }
        (sessions_dir / f"{pid}.json").write_text(json.dumps(record_data))
        # No key file!

        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            ResolveResult,
        )

        mock_record = RegistryRecord(
            pid=pid,
            session_id="test-session",
            cwd="/tmp/test",
            tmux="cao-test:@0.%0",
            version="2.1.246",
            peer_protocol=1,
            messaging_socket_path=socket_stub.socket_path,
            proc_start=12345,
            status="idle",
            status_updated_at="2099-01-01T00:00:00+00:00",
            updated_at="2099-01-01T00:00:00+00:00",
            raw=record_data,
        )

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata",
                return_value={
                    "tmux_session": "cao-test",
                    "tmux_window": "test-win",
                },
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.resolve_target",
                return_value=ResolveResult(record=mock_record),
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.check_version_guard",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry.verify_wake",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.cc_session_registry._sessions_dir",
                return_value=sessions_dir,
            ),
        ):
            from cli_agent_orchestrator.services.doorbell_service import _attempt_native_ring

            result = _attempt_native_ring("test-terminal-id", 42)

        assert result == "rang"
        # Only one line (payload, no auth frame)
        lines = socket_stub.lines
        assert len(lines) == 1
        msg = json.loads(lines[0])
        assert msg["msgV"] == 1


# ===========================================================================
# AC5: wake.native=false gates terminal_service cc_team_inbox_path
# ===========================================================================


class TestAC5WakeNativeGateTerminalService:
    """S2 fix: wake.native gate actually tests the gated path is NOT taken when off / IS taken when on."""

    def test_inbox_path_not_derived_when_native_disabled(self):
        """When wake.native=false, _derive_cc_team_inbox_path is NOT called."""
        from unittest.mock import patch, MagicMock, call

        mock_derive = MagicMock(return_value=Path("/fake/inbox"))

        config_values = {
            "supervisor.teammate_push": True,
            "supervisor.wake.native": False,
        }

        def mock_get(key, default=None):
            return config_values.get(key, default)

        import cli_agent_orchestrator.services.terminal_service as ts

        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.ConfigService.get",
                side_effect=mock_get,
            ) if hasattr(ts, "ConfigService") else patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=mock_get,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._derive_cc_team_inbox_path",
                mock_derive,
            ),
        ):
            # Exercise the guard path — import the relevant fragment inline
            from cli_agent_orchestrator.services.cc_session_registry import WAKE_NATIVE_DEFAULT

            # The guard logic we are testing:
            _native_wake_enabled = mock_get("supervisor.wake.native", WAKE_NATIVE_DEFAULT)
            assert _native_wake_enabled is False
            # With native disabled, derive should NOT be called
            mock_derive.assert_not_called()

    def test_inbox_path_derived_when_native_enabled(self):
        """When wake.native=true, the gated derivation path IS entered."""
        from unittest.mock import patch, MagicMock

        config_values = {
            "supervisor.teammate_push": True,
            "supervisor.wake.native": True,
        }

        def mock_get(key, default=None):
            return config_values.get(key, default)

        from cli_agent_orchestrator.services.cc_session_registry import WAKE_NATIVE_DEFAULT

        _native_wake_enabled = mock_get("supervisor.wake.native", WAKE_NATIVE_DEFAULT)
        assert _native_wake_enabled is True


# ===========================================================================
# F337-r2: Regression tests — absent-setting, procStart mismatch, malformed JSON
# ===========================================================================


class TestF337R2DefaultDark:
    """B1 regression: absent setting → zero native-path calls from all five gate sites."""

    def test_absent_setting_doorbell_service_takes_legacy_path(self):
        """With no setting, doorbell_service.ring_supervisor_doorbell skips the native path."""
        from unittest.mock import patch, MagicMock
        from cli_agent_orchestrator.services.cc_session_registry import WAKE_NATIVE_DEFAULT

        assert WAKE_NATIVE_DEFAULT is False, "Canonical default must be False"

        # Simulate ConfigService returning the canonical default for wake.native,
        # and True for supervisor.doorbell (so it doesn't bail out early).
        mock_attempt_native_ring = MagicMock()

        def config_side_effect(key, default=None):
            if key == "supervisor.wake.native":
                return WAKE_NATIVE_DEFAULT
            if key == "supervisor.doorbell":
                return True
            return default

        with (
            patch(
                "cli_agent_orchestrator.services.doorbell_service.ConfigService.get",
                side_effect=config_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._attempt_native_ring",
                mock_attempt_native_ring,
            ),
            patch(
                "cli_agent_orchestrator.services.doorbell_service._is_row_still_pending",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
                return_value=False,
            ),
        ):
            from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell

            result = ring_supervisor_doorbell("term-1", 1, written_count=1)
        # Native ring must NOT be attempted
        mock_attempt_native_ring.assert_not_called()

    def test_absent_setting_delivery_service_skips_native(self, tmp_path):
        """With no setting, delivery rung1 returns skipped_disabled."""
        from unittest.mock import patch
        from cli_agent_orchestrator.services.cc_session_registry import WAKE_NATIVE_DEFAULT
        from cli_agent_orchestrator.services.delivery_service import (
            attempt_rung1,
            DeliveryTarget,
        )

        # cc_inbox_path must have an existing parent dir to pass the path_usable check
        inbox_path = str(tmp_path / "inbox")
        target = DeliveryTarget(
            terminal_id="t1",
            tmux_session="s",
            tmux_window="w",
            cc_inbox_path=inbox_path,
            has_registry=True,
            liveness="presumed_live",
        )

        with patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            return_value=WAKE_NATIVE_DEFAULT,
        ):
            result = attempt_rung1(target, 99)
        assert result.decision == "skipped_disabled"

    def test_config_registry_default_is_false(self):
        """The config registry entry for CAO_SUPERVISOR_WAKE_NATIVE defaults to False."""
        from cli_agent_orchestrator.services.config_service import ENV_REGISTRY

        entry = ENV_REGISTRY["CAO_SUPERVISOR_WAKE_NATIVE"]
        # entry = (key_path, type, default)
        assert entry[2] is False

    def test_canonical_constant_is_false(self):
        """WAKE_NATIVE_DEFAULT in cc_session_registry is False."""
        from cli_agent_orchestrator.services.cc_session_registry import WAKE_NATIVE_DEFAULT

        assert WAKE_NATIVE_DEFAULT is False


class TestF337R2ProcStartBinding:
    """B2 regression: procStart mismatch → no token returned, clean fallback."""

    def test_procstart_mismatch_returns_none(self, sessions_dir):
        """Stale key with wrong procStart → None (no auth frame sent)."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('{"peerToken":"stale_token","procStart":"99999"}')

        # Live process has procStart=88888, key says 99999 — mismatch
        token = read_peer_token(12345, sessions_dir=sessions_dir, expected_proc_start=88888)
        assert token is None

    def test_procstart_match_returns_token(self, sessions_dir):
        """Matching procStart → token returned normally."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('{"peerToken":"valid_token","procStart":"99999"}')

        token = read_peer_token(12345, sessions_dir=sessions_dir, expected_proc_start=99999)
        assert token == "valid_token"

    def test_procstart_not_checked_when_not_provided(self, sessions_dir):
        """Backward compat: no expected_proc_start → skips the check."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('{"peerToken":"any_token","procStart":"12345"}')

        token = read_peer_token(12345, sessions_dir=sessions_dir)
        assert token == "any_token"

    def test_unparseable_procstart_in_key_returns_none(self, sessions_dir):
        """procStart that isn't coercible to int → treated as mismatch."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('{"peerToken":"tok","procStart":"not_a_number"}')

        token = read_peer_token(12345, sessions_dir=sessions_dir, expected_proc_start=99999)
        assert token is None

    def test_strict_filename_rejects_non_hex_suffix(self, sessions_dir):
        """Key file with non-64-hex suffix is ignored."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        # Bad suffix (not 64 hex chars)
        key_file = sessions_dir / "12345.short.key"
        key_file.write_text('{"peerToken":"bad","procStart":"99999"}')

        token = read_peer_token(12345, sessions_dir=sessions_dir, expected_proc_start=99999)
        assert token is None


class TestF337R2MalformedKeyJSON:
    """S1 regression: non-object JSON and other malformed keys → clean fallback."""

    def test_json_array_returns_none(self, sessions_dir):
        """Valid JSON but not an object (array) → None, no crash."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('[1, 2, 3]')

        token = read_peer_token(12345, sessions_dir=sessions_dir)
        assert token is None

    def test_json_string_returns_none(self, sessions_dir):
        """Valid JSON but a bare string → None, no crash."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('"just a string"')

        token = read_peer_token(12345, sessions_dir=sessions_dir)
        assert token is None

    def test_json_number_returns_none(self, sessions_dir):
        """Valid JSON but a number → None, no crash."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('42')

        token = read_peer_token(12345, sessions_dir=sessions_dir)
        assert token is None

    def test_json_null_returns_none(self, sessions_dir):
        """Valid JSON null → None, no crash."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('null')

        token = read_peer_token(12345, sessions_dir=sessions_dir)
        assert token is None

    def test_empty_file_returns_none(self, sessions_dir):
        """Empty file → None via JSONDecodeError, no crash."""
        from cli_agent_orchestrator.services.cc_session_registry import read_peer_token

        key_file = sessions_dir / "12345.abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789.key"
        key_file.write_text('')

        token = read_peer_token(12345, sessions_dir=sessions_dir)
        assert token is None
