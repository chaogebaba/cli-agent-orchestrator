"""F702 J4 (#473) — the fleet TUI window is created server-side at session start.

Two layers, matching the two halves of the fix:

1. ``fleet_window_service.ensure_fleet_window`` decides and creates: the
   ``CAO_FLEET_TUI`` opt-out, the absent ``[fleet]`` extra, an idempotent
   no-op when the window is already there, placement, and the total exception
   boundary that makes every failure a logged False.
2. ``session_service.create_session`` calls it for a supervisor session and
   not for a worker one, and a session start still succeeds when the window
   cannot be created.

**#786 — the seam these tests drive is the BACKEND PORT, not a tmux
subprocess.** The service used to run ``["tmux", ...]`` itself, so every test
here mocked ``subprocess.run`` and asserted on tmux argv. That seam is gone:
inventory is ``backend.enumerate_windows`` and creation is
``backend.create_window``. Each behaviour those argv assertions pinned is
re-pinned against the port below, and :class:`TestNeverExecutesTmux` adds the
property the old shape could not state at all — under a backend that is not
tmux, nothing shells out to tmux.
"""

import contextlib
import importlib.util
import os
import shutil
import subprocess
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, _patch, patch

import pytest

from cli_agent_orchestrator.backends import registry
from cli_agent_orchestrator.backends.base import TerminalBackend, TerminalBackendError
from cli_agent_orchestrator.services import fleet_window_service, session_service
from cli_agent_orchestrator.services.fleet_window_service import (
    FLEET_CONSOLE_SCRIPT,
    FLEET_TERMINAL_ID,
    FLEET_TUI_MODULE,
    FLEET_WINDOW_NAME,
    ensure_fleet_window,
    fleet_tui_enabled,
)
from cli_agent_orchestrator.services.session_service import create_session

CAO_FLEET_PATH = "/usr/local/bin/cao-fleet"


def _fleet_extra(present: bool = True) -> _patch[MagicMock]:
    """Patch the ``[fleet]`` extra probe: the textual spec is there, or is not."""
    return patch.object(
        importlib.util,
        "find_spec",
        MagicMock(return_value=MagicMock() if present else None),
    )


def _no_venv_script(tmp_path: Any) -> _patch[str]:
    """Point ``sys.executable`` at a dir with no ``cao-fleet`` beside it.

    The resolver (#633) probes ``Path(sys.executable).parent / "cao-fleet"``
    before ``shutil.which``. In a real venv that sibling exists, which would
    short-circuit before the PATH fallback the ``shutil.which`` tests exercise.
    Repointing ``sys.executable`` at an empty tmp dir forces resolution down to
    the PATH branch deterministically, independent of the test venv's layout.
    """
    return patch.object(
        fleet_window_service.sys,
        "executable",
        str(tmp_path / "python"),
    )


def _backend(
    *,
    windows: Any = ("ok", []),
    create: Any = None,
) -> MagicMock:
    """A backend double answering the two port methods this service uses.

    ``windows`` is returned verbatim from ``enumerate_windows`` so a test can
    hand over any of the port's three answers (``("ok", [...])``,
    ``("ok", [])``, ``("error", None)``) or an exception to raise. ``create``
    is the ``create_window`` return value, or an exception to raise.
    """
    backend = MagicMock(spec=TerminalBackend)
    if isinstance(windows, BaseException):
        backend.enumerate_windows.side_effect = windows
    else:
        backend.enumerate_windows.return_value = windows
    if isinstance(create, BaseException):
        backend.create_window.side_effect = create
    else:
        backend.create_window.return_value = create or FLEET_WINDOW_NAME
    return backend


def _named(*names: str) -> tuple[str, list[dict[str, object]]]:
    """An ``("ok", [...])`` inventory in the port's row shape."""
    return ("ok", [{"name": name} for name in names])


def _with_backend(backend: MagicMock) -> _patch[MagicMock]:
    return patch.object(fleet_window_service, "get_backend", MagicMock(return_value=backend))


def _create_kwargs(backend: MagicMock) -> tuple[tuple[Any, ...], dict[str, Any]]:
    assert backend.create_window.call_count == 1
    call = backend.create_window.call_args
    return call.args, call.kwargs


