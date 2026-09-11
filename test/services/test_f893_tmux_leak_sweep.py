"""F893 (#745) bug-family sweep: backend-neutral code must not call tmux.

#733 (launch health) and #745 (F829 runtime-identity capture) were the same
defect twice: a shared service resolved a pane fact by shelling out to tmux, so
under the herdr backend it raised ``CalledProcessError`` (or silently degraded)
on an ordinary round. These tests pin the whole family — the ported call sites
ask the configured backend, and the two sites that stay tmux-only do so behind a
capability gate or an unreachable-under-herdr guard.
"""

from __future__ import annotations

import ast
import subprocess
from unittest.mock import MagicMock, Mock, patch

import pytest


def _exploding_pane_pid(*_a, **_k):
    """Stand-in for the herdr reality: tmux is not there."""
    raise subprocess.CalledProcessError(1, ["tmux", "list-panes"])


class TestRuntimeIdentityUsesThePort:
    """#745 proper: _prepare_provider_runtime_identity (terminal_service)."""

    def _prepare(self, monkeypatch, backend):
        from cli_agent_orchestrator.services.terminal_service import (
            _prepare_provider_runtime_identity,
        )

        provider = Mock()
        provider.supports_reauth_rebind = True
        provider.shell_baseline = "bash"
        provider.allocated_session_uuid = None
        provider.resume_session_uuid.return_value = "uuid-from-resume"
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda _tid: {"tmux_session": "s", "tmux_window": "w", "shell_command": None},
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_backend",
            lambda: backend,
        )
        # tmux is absent, exactly as on a herdr host.
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            _exploding_pane_pid,
        )
        return _prepare_provider_runtime_identity(provider, "t1", settlement_form="first_time")

    def test_resolves_the_pid_through_the_backend_not_tmux(self, monkeypatch):
        backend = Mock()
        backend.get_pane_process_id.return_value = 4242
        backend.get_pane_working_directory.return_value = "/work"

        result = self._prepare(monkeypatch, backend)

        assert result is not None
        backend.get_pane_process_id.assert_called_once_with("s", "w")

    def test_the_pid_the_backend_returns_is_the_one_handed_to_capture(self, monkeypatch):
        """Mutation guard: a backend pid that is ignored would break codex capture."""
        from cli_agent_orchestrator.services.terminal_service import (
            _prepare_provider_runtime_identity,
        )

        backend = Mock()
        backend.get_pane_process_id.return_value = 4242
        backend.get_pane_working_directory.return_value = "/work"
        provider = Mock()
        provider.supports_reauth_rebind = True
        provider.shell_baseline = "bash"
        provider.allocated_session_uuid = None
        provider.resume_session_uuid.return_value = None
        provider.capture_session_uuid.return_value = "captured"
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda _tid: {"tmux_session": "s", "tmux_window": "w", "shell_command": None},
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_backend", lambda: backend
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_launch_epoch",
            lambda pid: 100.0 + pid,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.fork_context_service.pane_pid",
            _exploding_pane_pid,
        )

        _prepare_provider_runtime_identity(provider, "t1", settlement_form="first_time")

        provider.capture_session_uuid.assert_called_once_with(4242, 4342.0, "/work")


# WP-ARCH 3c K7: ``TestDeliveryNudgeUsesThePort`` is GONE with its subject. It
# parsed the AST of ``delivery_service.attempt_rung2`` and asserted that every
# ``send_keys`` call inside it went through ``get_backend()`` rather than through
# ``clients.tmux`` directly — the #745 family rule applied to the rung-2 nudge.
# K7 deletes the whole ladder: ``attempt_rung2`` is gone, and with it the only
# ``send_keys`` this module ever issued. ``delivery_service`` is now 52 lines
# holding one DB predicate, with no subprocess call, no backend call and no tmux
# import of any kind.
#
# The family rule is untouched and is asserted on every site that still has a
# pane to write to — the arms above and below this note. An arm re-pointed at the
# reduced module would be asserting that a file which calls nothing calls nothing
# through the wrong door, which is the vacuity the family guard exists to avoid.


class TestFifoScopeProbeUsesTheConfiguredBackend:
    def test_no_hardcoded_tmux_backend_in_fifo_reader(self):
        from cli_agent_orchestrator.services import fifo_reader

        # AST, not text: prose about the old defect must not trip the guard.
        assert _instantiations(fifo_reader, "TmuxBackend") == 0
        assert _imported_names(fifo_reader, "cli_agent_orchestrator.clients.tmux") == set()


class TestStatusDecorationIsGated:
    """@cao_pending is tmux-only BY DESIGN — but it must not run elsewhere."""

    def test_no_tmux_call_when_backend_lacks_decorations(self, monkeypatch):
        from cli_agent_orchestrator.services.boundary_pull_service import BoundaryPullService

        backend = MagicMock()
        backend.supports_status_decorations.return_value = False
        monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
        with patch("subprocess.run") as run:
            BoundaryPullService._write_tmux_pending(MagicMock(), "cao-x", 3)
        run.assert_not_called()

    def test_tmux_call_still_happens_when_the_backend_has_them(self, monkeypatch):
        from cli_agent_orchestrator.services.boundary_pull_service import BoundaryPullService

        backend = MagicMock()
        backend.supports_status_decorations.return_value = True
        monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
            BoundaryPullService._write_tmux_pending(MagicMock(), "cao-x", 3)
        run.assert_called_once()
        assert "@cao_pending" in run.call_args.args[0]


class TestNoBackendNeutralModuleImportsPanePid:
    """Static family guard: the ported sites must not regress to pane_pid."""

    PORTED = (
        "services/terminal_service.py",
        "services/session_manifest_service.py",
        "services/message_trace_service.py",
        "services/provider_rebind_service.py",
        "providers/grok_cli.py",
    )

    def test_no_ported_site_imports_pane_pid(self):
        from pathlib import Path

        import cli_agent_orchestrator

        root = Path(cli_agent_orchestrator.__file__).parent
        offenders = []
        for rel in self.PORTED:
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "pane_pid" for alias in node.names
                ):
                    offenders.append(f"{rel}: import")
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "id", None) or getattr(func, "attr", None)
                    if name == "pane_pid":
                        offenders.append(f"{rel}: call")
        assert offenders == [], f"pane_pid re-entered backend-neutral code: {offenders}"


def _source_of(func) -> str:
    import inspect

    return inspect.getsource(func)


def _module_source(module) -> str:
    from pathlib import Path

    return Path(module.__file__).read_text(encoding="utf-8")


def _instantiations(module, class_name: str) -> int:
    tree = ast.parse(_module_source(module))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == class_name
    )


def _imported_names(module, module_path: str) -> set:
    tree = ast.parse(_module_source(module))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module_path
        for alias in node.names
    }
