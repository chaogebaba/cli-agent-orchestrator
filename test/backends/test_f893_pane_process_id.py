"""F893 (#745): the runtime-identity process root goes through the backend port.

Fail-before/pass-after for the bug found live on grok-box-009: every
``supports_reauth_rebind`` provider's deferred init reached
``fork_context_service.pane_pid`` → ``tmux list-panes`` and died with
``CalledProcessError`` under the herdr backend. The port method
``TerminalBackend.get_pane_process_id`` answers per backend; tmux keeps its
exact former behaviour and herdr reads ``herdr pane process-info``.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.base import (
    TerminalBackend,
    TerminalBackendError,
    TerminalNotFoundError,
)
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

# The real herdr 0.9.0 (protocol 22) process-info body, captured live on
# grok-box-009 — the list is nested under result.process_info.
HERDR_0_9_0_PROCESS_INFO = (
    '{"id":41,"result":{"process_info":{"foreground_processes":'
    '[{"pid":4242,"name":"grok","cmdline":"grok"}]}}}'
)


class TestPortContract:
    def test_base_raises_so_an_unported_backend_is_loud(self):
        class Bare(TerminalBackend):
            pass

        with pytest.raises(NotImplementedError, match="get_pane_process_id"):
            TerminalBackend.get_pane_process_id(object(), "s", "w")  # type: ignore[arg-type]

    def test_both_shipped_backends_override_it(self):
        for cls in (TmuxBackend, HerdrBackend):
            assert cls.get_pane_process_id is not TerminalBackend.get_pane_process_id


class TestTmuxGetPaneProcessId:
    def test_returns_first_pane_pid_verbatim(self):
        with patch(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            return_value=777,
        ) as pane_pid:
            assert TmuxBackend().get_pane_process_id("sess", "win") == 777
        pane_pid.assert_called_once_with("sess", "win")

    def test_propagates_the_same_failure_callers_used_to_see(self):
        with patch(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            side_effect=subprocess.CalledProcessError(1, ["tmux", "list-panes"]),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                TmuxBackend().get_pane_process_id("sess", "win")


class TestHerdrGetPaneProcessId:
    def _backend(self):
        backend = HerdrBackend.__new__(HerdrBackend)
        backend._resolve_pane_id_from_window = MagicMock(return_value="pane-1")  # type: ignore[method-assign]
        return backend

    def test_reads_real_0_9_0_process_info_nesting(self):
        backend = self._backend()
        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=0, stdout=HERDR_0_9_0_PROCESS_INFO, stderr="")
        )
        assert backend.get_pane_process_id("sess", "win") == 4242

    def test_skips_entries_without_a_usable_pid(self):
        backend = self._backend()
        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                returncode=0,
                stdout=(
                    '{"result":{"process_info":{"foreground_processes":'
                    '[{"name":"bash"},{"pid":0,"name":"x"},{"pid":91,"name":"grok"}]}}}'
                ),
                stderr="",
            )
        )
        assert backend.get_pane_process_id("sess", "win") == 91

    def test_empty_seat_raises_backend_error_not_a_bogus_pid(self):
        backend = self._backend()
        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                returncode=0,
                stdout='{"result":{"process_info":{"foreground_processes":[]}}}',
                stderr="",
            )
        )
        with pytest.raises(TerminalBackendError, match="no foreground process pid"):
            backend.get_pane_process_id("sess", "win")

    def test_process_info_error_raises_backend_error(self):
        backend = self._backend()
        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=3, stdout="", stderr="pane not found")
        )
        with pytest.raises(TerminalBackendError, match="process-info failed"):
            backend.get_pane_process_id("sess", "win")

    def test_unresolvable_pane_propagates_not_found(self):
        backend = HerdrBackend.__new__(HerdrBackend)
        backend._resolve_pane_id_from_window = MagicMock(  # type: ignore[method-assign]
            side_effect=TerminalNotFoundError("sess:win")
        )
        with pytest.raises(TerminalNotFoundError):
            backend.get_pane_process_id("sess", "win")

    def test_never_shells_out_to_tmux(self):
        backend = self._backend()
        backend._run_herdr = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=0, stdout=HERDR_0_9_0_PROCESS_INFO, stderr="")
        )
        backend.get_pane_process_id("sess", "win")
        for call in backend._run_herdr.call_args_list:
            assert "tmux" not in " ".join(call.args[0])


class TestStatusDecorationCapability:
    def test_tmux_advertises_status_decorations(self):
        assert TmuxBackend().supports_status_decorations() is True

    def test_herdr_does_not(self):
        assert HerdrBackend.__new__(HerdrBackend).supports_status_decorations() is False
