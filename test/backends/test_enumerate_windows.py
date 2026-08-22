"""Unit tests for TmuxBackend.enumerate_windows (subprocess-only path)."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend


@pytest.fixture
def backend() -> TmuxBackend:
    return TmuxBackend(client=MagicMock())


class TestEnumerateWindowsReturncode0:
    def test_parseable_stdout_returns_ok(self, backend: TmuxBackend) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="win-a\nwin-b\n", stderr=""
        )
        with patch("subprocess.run", return_value=fake):
            state, windows = backend.enumerate_windows("cao-session")
        assert state == "ok"
        assert windows == [{"name": "win-a"}, {"name": "win-b"}]

    def test_empty_stdout_returns_ok_empty_list(self, backend: TmuxBackend) -> None:
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=fake):
            state, windows = backend.enumerate_windows("cao-session")
        assert state == "ok"
        assert windows == []

    def test_filters_blank_lines(self, backend: TmuxBackend) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="win-a\n\nwin-b\n\n", stderr=""
        )
        with patch("subprocess.run", return_value=fake):
            state, windows = backend.enumerate_windows("cao-session")
        assert state == "ok"
        assert windows == [{"name": "win-a"}, {"name": "win-b"}]


class TestEnumerateWindowsCantFindSession:
    def test_session_not_found(self, backend: TmuxBackend) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="can't find session: foo\n"
        )
        with patch("subprocess.run", return_value=fake):
            state, windows = backend.enumerate_windows("foo")
        assert state == "ok"
        assert windows == []

    def test_no_server_running(self, backend: TmuxBackend) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no server running on /tmp/tmux-1000/default\n"
        )
        with patch("subprocess.run", return_value=fake):
            state, windows = backend.enumerate_windows("foo")
        assert state == "ok"
        assert windows == []


class TestEnumerateWindowsUnrecognizedError:
    def test_unrecognized_stderr_returns_error(self, backend: TmuxBackend) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="permission denied\n"
        )
        with patch("subprocess.run", return_value=fake):
            state, windows = backend.enumerate_windows("cao-session")
        assert state == "error"
        assert windows is None


class TestEnumerateWindowsTimeout:
    def test_timeout_returns_error(self, backend: TmuxBackend) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tmux", 10)):
            state, windows = backend.enumerate_windows("cao-session")
        assert state == "error"
        assert windows is None


class TestEnumerateWindowsGenericException:
    def test_generic_exception_returns_error(self, backend: TmuxBackend) -> None:
        with patch("subprocess.run", side_effect=OSError("broken")):
            state, windows = backend.enumerate_windows("cao-session")
        assert state == "error"
        assert windows is None