class TestOptOutFlag:
    """``CAO_FLEET_TUI`` keeps the exact semantics of the two shell guards."""

    @pytest.mark.parametrize(
        "env,expected",
        [
            (None, True),
            ({}, True),
            ({"CAO_FLEET_TUI": "1"}, True),
            ({"CAO_FLEET_TUI": "0"}, False),
            # Only the literal "0" disables — "false"/"no"/"" all stay enabled,
            # because fleet-tui-ensure.sh:12 compares against 0 and nothing else.
            ({"CAO_FLEET_TUI": "false"}, True),
            ({"CAO_FLEET_TUI": "no"}, True),
            ({"CAO_FLEET_TUI": ""}, True),
            ({"CAO_FLEET_TUI": "00"}, True),
        ],
    )
    def test_flag_semantics(self, env: dict[str, str] | None, expected: bool) -> None:
        assert fleet_tui_enabled(env) is expected

    def test_opt_out_touches_neither_path_nor_backend(self) -> None:
        get_backend = MagicMock()
        with (
            patch.object(shutil, "which") as mock_which,
            _fleet_extra() as mock_find_spec,
            patch.object(fleet_window_service, "get_backend", get_backend),
            patch.object(subprocess, "run") as mock_run,
        ):
            assert ensure_fleet_window("cao-x", {"CAO_FLEET_TUI": "0"}) is False

        mock_which.assert_not_called()
        mock_find_spec.assert_not_called()
        # The backend is never even RESOLVED, let alone asked anything — the
        # opt-out is decided before any multiplexer is in the picture.
        get_backend.assert_not_called()
        mock_run.assert_not_called()


class TestWindowCreation:
    """Placement, idempotence and the exact creation call."""

    def test_creates_through_the_port_when_the_session_has_room(self, tmp_path: Any) -> None:
        """The successor to the old ``creates at index 1 when free`` argv test.

        The old assertion was on ``tmux new-window -t cao-foreign:1``. #786
        moved creation onto ``backend.create_window``, which takes no index —
        and none is needed: the old code only asked for ``:1`` when index 1 was
        free, and both tmux and libtmux's empty-index target resolve to the
        lowest free index at or after base-index, which for a session holding
        only window 0 IS 1. So what stays pinned is the whole creation call:
        the session, the window name, the deliberately-empty terminal id
        (:data:`FLEET_TERMINAL_ID`), and the fleet command as ``window_shell``.
        """
        backend = _backend(windows=_named("chao_supervisor-abc123"))
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        backend.enumerate_windows.assert_called_once_with("cao-foreign")
        args, kwargs = _create_kwargs(backend)
        assert args == ("cao-foreign", FLEET_WINDOW_NAME, FLEET_TERMINAL_ID)
        assert kwargs == {"window_shell": f"{CAO_FLEET_PATH} --session cao-foreign"}

    def test_the_fleet_pane_is_given_no_resolvable_terminal_id(self, tmp_path: Any) -> None:
        """#786 (b): the fleet window is not a CAO terminal and must not claim one.

        ``create_window`` forces its ``terminal_id`` into the new pane's
        environment as ``CAO_TERMINAL_ID`` on every backend. The raw
        ``tmux new-window`` this replaced injected nothing, so the value passed
        has to be one no consumer can resolve: empty, which every reader's
        truthiness check treats exactly as absent and which can never equal an
        8-hex terminal id in ``purge_stale_terminal_records``' pane-identity
        sweep.
        """
        backend = _backend(windows=_named("supervisor-a"))
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        assert FLEET_TERMINAL_ID == ""
        args, _ = _create_kwargs(backend)
        assert args[2] == ""

    def test_creates_without_renumbering_when_index_1_is_taken(self, tmp_path: Any) -> None:
        """The successor to the old ``appends when index 1 is taken`` argv test.

        A worker already holds index 1. The old code switched its target from
        ``session:1`` to the bare session so the window was APPENDED rather
        than renumbering a live worker — shuffling a worker's index would break
        every ``session:index`` reference already handed out. The port call is
        index-free, so the guarantee is now structural: the service never names
        an index, never asks for a move, and issues the same single create.
        """
        backend = _backend(windows=_named("supervisor-a", "kiro_dev-b"))
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        args, kwargs = _create_kwargs(backend)
        assert args == ("cao-foreign", FLEET_WINDOW_NAME, FLEET_TERMINAL_ID)
        # No index, no target, no move-window: nothing that could renumber the
        # worker sitting at index 1.
        assert set(kwargs) == {"window_shell"}
        assert [call[0] for call in backend.method_calls] == [
            "enumerate_windows",
            "create_window",
        ]

    def test_existing_fleet_window_is_left_alone(self, tmp_path: Any) -> None:
        backend = _backend(windows=_named("supervisor-a", FLEET_WINDOW_NAME))
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

        backend.create_window.assert_not_called()

    def test_window_name_is_matched_exactly_not_by_prefix(self, tmp_path: Any) -> None:
        """A window called ``fleet-notes`` is not the fleet window."""
        backend = _backend(windows=_named("supervisor-a", "fleet-notes"))
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        backend.create_window.assert_called_once()


class TestNeverRaises:
    """Every failure mode is a logged False, never an exception."""

    def test_absent_console_script_is_a_no_op(self, tmp_path: Any) -> None:
        """No ``cao-fleet`` beside the interpreter or on PATH: the backend is untouched."""
        get_backend = MagicMock()
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=None) as mock_which,
            patch.object(fleet_window_service, "get_backend", get_backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

        mock_which.assert_called_once_with(FLEET_CONSOLE_SCRIPT)
        get_backend.assert_not_called()

    def test_absent_fleet_extra_is_a_no_op(self, tmp_path: Any) -> None:
        """The script is on PATH but textual is not: a server-only install.

        pyproject declares ``cao-fleet`` unconditionally, so PATH alone does not
        prove the extra is installed; without this probe the session would get a
        window that opens only to print an install hint and die.
        """
        get_backend = MagicMock()
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(present=False) as mock_find_spec,
            patch.object(fleet_window_service, "get_backend", get_backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

        mock_find_spec.assert_called_once_with(FLEET_TUI_MODULE)
        get_backend.assert_not_called()

    def test_inventory_error_creates_nothing(self, tmp_path: Any) -> None:
        """An unknown inventory must not be guessed at.

        The old test drove this with ``tmux list-windows`` exiting non-zero on
        ``no server running``. On the port that classification is the backend's
        job and its verdict is ``("error", None)`` — which is also what a
        backend with no ``enumerate_windows`` of its own inherits from
        ``base.py``, so this single case covers "the read failed" and "this
        backend cannot answer" alike.
        """
        backend = _backend(windows=("error", None))
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

        backend.create_window.assert_not_called()

    def test_absent_session_creates_nothing(self, tmp_path: Any) -> None:
        """``("ok", [])`` is the port's "no such session", not "an empty session".

        The module has always refused to read an empty listing as a green
        light: a live session with zero windows cannot exist. On the port that
        answer is explicit, and it must still create nothing — creating into a
        session that is not there is how the pre-#786 code could have landed a
        window on the WRONG tmux server.
        """
        backend = _backend(windows=("ok", []))
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

        backend.create_window.assert_not_called()

    def test_create_window_failure_returns_false(self, tmp_path: Any) -> None:
        backend = _backend(
            windows=_named("supervisor-a"),
            create=TerminalBackendError("can't create window"),
        )
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

    @pytest.mark.parametrize(
        "boom",
        [
            FileNotFoundError("tmux"),
            OSError("cannot fork"),
            RuntimeError("something unforeseen"),
        ],
    )
    def test_backend_explosion_is_swallowed(self, boom: Exception, tmp_path: Any) -> None:
        backend = _backend(windows=boom)
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

    def test_backend_resolution_explosion_is_swallowed(self, tmp_path: Any) -> None:
        """``get_backend()`` itself can raise (no backend configured)."""
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(
                fleet_window_service,
                "get_backend",
                MagicMock(side_effect=RuntimeError("no backend")),
            ),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False

    def test_which_explosion_is_swallowed(self, tmp_path: Any) -> None:
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", MagicMock(side_effect=RuntimeError("boom"))),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is False


class _PortOnlyBackend(TerminalBackend):
    """A backend that supplies nothing but the port's own defaults.

    ``__abstractmethods__`` is emptied below the class (ABCMeta recomputes it
    during class creation, so a class-body assignment would be overwritten) and
    every abstract method keeps its ``...`` body. Crucially ``enumerate_windows``
    is NOT overridden, so it answers with ``base.py``'s ``("error", None)`` —
    exactly HerdrBackend's position today. ``create_window`` is overridden only
    to fail loudly, because reaching it would mean the bail did not happen.
    """

    def create_window(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("create_window must not be reached on an unreadable inventory")


_PortOnlyBackend.__abstractmethods__ = frozenset()


class TestNeverExecutesTmux:
    """#786: a non-tmux deployment must not spawn a tmux process. Ever."""

    @staticmethod
    @contextlib.contextmanager
    def _no_subprocess() -> Iterator[list[Any]]:
        """Trip on ANY subprocess spawn, whatever the argv."""
        spawned: list[Any] = []

        def explode(argv: Any = None, *args: Any, **kwargs: Any) -> Any:
            spawned.append(argv)
            raise AssertionError(f"a subprocess was spawned: {argv!r}")

        with contextlib.ExitStack() as stack:
            for name in ("run", "Popen", "call", "check_call", "check_output"):
                stack.enter_context(patch.object(subprocess, name, explode))
            yield spawned

    def test_non_tmux_backend_spawns_no_process_at_all(self, tmp_path: Any) -> None:
        """The whole point of #786, driven through the REAL registry lookup.

        The service resolves its backend with the production ``get_backend``;
        the registry's module-level singleton is swapped for a backend that
        implements only the port's defaults. Nothing may be executed: the
        inventory read fails closed at ``("error", None)`` and the call bails
        before any creation. Before the fix this path ran
        ``subprocess.run(["tmux", "list-windows", ...])`` on every supervisor
        session start of a herdr deployment.
        """
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            # mypy reads the ABC statically and cannot see the emptied
            # __abstractmethods__ that makes this instantiable at runtime.
            patch.object(registry, "_backend", _PortOnlyBackend()),  # type: ignore[abstract]
            self._no_subprocess() as spawned,
        ):
            assert ensure_fleet_window("cao-herdr", {}) is False

        assert spawned == []

    def test_the_creating_path_spawns_no_process_either(self, tmp_path: Any) -> None:
        """Even the happy path executes nothing itself — the backend owns that."""
        backend = _backend(windows=_named("supervisor-a"))
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            _with_backend(backend),
            self._no_subprocess() as spawned,
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        assert spawned == []
        backend.create_window.assert_called_once()

    def test_the_module_no_longer_carries_a_tmux_execution_seam(self) -> None:
        """``subprocess`` is not even imported here any more.

        ``test_g7a_sandbox.test_tmux_ast_guard_is_closed`` bans a raw
        ``["tmux", ...]`` argv statically; this pins the complementary runtime
        fact, so an accidental re-import of the old seam is caught from both
        sides.
        """
        assert not hasattr(fleet_window_service, "subprocess")
        assert not hasattr(fleet_window_service, "_run_tmux")


class TestConsoleScriptResolution:
    """#633 — resolve ``cao-fleet`` beside ``sys.executable`` before PATH.

    cao-server is a systemd user unit whose PATH is the systemd default
    (``/usr/local/bin:/usr/bin``), so the venv ``bin`` is not on it and a
    ``shutil.which`` alone finds nothing — every server-side fleet window was
    skipped. The console script lives beside the interpreter running the
    server, so that sibling is probed first.
    """

    @staticmethod
    def _make_fake_script(directory: Any) -> Any:
        """Create an executable ``cao-fleet`` beside a fake interpreter."""
        script = directory / FLEET_CONSOLE_SCRIPT
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        return script

    def test_resolves_venv_script_with_empty_path(self, tmp_path: Any) -> None:
        """PATH is empty; the script beside ``sys.executable`` still reaches the backend."""
        venv_bin = tmp_path / "bin"
        venv_bin.mkdir()
        script = self._make_fake_script(venv_bin)
        backend = _backend(windows=_named("chao_supervisor-abc123"))
        with (
            patch.object(fleet_window_service.sys, "executable", str(venv_bin / "python")),
            patch.dict(os.environ, {"PATH": ""}, clear=False),
            patch.object(shutil, "which", return_value=None) as mock_which,
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        # The venv sibling won, so the PATH fallback was never consulted.
        mock_which.assert_not_called()
        _, kwargs = _create_kwargs(backend)
        assert kwargs["window_shell"] == f"{script} --session cao-foreign"

    def test_non_executable_venv_sibling_falls_back_to_path(self, tmp_path: Any) -> None:
        """A ``cao-fleet`` beside the interpreter that is not executable is skipped."""
        venv_bin = tmp_path / "bin"
        venv_bin.mkdir()
        script = venv_bin / FLEET_CONSOLE_SCRIPT
        script.write_text("#!/bin/sh\n")
        script.chmod(0o644)  # not executable
        backend = _backend(windows=_named("supervisor-a"))
        with (
            patch.object(fleet_window_service.sys, "executable", str(venv_bin / "python")),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH) as mock_which,
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        mock_which.assert_called_once_with(FLEET_CONSOLE_SCRIPT)
        _, kwargs = _create_kwargs(backend)
        assert kwargs["window_shell"] == f"{CAO_FLEET_PATH} --session cao-foreign"

    def test_missing_venv_script_falls_back_to_path(self, tmp_path: Any) -> None:
        """No script beside the interpreter: resolution falls back to PATH."""
        backend = _backend(windows=_named("supervisor-a"))
        with (
            _no_venv_script(tmp_path),
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH) as mock_which,
            _fleet_extra(),
            _with_backend(backend),
        ):
            assert ensure_fleet_window("cao-foreign", {}) is True

        mock_which.assert_called_once_with(FLEET_CONSOLE_SCRIPT)
        _, kwargs = _create_kwargs(backend)
        assert kwargs["window_shell"] == f"{CAO_FLEET_PATH} --session cao-foreign"


def _supervisor_session_patches(terminal: MagicMock) -> tuple[_patch[Any], ...]:
    """The mock stack a supervisor ``create_session`` needs to reach the end."""
    return (
        patch.object(session_service, "create_terminal", AsyncMock(return_value=terminal)),
        patch.object(
            session_service,
            "load_agent_profile",
            MagicMock(return_value=MagicMock(role="supervisor")),
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
            AsyncMock(return_value=None),
        ),
        patch(
            "cli_agent_orchestrator.services.mailbox_service.claim_mailbox",
            MagicMock(return_value=MagicMock(session_name="cao-f702", role="supervisor")),
        ),
        patch(
            "cli_agent_orchestrator.services.mailbox_service.publish_supervisor_incarnation",
            MagicMock(return_value={"mailbox_id": "mb-1", "generation": 1}),
        ),
        patch("cli_agent_orchestrator.services.inbox_service.inbox_service", MagicMock()),
        patch.object(session_service, "_reconcile_inbox_path_on_publish", AsyncMock()),
        patch.object(session_service, "dispatch_plugin_event", MagicMock()),
    )


@contextlib.contextmanager
def _supervisor_session(terminal: MagicMock, *extra: _patch[Any]) -> Iterator[None]:
    """Enter the supervisor mock stack plus any test-specific patches."""
    with contextlib.ExitStack() as stack:
        for patcher in (*_supervisor_session_patches(terminal), *extra):
            stack.enter_context(patcher)
        yield


class TestCreateSessionWiring:
    """``create_session`` is the repo-agnostic choke point (#473)."""

    @pytest.mark.asyncio
    async def test_supervisor_session_gets_the_window(self) -> None:
        terminal = MagicMock(id="f7020001", session_name="cao-f702")
        ensure = MagicMock(return_value=True)
        with _supervisor_session(
            terminal,
            patch.object(fleet_window_service, "ensure_fleet_window", ensure),
        ):
            result = await create_session(
                provider="kiro_cli",
                agent_profile="chao_supervisor",
                env_vars={"F702_PROBE": "sentinel"},
            )

        assert result is terminal
        # Called with the session actually created and the canonical session
        # env — the channel the --env opt-out travels on.
        ensure.assert_called_once()
        session_name, env = ensure.call_args.args
        assert session_name == "cao-f702"
        # An operator-forwarded var survives verbatim, and the canonical floor
        # is present. Asserting the probe rather than the artifact root keeps
        # this independent of any path remapping the test environment applies.
        assert env["F702_PROBE"] == "sentinel"
        assert "CAO_ARTIFACTS_DIR" in env

    @pytest.mark.asyncio
    async def test_opt_out_env_reaches_the_service_and_creates_nothing(self) -> None:
        """``--env CAO_FLEET_TUI=0`` → request env_vars → no window (#473 AC3b)."""
        terminal = MagicMock(id="f7020002", session_name="cao-f702-optout")
        get_backend = MagicMock()
        with _supervisor_session(
            terminal,
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(fleet_window_service, "get_backend", get_backend),
        ):
            result = await create_session(
                provider="kiro_cli",
                agent_profile="chao_supervisor",
                env_vars={"CAO_FLEET_TUI": "0"},
            )

        assert result is terminal
        get_backend.assert_not_called()

    @pytest.mark.asyncio
    async def test_worker_session_gets_no_window(self) -> None:
        terminal = MagicMock(id="f7020003", session_name="cao-f702-worker")
        ensure = MagicMock(return_value=True)
        with (
            patch.object(session_service, "create_terminal", AsyncMock(return_value=terminal)),
            patch.object(
                session_service,
                "load_agent_profile",
                MagicMock(return_value=MagicMock(role="worker")),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
                AsyncMock(return_value=None),
            ),
            patch.object(session_service, "dispatch_plugin_event", MagicMock()),
            patch.object(fleet_window_service, "ensure_fleet_window", ensure),
        ):
            await create_session(provider="kiro_cli", agent_profile="developer")

        ensure.assert_not_called()

    @pytest.mark.asyncio
    async def test_absent_fleet_extra_does_not_crash_session_creation(self) -> None:
        """The real service runs: no ``cao-fleet`` binary, session still starts."""
        terminal = MagicMock(id="f7020004", session_name="cao-f702-noextra")
        get_backend = MagicMock()
        with _supervisor_session(
            terminal,
            patch.object(shutil, "which", return_value=None),
            _fleet_extra(present=False),
            patch.object(fleet_window_service, "get_backend", get_backend),
        ):
            result = await create_session(provider="kiro_cli", agent_profile="chao_supervisor")

        assert result is terminal
        get_backend.assert_not_called()

    @pytest.mark.asyncio
    async def test_backend_explosion_does_not_crash_session_creation(self) -> None:
        """The real service runs and the backend blows up: the session still starts."""
        terminal = MagicMock(id="f7020005", session_name="cao-f702-nobackend")
        with _supervisor_session(
            terminal,
            patch.object(shutil, "which", return_value=CAO_FLEET_PATH),
            _fleet_extra(),
            patch.object(
                fleet_window_service,
                "get_backend",
                MagicMock(side_effect=FileNotFoundError("no multiplexer")),
            ),
        ):
            result = await create_session(provider="kiro_cli", agent_profile="chao_supervisor")

        assert result is terminal

    @pytest.mark.asyncio
    async def test_missing_profile_gets_no_window(self) -> None:
        """An unknown profile (load raises FileNotFoundError) is not a supervisor."""
        terminal = MagicMock(id="f7020006", session_name="cao-f702-noprofile")
        ensure = MagicMock(return_value=True)
        with (
            patch.object(session_service, "create_terminal", AsyncMock(return_value=terminal)),
            patch.object(
                session_service,
                "load_agent_profile",
                MagicMock(side_effect=FileNotFoundError("no such profile")),
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
                AsyncMock(return_value=None),
            ),
            patch.object(session_service, "dispatch_plugin_event", MagicMock()),
            patch.object(fleet_window_service, "ensure_fleet_window", ensure),
        ):
            await create_session(provider="kiro_cli", agent_profile="ghost")

        ensure.assert_not_called()
